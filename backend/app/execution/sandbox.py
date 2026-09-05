"""Deterministic SeededSandbox — evaluation/demo provider.

IMPORTANT (honesty contract, docs/SANDBOX.md): outcomes here are a DETERMINISTIC
simulation of how the real world might respond. They are seeded purely from case
data (customer.behavior_profile + invoice id + cause + attempt number) so every
run is reproducible. These are NOT real Razorpay results and are never
presented as such.

TWO outcome models:

1) Structured (payment-failure / dunning) cases use the *behaviour-profile*
   model: the counterparty's disposition (pays after a reminder/link/retry)
   is a pure function of the persisted profile:
     retry (attempt N) for technical/insufficient-funds errors succeeds at a
     deterministic attempt; outreach pays per profile+step; never_pays never.

2) Unstructured (communication) scenarios use the *scenario-gated* model:
   the counterparty's situation (documentation blocker, AP queue, explicit
   promise, dispute) is seeded as the behavior_profile, and the world responds
   ONLY to the intervention the situation actually calls for:
       docs_issue / docs_issue_latepay -> resend_corrected_invoice
       ap_queue                       -> request_ap_update
       ptp_offer_will_pay             -> confirm_ptp (pays on the promised date)
       dispute / refusal / never_pays -> nothing ever pays
   Any OTHER outbound action succeeds as a dispatch but produces NO simulated
   payment — the wrong intervention fails. This is what makes evaluation
   measure intervention QUALITY (correct action vs wrong action) instead of
   rewarding generic outreach. The world never reads the diagnosis or the
   LLM's reasoning — only the actually executed action and the case seeds.

Payment offsets are deterministic: either seeded explicitly per case
(invoice.meta["scenario_offset_hours"]) or derived from a hash of the invoice
id, so identical runs give identical money-timing.
"""
from __future__ import annotations

from datetime import datetime
from hashlib import sha256

from app import constants as C
from app.execution.provider import PaymentProvider
from app.models import Invoice

_RETRY_OK = {
    C.CAUSE_TECHNICAL: lambda attempt: attempt >= 1,
    C.CAUSE_INSUFFICIENT_FUNDS: lambda attempt: attempt >= 2,
}

# Structured world: behaviour profile -> (action, required step, offset hours)
_PROFILE_PAYS = {
    "pays_after_reminder": ("send_reminder", 1, 2.0),
    "pays_after_second_reminder": ("send_reminder", 2, 2.0),
    "pays_after_link": ("create_payment_link", 1, 6.0),
    "pays_after_followup": ("ptp_followup", 1, 2.0),
}

# Scenario world: profile -> set of actions that actually settle the invoice.
# Offsets default to a deterministic 24-72h window unless the scenario seeds
# invoice.meta["scenario_offset_hours"] (e.g. a late-paying documentation case
# whose settlement lands beyond the eval horizon).
_SCENARIO_PAYS = {
    "docs_issue": {"resend_corrected_invoice"},
    "docs_issue_latepay": {"resend_corrected_invoice"},
    "ap_queue": {"request_ap_update"},
    "ptp_offer_will_pay": {"confirm_ptp"},
}


