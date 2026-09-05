"""Deterministic evaluation corpus (~63 B2B receivables cases).

Every case carries a ground truth label used ONLY for scoring (never for
steering the agent — the sandbox responds purely to persisted case data:
customer.behavior_profile, payment error codes, dates, scenario seeds).

Scenario mix covers: normal recoverable invoices, repeated failures,
high-value gate, compliance blocks (DNC/bankruptcy/120-vs-121 boundary),
fraud hard-stops, self-healing/NO_ACTION negatives, PTP kept/broken,
partial payments, an after-hours deferral case, AND a set of UNSTRUCTURED
cases whose answer lives in customer emails/AP communication:

  documentation blocker (GST/PO wrong; payer willing)   -> documentation_fix
  AP approval queue                                     -> ap_coordination
  explicit promise on a future date                     -> promise_accept
  invoice dispute / refusal                             -> escalate (human)
  DNC / bankruptcy / fraud requested via email          -> compliance/fraud stop

The unstructured cases carry `messages` (the real thread text). The correct
intervention is only discoverable by reading that text, which the deterministic
rules classifier cannot do (it abstains — see diagnosis/rules_classifier.py).
Sandbox outcome for these scenarios is gated on the ACTUAL intervention
matching the situation (correct action settles; wrong actions do not), so the
evaluation measures decision quality, not profile matching.

`build_corpus(seed)` is deterministic for a given seed (reproducibility test).
"""
from __future__ import annotations

import random
from datetime import datetime, timedelta

from app import constants as C

T0 = datetime(2026, 1, 5, 10, 0, 0)  # Monday 10:00 (contact hours)

_ORGS = [
    "Agarwal Steel Works", "Mehta Textiles", "Kulkarni Pharma", "Choudhary Auto Parts",
    "Rao Infotech", "Sharma Distributors", "Patel Agri Exports", "Nair Logistics",
    "Iyer Consulting", "Bose Electronics", "Deshmukh Constructions", "Reddy Foods",
    "Joshi Packaging", "Menon Hospitality", "Saxena Chemicals",
]


def _money(inr: int) -> int:
    return inr * 100  # paise


