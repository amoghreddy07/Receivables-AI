"""Optional LLM diagnoser.

The LLM is an OPTIONAL enhancement. It receives one read-only, redacted case
snapshot and returns structured JSON. It has no tools, no write access, and —
critically — the diagnosis modules must never import execution/Razorpay code
(air-gap test). All failure modes (no key, timeout, HTTP error, malformed
output) degrade to `None`; the agent then uses the deterministic rules
diagnosis and flags the case DIAGNOSED_BY_RULES.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field

from app import constants as C

# Root-cause enum shared with the rules classifier (JSON schema for the model).
CAUSE_CHOICES = [
    C.CAUSE_TECHNICAL,
    C.CAUSE_INSUFFICIENT_FUNDS,
    C.CAUSE_AUTH_FAILURE,
    C.CAUSE_INSTRUMENT_EXPIRED,
    C.CAUSE_FRAUD_SUSPECTED,
    C.CAUSE_OVERDUE,
    C.CAUSE_PARTIAL_BALANCE,
    C.CAUSE_PTP_MISSED,
    # causes read from UNSTRUCTURED context (email / support / AP comms):
    C.CAUSE_DOCUMENTATION_ISSUE,
    C.CAUSE_AWAITING_AP_APPROVAL,
    C.CAUSE_PTP_OFFERED,
    C.CAUSE_DISPUTE,
    C.CAUSE_UNKNOWN,
]

PLAYBOOK_CHOICES = [
    C.PB_SMART_RETRY,
    C.PB_PAYMENT_LINK,
    C.PB_DUNNING,
    C.PB_PTP,
    C.PB_DOCUMENTATION_FIX,
    C.PB_AP_COORDINATION,
    C.PB_PROMISE_ACCEPT,
    C.PB_ESCALATE,
]

_SYSTEM_PROMPT = """You are the diagnosis engine of ReceivablesAI, a B2B receivables recovery agent.

You receive a JSON snapshot of one revenue-at-risk case (overdue B2B invoice or
failed invoice payment) that includes a `communication` transcript: real
customer emails, AP/accounts-payable messages, support conversations and
payment notes. Diagnose WHY the money has not arrived.

Read the unstructured text carefully — it usually states the real reason:
  - "GST / PO / invoice details are wrong — please resend a corrected copy, then
    we will pay"  -> documentation_issue (payment is WILLING but BLOCKED)
  - "the invoice is with our AP / accounts payable / finance team and will be
    approved / released / processed [date]" -> awaiting_ap_approval
  - an explicit promise "we will pay / transfer / release payment by <date>"
    -> ptp_offered (report the date in `promised_date`)
  - "we dispute / did not receive / amount is wrong / will not pay"
    -> invoice_dispute (NEVER propose collection actions)

Rules:
- Base the diagnosis ONLY on evidence present in the snapshot (payment attempts,
  invoice state, promises AND communication).
- Choose exactly one root cause from the allowed enum.
- The deterministic rules classifier abstains on these threads — YOU are the
  reader of the free text. Quote the specific sentence you relied on in
  `evidence`; if the thread is genuinely ambiguous, choose `unknown` with low
  confidence instead of guessing.
- Do not propose refunds, discounts, legal threats or anything outside the
  playbook enum. You only PROPOSE — a separate policy engine decides.
- Set confidence honestly (0.0-1.0). Low or contradictory evidence -> low confidence.
- recommended_playbook must be consistent with the cause:
    technical_failure / insufficient_funds -> smart_retry
    auth_failure / instrument_expired      -> payment_link_resend
    overdue / partial_balance              -> dunning_reminder
    ptp_missed                             -> promise_to_pay
    documentation_issue                    -> documentation_fix
    awaiting_ap_approval                   -> ap_coordination
    ptp_offered                            -> promise_accept (include promised_date)
    invoice_dispute / fraud_suspected / unknown -> escalate_human
- Return STRICT JSON only, matching this schema:
{"root_cause": "...", "confidence": 0.0, "evidence": ["..."], "recommended_playbook": "...", "promised_date": "YYYY-MM-DD or null", "rationale": "..."}"""


@dataclass
class LLMDiagnosis:
    cause: str
    confidence: float
    evidence: list[str] = field(default_factory=list)
    rationale: str = ""
    proposed_playbook: str = ""
    promised_date_iso: str = ""  # extracted by the model for ptp_offered
    model: str = ""

    def as_dict(self) -> dict:
        return {
            "cause": self.cause,
            "confidence": round(self.confidence, 3),
            "evidence": self.evidence,
            "rationale": self.rationale,
            "proposed_playbook": self.proposed_playbook,
            "promised_date_iso": self.promised_date_iso,
            "model": self.model,
        }


def _parse_promise_date(raw) -> str:
    """Normalize any ISO-ish date the model returns to YYYY-MM-DD ("" if none)."""
    if not raw:
        return ""
    s = str(raw).strip()[:10]  # tolerate datetime strings
    from datetime import datetime

    try:
        return datetime.strptime(s, "%Y-%m-%d").date().isoformat()
    except ValueError:
        return ""


def _validate(raw: dict) -> LLMDiagnosis | None:
    cause = raw.get("root_cause")
    if cause not in CAUSE_CHOICES:
        return None
    conf = float(raw.get("confidence", 0.0))
    conf = max(0.0, min(1.0, conf))
    proposed = raw.get("recommended_playbook")
    if proposed not in PLAYBOOK_CHOICES:
        proposed = ""
    return LLMDiagnosis(
        cause=cause,
        confidence=conf,
        evidence=[str(e) for e in (raw.get("evidence") or [])][:8],
        rationale=str(raw.get("rationale", ""))[:500],
        proposed_playbook=proposed,
        promised_date_iso=_parse_promise_date(raw.get("promised_date")) if cause == C.CAUSE_PTP_OFFERED else "",
    )


def diagnose_with_llm(snapshot: dict, *, api_key: str = "", model: str = "", timeout: float = 8.0) -> LLMDiagnosis | None:
    """One-shot structured diagnosis. Never raises — returns None on any failure."""
    if not api_key:
        return None
    try:
        # Lazy import: the openai package is optional and must not be required
        # for the application to import or run.
        from openai import OpenAI

        client = OpenAI(api_key=api_key, timeout=timeout, max_retries=1)
        resp = client.chat.completions.create(
            model=model or "gpt-4o-mini",
            temperature=0,
            response_format={"type": "json_object"},
            messages=[
                {"role": "system", "content": _SYSTEM_PROMPT},
                {"role": "user", "content": json.dumps(snapshot, sort_keys=True)},
            ],
            timeout=timeout,
        )
        content = resp.choices[0].message.content or ""
        return _validate(json.loads(content))
    except Exception:
        # Key missing/invalid, network failure, timeout, rate limit, malformed
        # JSON — every failure mode degrades to the deterministic rules path.
        return None