class SeededSandbox(PaymentProvider):
    name = "seeded_sandbox"

    def _ref(self, kind: str, *parts) -> str:
        raw = ":".join([kind, *map(str, parts)])
        return "sim_" + sha256(raw.encode("utf-8")).hexdigest()[:18]

    def _pending(self, invoice, event_type: str, offset_hours: float) -> dict:
        from app.clock import now

        amount = invoice.outstanding_minor
        return {
            "event_type": event_type,
            "amount_minor": amount,
            "offset_hours": offset_hours,
            "payment_id": self._ref("pay", invoice.rzr_invoice_id, event_type, str(offset_hours)),
        }

    # ------------------------------------------------------------------ world
    def _pays_on(self, profile: str, action: str, attempt: int | None) -> bool:
        if profile in _PROFILE_PAYS:
            spec = _PROFILE_PAYS[profile]
            return spec[0] == action and (attempt is None or spec[1] == attempt)
        if profile in _SCENARIO_PAYS:
            return action in _SCENARIO_PAYS[profile]
        return False

    def _scenario_offset_hours(self, invoice: Invoice) -> float:
        seeded = (invoice.meta or {}).get("scenario_offset_hours")
        if seeded:
            return float(seeded)
        digest = sha256(f"off:{invoice.rzr_invoice_id}".encode("utf-8")).hexdigest()
        return float(24 + int(digest, 16) % 49)  # deterministic 24..72h

    def _world_dispatch(self, invoice, customer, action: str, *, attempt: int | None = None, offset_hours: float | None = None) -> dict:
        """Pure deterministic counterparty-response for outreach actions.

        The world pays ONLY when the executed action is the one the scenario
        calls for (or, for structured profiles, the one the disposition says);
        any other dispatch succeeds but changes nothing.
        """
        profile = customer.behavior_profile
        pays = self._pays_on(profile, action, attempt)
        pending = None
        if pays:
            if offset_hours is None:
                offset_hours = (
                    _PROFILE_PAYS[profile][2]
                    if profile in _PROFILE_PAYS
                    else self._scenario_offset_hours(invoice)
                )
            pending = self._pending(invoice, "invoice.paid", offset_hours)
        return {
            "ok": True,
            "provider_ref": self._ref(action, invoice.rzr_invoice_id),
            "raw": {"action": action, "profile": profile, "scenario": (invoice.meta or {}).get("eval_scenario", "")},
            "pending_payment": pending,
            "failure_code": "",
        }

    # ------------------------------------------------------------------ ops
    def send_reminder(self, invoice, customer, *, template, channel, amount_minor) -> dict:
        res = self._world_dispatch(invoice, customer, "send_reminder", attempt=int((template or "1")[-1] or 1))
        res["raw"].update({"template": template, "channel": channel})
        return res

    def create_payment_link(self, invoice, customer, *, amount_minor, expire_hours, channel) -> dict:
        res = self._world_dispatch(invoice, customer, "create_payment_link")
        res["provider_ref"] = self._ref("link", invoice.rzr_invoice_id)
        res["raw"].update({"expire_hours": expire_hours, "channel": channel})
        return res

    def retry_payment(self, payment, invoice, customer, *, attempt: int) -> dict:
        # Retry semantics are a pure function of persisted case data: the cause
        # is derived from the payment's own error code. No agent-specific
        # steering is possible. (Mapping lives beside the policy signal sets so
        # this provider never depends on the diagnosis package.)
        from app.policy.rules import SIGNAL_FUNDS_CODES, SIGNAL_TECH_CODES

        code = (payment.error_code or "").upper()
        if code in SIGNAL_TECH_CODES:
            cause = C.CAUSE_TECHNICAL
        elif code in SIGNAL_FUNDS_CODES:
            cause = C.CAUSE_INSUFFICIENT_FUNDS
        else:
            cause = C.CAUSE_UNKNOWN
        rule = _RETRY_OK.get(cause, lambda a: False)
        if rule(attempt):
            pending = self._pending(invoice, "invoice.paid", 0.0)
            return {
                "ok": True,
                "provider_ref": self._ref("retry", payment.rzr_payment_id, str(attempt)),
                "raw": {"attempt": attempt, "cause": cause},
                "pending_payment": pending,
                "failure_code": "",
            }
        return {
            "ok": False,
            "provider_ref": self._ref("retry", payment.rzr_payment_id, str(attempt)),
            "raw": {"attempt": attempt, "cause": cause},
            "pending_payment": None,
            "failure_code": payment.error_code,
        }

    def ptp_followup(self, invoice, customer, *, followup_number: int, channel: str) -> dict:
        res = self._world_dispatch(invoice, customer, "ptp_followup", attempt=followup_number)
        res["raw"].update({"followup_number": followup_number, "channel": channel})
        return res

    # ------------------------------------------------- unstructured-context ops
    def resend_corrected_invoice(self, invoice, customer, *, correction_note: str, channel: str) -> dict:
        res = self._world_dispatch(invoice, customer, "resend_corrected_invoice")
        res["raw"].update({"correction_note": correction_note[:200], "channel": channel})
        return res

    def request_ap_update(self, invoice, customer, *, followup_number: int, channel: str) -> dict:
        res = self._world_dispatch(invoice, customer, "request_ap_update", attempt=followup_number)
        res["raw"].update({"followup_number": followup_number, "channel": channel})
        return res

    def confirm_ptp(self, invoice, customer, *, promised_date_iso: str, channel: str) -> dict:
        """Confirm the promise; in the ptp world the counterparty pays ON the
        promised date (offset = hours until that date, deterministic)."""
        res = self._world_dispatch(invoice, customer, "confirm_ptp")
        if res["pending_payment"] is None:
            res["raw"].update({"channel": channel, "promised_date_iso": promised_date_iso})
            return res
        try:
            from app.clock import now

            promised = datetime.fromisoformat(promised_date_iso) if promised_date_iso else None
            if promised is not None:
                offset = max(2.0, (promised - now()).total_seconds() / 3600.0 + 2.0)
                res["pending_payment"] = self._pending(invoice, "invoice.paid", offset)
        except ValueError:
            pass  # malformed date -> keep the default deterministic window
        res["raw"].update({"channel": channel, "promised_date_iso": promised_date_iso})
        return res
