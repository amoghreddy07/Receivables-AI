"""Deterministic rules classifier — always available, no LLM required.

Maps the persisted case context (payment error codes + invoice state +
promises + compliance context) to a root cause. The signal vocabulary is
shared with the fraud rules in app.policy.rules so both layers can never
disagree about what a code means (docs/SIGNAL_MAP.md).

ABSTENTION CONTRACT (why rules-only cannot read unstructured context):

  When a case carries substantive inbound unstructured context (customer
  emails / AP communication / support threads) but NO hard structured payment
  signal, this classifier returns CAUSE_INSUFFICIENT_CONTEXT instead of
  guessing a cause from invoice state alone. A plain "overdue -> dunning"
  inference would be wrong for, e.g., a customer whose GST details block
  payment or whose AP queue is about to release funds. Free text is only
  interpretable by the LLM diagnoser; the policy engine escalates abstained
  cases in rules-only mode (no guesses, no false dunning).

  Hard structured signals (recognized Razorpay error codes, a missed PTP
  promise, compliance flags) are ALWAYS classified deterministically — the
  rules never abstain where persisted facts already decide.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from sqlalchemy import select
from sqlalchemy.orm import Session

from app import constants as C
from app.diagnosis.rules_map import classify_error_code, infer_cause_from_state
from app.models import CaseMessage, Invoice, Payment, PTPPromise

# Minimum confidence for an error-code classification to count as a HARD
# structured signal (below this the rules abstain when context exists).
_STRUCTURED_SIGNAL_CONF = 0.85


@dataclass
class RuleDiagnosis:
    cause: str
    confidence: float
    evidence: list[dict] = field(default_factory=list)
    path: str = C.DIAG_RULES
    proposed_playbook: str = ""

    def as_dict(self) -> dict:
        return {
            "cause": self.cause,
            "confidence": round(self.confidence, 3),
            "evidence": self.evidence,
            "path": self.path,
            "proposed_playbook": self.proposed_playbook,
        }


def substantive_messages(db: Session, invoice_id: int) -> list[CaseMessage]:
    """Inbound free-text messages substantial enough to carry meaning.

    A threshold on content length keeps trivial auto-acknowledgements
    ("thanks", "ok") from blocking the deterministic path.
    """
    msgs = db.scalars(
        select(CaseMessage)
        .where(
            CaseMessage.invoice_id == invoice_id,
            CaseMessage.direction == "in",
        )
        .order_by(CaseMessage.received_at.asc())
    ).all()
    return [m for m in msgs if len((m.content or "").strip()) >= 60]


def _abstain(evidence: list[dict]) -> RuleDiagnosis:
    evidence = evidence + [
        {"rule": "unstructured_context_present", "flag": C.DIAG_FLAG_ABSTAINED},
        {"rule": "requires_llm_interpretation", "reason": "deterministic rules cannot read free text"},
    ]
    return RuleDiagnosis(
        cause=C.CAUSE_INSUFFICIENT_CONTEXT,
        confidence=0.35,
        evidence=evidence,
        proposed_playbook="",  # never propose an action on an abstention
    )


def classify(db: Session, case) -> RuleDiagnosis:
    invoice = db.get(Invoice, case.invoice_id)
    payments = db.scalars(
        select(Payment)
        .where(Payment.invoice_id == invoice.id)
        .order_by(Payment.attempted_at.desc())
        .limit(8)
    ).all()
    promises = db.scalars(select(PTPPromise).where(PTPPromise.case_id == case.id)).all()
    evidence: list[dict] = []

    # Compliance context overrides payment-level cause for diagnosis purposes:
    # those cases will be hard-stopped by the policy engine anyway, but we want
    # the cause label to be truthful.
    from app.clock import now
    from app.policy.rules import compliance_hits

    comp = compliance_hits(db, case)
    if comp:
        # The cause label for compliance-blocked cases is the invoice being
        # overdue; the policy engine will hard-stop them regardless.
        return RuleDiagnosis(
            cause=C.CAUSE_OVERDUE,
            confidence=1.0,
            evidence=[{"rule": "compliance_context", "code": comp[0].code, "message": comp[0].message}],
        )

    # 1) PTP promise present & date passed & unpaid -> PTP_MISSED (deterministic:
    #    a persisted promise is a structured fact, not free text)
    active_promise = next((p for p in promises if p.status == "active"), None)
    if active_promise and active_promise.promised_date < now() and not invoice.is_paid:
        evidence.append(
            {
                "rule": "promise_past_due",
                "promised_date": active_promise.promised_date.isoformat(),
            }
        )
        return RuleDiagnosis(cause=C.CAUSE_PTP_MISSED, confidence=0.95, evidence=evidence)

    # 2) Most recent failed payment -> classify from error signal. Recognized
    #    codes are hard structured evidence (technical / funds / auth /
    #    expired / fraud) and always win over free text.
    failed = [p for p in payments if p.status == "failed"]
    if failed:
        latest = failed[0]
        cause, conf, ev = classify_error_code(latest.error_code, latest.error_description)
        evidence.append(
            {
                "rule": "payment_error",
                "payment_id": latest.rzr_payment_id,
                "error_code": latest.error_code,
                "matches": ev,
            }
        )
        # multiple consecutive failures of the same class strengthen confidence
        same = [p for p in failed if (p.error_code or "").upper() == (latest.error_code or "").upper()]
        if len(same) >= 2:
            conf = min(1.0, conf + 0.1)
            evidence.append({"rule": "repeated_same_error", "count": len(same)})
        if conf >= _STRUCTURED_SIGNAL_CONF:
            return RuleDiagnosis(cause=cause, confidence=conf, evidence=evidence)
        # low-confidence / unrecognized code: the failure alone does not decide.
        # Fall through — free text may explain what actually happened.

    # 3) Substantive unstructured context + no hard signal => ABSTAIN. The
    #    invoice state (overdue/partial) would be a guess here: the customer's
    #    email may say the money is blocked on docs, in AP approval, promised
    #    for a specific date, or disputed — none of which free text rules can
    #    read. Escalate (rules-only) or let the LLM interpret (AI mode).
    msgs = substantive_messages(db, invoice.id)
    if msgs:
        evidence.append(
            {
                "rule": "invoice_state_unreliable_with_unstructured_context",
                "days_overdue": case.days_overdue,
                "message_count": len(msgs),
            }
        )
        return _abstain(evidence)

    # 4) Invoice state (overdue / partial balance) — only when no unstructured
    #    context exists to contradict it.
    cause, conf, ev = infer_cause_from_state(invoice, case)
    evidence.append({"rule": "invoice_state", "days_overdue": case.days_overdue, "paid": invoice.paid_amount_minor})
    return RuleDiagnosis(cause=cause, confidence=conf, evidence=evidence)


def rules_proposed_playbook(db: Session, case) -> str:
    from app.policy.playbooks import CAUSE_TO_PLAYBOOK

    diag = classify(db, case)
    return CAUSE_TO_PLAYBOOK.get(diag.cause, "")