def build_corpus(seed: int = 1) -> list[dict]:
    rng = random.Random(seed)
    cases: list[dict] = []
    n = 0

    def add(
        scenario: str,
        *,
        profile: str,
        amount_inr: int,
        flags: list[str] | None = None,
        due_days_ago: int = 0,
        attempts: list[tuple[str, int, str, str]] | None = None,  # (code, days_ago, desc, method)
        paid_inr: int = 0,
        promise_days: int | None = None,          # promise date offset from t0 (negative = already missed)
        future_events: list[tuple[float, str, int]] | None = None,  # (offset_hours, event_type, amount_inr)
        messages: list[dict] | None = None,       # unstructured comms: {content, channel, days_ago}
        world_offset_hours: float | None = None,  # scenario-world settlement offset seed
        gt_playbook: str,
        expected_inr: int = 0,
        should_escalate: bool = False,
        start_hour: int = 10,
        detection: str = "webhook",               # webhook | overdue_tick
    ) -> None:
        nonlocal n
        n += 1
        org = rng.choice(_ORGS)
        amount = _money(amount_inr)
        paid = _money(paid_inr)
        inv_id = f"INV-2026-{1000 + n}"
        cases.append(
            {
                "id": f"c{n:03d}",
                "scenario": scenario,
                "org": org,
                "invoice_id": inv_id,
                "amount_minor": amount,
                "paid_minor": paid,
                "currency": "INR",
                "profile": profile,
                "flags": flags or [],
                "due_days_ago": due_days_ago,
                "attempts": attempts or [],  # historical failed attempts (days_ago >= 0)
                "promise_days": promise_days,
                "future_events": [
                    {"offset_hours": off, "event_type": et, "amount_minor": _money(am), "rzr_invoice_id": inv_id}
                    for off, et, am in (future_events or [])
                ],
                "messages": [
                    {
                        "direction": "in",
                        "channel": m.get("channel", "email"),
                        "content": m["content"].replace("$INV", inv_id),
                        "days_ago": float(m.get("days_ago", 2.0)),
                    }
                    for m in (messages or [])
                ],
                "world_offset_hours": world_offset_hours,
                "start_hour": start_hour,
                "detection": detection,
                "ground_truth": {
                    "playbook": gt_playbook,
                    "expected_recovery_minor": _money(expected_inr),
                    "should_escalate": should_escalate,
                },
            }
        )

    # ---- failed-payment recoveries (webhook detection) --------------------- #
    for i, amt in enumerate([180000, 64000, 120000, 96000, 250000, 45000]):
        add(
            f"technical_failure_{i+1}", profile="default", amount_inr=amt,
            attempts=[("BANK_TECHNICAL_ISSUE", 0, "Bank technical issue at transaction time", "card")],
            gt_playbook=C.PB_SMART_RETRY, expected_inr=amt,
        )
    for i, amt in enumerate([210000, 88000, 145000, 320000, 52000, 168000]):
        add(
            f"insufficient_funds_{i+1}", profile="default", amount_inr=amt,
            attempts=[("INSUFFICIENT_FUNDS", 0, "Your account has insufficient balance", "upi")],
            gt_playbook=C.PB_SMART_RETRY, expected_inr=amt,
        )
    for i, amt in enumerate([275000, 92000, 154000, 61000]):
        add(
            f"auth_failure_{i+1}", profile="pays_after_link", amount_inr=amt,
            attempts=[("AUTH_FAILED", 0, "2-step authentication failed for the payment", "card")],
            gt_playbook=C.PB_PAYMENT_LINK, expected_inr=amt,
        )
    for i, amt in enumerate([130000, 77000]):
        add(
            f"instrument_expired_{i+1}", profile="pays_after_link", amount_inr=amt,
            attempts=[("CARD_EXPIRED", 0, "The card has expired", "card")],
            gt_playbook=C.PB_PAYMENT_LINK, expected_inr=amt,
        )

    # ---- dunning ladder (overdue detection) --------------------------------- #
    for i, (amt, days) in enumerate([(98000, 30), (156000, 45), (84000, 60), (262000, 30), (72000, 15), (188000, 75)]):
        add(
            f"overdue_pays_after_reminder_{i+1}", profile="pays_after_reminder", amount_inr=amt,
            due_days_ago=days, detection="overdue_tick",
            gt_playbook=C.PB_DUNNING, expected_inr=amt,
        )
    for i, (amt, days) in enumerate([(146000, 60), (93000, 90), (214000, 45)]):
        add(
            f"overdue_pays_second_reminder_{i+1}", profile="pays_after_second_reminder", amount_inr=amt,
            due_days_ago=days, detection="overdue_tick",
            gt_playbook=C.PB_DUNNING, expected_inr=amt,
        )
    for i, (amt, days) in enumerate([(118000, 90), (205000, 60), (67000, 30)]):
        add(
            f"overdue_never_pays_{i+1}", profile="never_pays", amount_inr=amt,
            due_days_ago=days, detection="overdue_tick",
            gt_playbook=C.PB_DUNNING, expected_inr=0, should_escalate=True,
        )

    # ---- partial balance ------------------------------------------------------ #
    for i, (amt, paid) in enumerate([(260000, 100000), (340000, 200000)]):
        add(
            f"partial_balance_{i+1}", profile="pays_after_reminder", amount_inr=amt, paid_inr=paid,
            due_days_ago=20, detection="overdue_tick",
            gt_playbook=C.PB_DUNNING, expected_inr=amt - paid,
        )

    # ---- compliance hard-stops ------------------------------------------------- #
    for i, amt in enumerate([96000, 240000]):
        add(
            f"dnc_overdue_{i+1}", profile="never_pays", amount_inr=amt, flags=[C.FLAG_DNC],
            due_days_ago=40, detection="overdue_tick",
            gt_playbook=C.PB_DUNNING, expected_inr=0, should_escalate=True,
        )
    add(
        "bankruptcy_flag", profile="never_pays", amount_inr=310000, flags=[C.FLAG_BANKRUPTCY],
        due_days_ago=60, detection="overdue_tick",
        gt_playbook=C.PB_DUNNING, expected_inr=0, should_escalate=True,
    )
    add(
        "overdue_121_days_a", profile="pays_after_reminder", amount_inr=178000,
        due_days_ago=121, detection="overdue_tick",
        gt_playbook=C.PB_DUNNING, expected_inr=0, should_escalate=True,
    )
    add(
        "overdue_121_days_b", profile="pays_after_reminder", amount_inr=99000,
        due_days_ago=125, detection="overdue_tick",
        gt_playbook=C.PB_DUNNING, expected_inr=0, should_escalate=True,
    )
    # exactly 120 days: NOT yet compliance-blocked -> reminder may recover
    add(
        "overdue_120_days_boundary_allowed", profile="pays_after_reminder", amount_inr=133000,
        due_days_ago=120, detection="overdue_tick",
        gt_playbook=C.PB_DUNNING, expected_inr=133000,
    )

    # ---- fraud hard-stops -------------------------------------------------------- #
    add(
        "avs_mismatch_a", profile="default", amount_inr=87000,
        attempts=[("AVS_FAILED", 0, "AVS check failed — billing address mismatch", "card")],
        gt_playbook=C.PB_ESCALATE, expected_inr=0, should_escalate=True,
    )
    add(
        "avs_mismatch_b", profile="default", amount_inr=152000,
        attempts=[("AVS_MISMATCH", 0, "AVS mismatch, transaction declined", "card")],
        gt_playbook=C.PB_ESCALATE, expected_inr=0, should_escalate=True,
    )
    add(
        "three_auth_failures", profile="default", amount_inr=196000,
        attempts=[
            ("AUTH_FAILED", 5, "authentication failed", "card"),
            ("AUTH_FAILED", 3, "authentication failed", "card"),
            ("AUTH_FAILED", 0, "authentication failed", "card"),
        ],
        gt_playbook=C.PB_ESCALATE, expected_inr=0, should_escalate=True,
    )
    add(
        "fraud_lock_suspicious", profile="default", amount_inr=64000,
        attempts=[("RISK_DECLINE", 0, "SUSPICIOUS_ACTIVITY — transaction blocked by bank risk engine", "card")],
        gt_playbook=C.PB_ESCALATE, expected_inr=0, should_escalate=True,
    )

    # ---- high-value gate ----------------------------------------------------------- #
    add(
        "high_value_overdue", profile="pays_after_reminder", amount_inr=1200000,
        due_days_ago=15, detection="overdue_tick",
        gt_playbook=C.PB_DUNNING, expected_inr=0,  # human approval required -> auto-decline in batch
    )

    # ---- promise-to-pay ------------------------------------------------------------ #
    add(
        "ptp_kept_on_time", profile="pays_after_followup", amount_inr=172000,
        due_days_ago=10, promise_days=5, detection="overdue_tick",
        future_events=[(5 * 24 + 2, "invoice.paid", 172000)],
        gt_playbook=C.PB_PTP, expected_inr=172000,
    )
    add(
        "ptp_broken", profile="never_pays", amount_inr=89000,
        due_days_ago=25, promise_days=-1,  # promise date already missed at detection
        detection="overdue_tick",
        gt_playbook=C.PB_PTP, expected_inr=0, should_escalate=True,
    )

    # ---- after-hours deferral -------------------------------------------------------- #
    add(
        "technical_after_hours", profile="default", amount_inr=141000,
        attempts=[("BANK_TECHNICAL_ISSUE", 0, "Bank technical issue at 21:47", "card")],
        gt_playbook=C.PB_SMART_RETRY, expected_inr=141000, start_hour=22,
    )

    # ---- NO_ACTION negatives ----------------------------------------------------------- #
    # self-healed: failure + success arrive together; invoice paid before any action
    add(
        "self_heal_a", profile="default", amount_inr=74000,
        attempts=[("TECHNICAL_ISSUE", 0, "transient timeout", "card")],
        future_events=[(0, "invoice.paid", 74000)],
        gt_playbook="NO_ACTION", expected_inr=0,
    )
    add(
        "self_heal_b", profile="default", amount_inr=121000,
        attempts=[("BANK_TECHNICAL_ISSUE", 0, "transient failure", "card")],
        future_events=[(0, "invoice.paid", 121000)],
        gt_playbook="NO_ACTION", expected_inr=0,
    )
    # low-risk overdue invoices far below the action threshold (risk-gate observe-only)
    for i, (amt, days) in enumerate([(800, 2), (400, 3)]):
        add(
            f"low_risk_watch_{i+1}", profile="never_pays", amount_inr=amt,
            due_days_ago=days, detection="overdue_tick",
            gt_playbook="NO_ACTION", expected_inr=0,
        )

    # ---------------------------------------------------------------------- #
    # UNSTRUCTURED-context cases. The correct intervention is stated in the
    # customer's own message; deterministic rules cannot read it (they abstain),
    # the LLM diagnoser can. Ground truth here = what the message literally says
    # the payer will do, so it is defensible, not engineered to flatter the AI.
    # ---------------------------------------------------------------------- #
    # A. willing payer blocked on wrong invoice documentation -> correction
    add(
        "docs_issue_gst", profile="docs_issue", amount_inr=230000,
        due_days_ago=32, detection="overdue_tick",
        messages=[
            {"content": "Hi, our AP team has approved invoice $INV but the GST details printed on it are incorrect. Please resend a corrected copy and we will release payment within 2 working days of receiving it.", "days_ago": 3},
        ],
        gt_playbook=C.PB_DOCUMENTATION_FIX, expected_inr=230000,
    )
    add(
        "docs_issue_po_reference", profile="docs_issue", amount_inr=415000,
        due_days_ago=26, detection="overdue_tick",
        messages=[
            {"content": "Dear team, the PO reference printed on invoice $INV is wrong, so our system will not release the payment. Please resend the invoice with corrections so our finance team can process it.", "days_ago": 5},
        ],
        gt_playbook=C.PB_DOCUMENTATION_FIX, expected_inr=415000,
    )
    # A2. same blocker, but the payer settles only LATE (beyond the 45-day
    #     eval horizon) even after a correct resend -> correct action, honest
    #     zero recovery inside the horizon (difficult case for BOTH modes).
    add(
        "docs_issue_latepay", profile="docs_issue_latepay", amount_inr=198000,
        due_days_ago=30, detection="overdue_tick", world_offset_hours=1400.0,  # ~58 days
        messages=[
            {"content": "Please resend the corrected invoice for $INV after fixing the GSTIN. Our finance team will release payment 45 days after the correction is received.", "days_ago": 4},
        ],
        gt_playbook=C.PB_DOCUMENTATION_FIX, expected_inr=198000,
    )
    # B. payment queued in the customer's AP -> AP coordination, not escalation
    add(
        "ap_queue_week", profile="ap_queue", amount_inr=188000,
        due_days_ago=38, detection="overdue_tick",
        messages=[
            {"content": "We have received invoice $INV. It is with our accounts payable team for approval and we expect to release payment next week. There is no need to escalate — everything is on track.", "days_ago": 2},
        ],
        gt_playbook=C.PB_AP_COORDINATION, expected_inr=188000,
    )
    add(
        "ap_queue_ten_days", profile="ap_queue", amount_inr=275000,
        due_days_ago=41, detection="overdue_tick",
        messages=[
            {"content": "Payment for invoice $INV is queued in our finance team and will be processed within the next 10 days. Please hold off on any collection reminders.", "days_ago": 1},
        ],
        gt_playbook=C.PB_AP_COORDINATION, expected_inr=275000,
    )
    # C. explicit promise on a concrete future date -> confirm & wait
    add(
        "ptp_offer_explicit_date", profile="ptp_offer_will_pay", amount_inr=332000,
        due_days_ago=27, detection="overdue_tick",
        messages=[
            {"content": "Hi, we will pay invoice $INV on 20 January 2026 via NEFT. Please confirm receipt of this message and do not send any reminders before that date.", "days_ago": 2},
        ],
        gt_playbook=C.PB_PROMISE_ACCEPT, expected_inr=332000,
    )
    add(
        "ptp_offer_transfer", profile="ptp_offer_will_pay", amount_inr=152000,
        due_days_ago=33, detection="overdue_tick",
        messages=[
            {"content": "Please note we will transfer the amount for invoice $INV on or before 12 January 2026. Kindly confirm and hold further follow-ups until then.", "days_ago": 3},
        ],
        gt_playbook=C.PB_PROMISE_ACCEPT, expected_inr=152000,
    )
    # D. invoice dispute / refusal -> human, never automated collection
    add(
        "dispute_services", profile="never_pays", amount_inr=249000,
        due_days_ago=29, detection="overdue_tick",
        messages=[
            {"content": "We did not receive the services billed on invoice $INV and we dispute the charges. We will not pay until this is resolved by your support team.", "days_ago": 6},
        ],
        gt_playbook=C.PB_ESCALATE, expected_inr=0, should_escalate=True,
    )
    add(
        "dispute_duplicate_charge", profile="never_pays", amount_inr=301000,
        due_days_ago=24, detection="overdue_tick",
        messages=[
            {"content": "The amount billed on invoice $INV is incorrect — we were charged twice for line item 7. Please investigate before we pay anything.", "days_ago": 4},
        ],
        gt_playbook=C.PB_ESCALATE, expected_inr=0, should_escalate=True,
    )
    add(
        "refusal_vendor_switch", profile="never_pays", amount_inr=167000,
        due_days_ago=35, detection="overdue_tick",
        messages=[
            {"content": "Our company has switched vendors and will not be paying invoice $INV. Please close it out on your side.", "days_ago": 7},
        ],
        gt_playbook=C.PB_ESCALATE, expected_inr=0, should_escalate=True,
    )
    # E. do-not-contact requested in writing -> DNC flag (deterministic stop)
    add(
        "dnc_requested_in_email", profile="never_pays", amount_inr=123000,
        flags=[C.FLAG_DNC], due_days_ago=44, detection="overdue_tick",
        messages=[
            {"content": "Please stop contacting us about invoice $INV. Do not email or call again about it — we have formally requested removal from your billing contact lists.", "days_ago": 2},
        ],
        gt_playbook=C.PB_ESCALATE, expected_inr=0, should_escalate=True,
    )
    # G. bankruptcy notice -> compliance stop (deterministic)
    add(
        "bankruptcy_notice_email", profile="never_pays", amount_inr=460000,
        flags=[C.FLAG_BANKRUPTCY], due_days_ago=50, detection="overdue_tick",
        messages=[
            {"content": "Our company has filed for bankruptcy and is under court protection. We cannot make payments on invoice $INV at this time.", "days_ago": 1},
        ],
        gt_playbook=C.PB_ESCALATE, expected_inr=0, should_escalate=True,
    )
    # F. fraud signalled in structured payment data + corroborating email
    add(
        "fraud_email_corroboration", profile="default", amount_inr=84000,
        attempts=[("AVS_FAILED", 0, "AVS check failed — billing address mismatch", "card")],
        messages=[
            {"content": "Someone tried to use our corporate card for invoice $INV without authorization. Do not retry the card — we suspect it was compromised.", "days_ago": 0.2},
        ],
        gt_playbook=C.PB_ESCALATE, expected_inr=0, should_escalate=True,
    )

    return cases


def build_one(scenario_prefix: str, seed: int = 1) -> dict | None:
    """Helper for targeted tests: return the first case whose scenario starts
    with the prefix (or a fresh generated clone)."""
    for cc in build_corpus(seed):
        if cc["scenario"].startswith(scenario_prefix):
            return cc
    return None
