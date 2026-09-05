"""Detection: risk scoring + case lifecycle helpers.

The risk score is a transparent, weighted model (documented in
docs/ARCHITECTURE.md). It RANKS and QUEUES cases and decides the observe-only
threshold; it never authorizes an action by itself — the policy engine does.
"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from app import constants as C
from app.audit import audit_event
from app.clock import now
from app.models import Escalation, Invoice, Payment, RiskCase


def risk_score_for(db: Session, invoice: Invoice, payments: list[Payment]) -> float:
    """Weighted, interpretable risk in [0, 1]. Higher = more likely to leak.

    Weights (documented in docs/ARCHITECTURE.md):
      base           0.25  (an unpaid invoice is always somewhat at risk)
      amount         outstanding buckets: >=Rs50k +0.15, >=Rs10k +0.10,
                     >=Rs1k +0.05, else +0
      failed attempt any failed payment        +0.20
      error class    technical/funds nudge      +0.05 (recoverable but needs action)
      overdue        min(0.25, days/600)
    """
    score = 0.25
    outstanding = invoice.outstanding_minor
    inr = outstanding / 100

    if inr >= 50_000:
        score += 0.15
    elif inr >= 10_000:
        score += 0.10
    elif inr >= 1_000:
        score += 0.05

    failed = [p for p in payments if p.status == "failed"]
    if failed:
        score += 0.20
        # deterministic error-class nudge (signal map shared with diagnosis)
        from app.diagnosis.rules_map import classify_error_code

        cause, conf, _ = classify_error_code(failed[0].error_code, failed[0].error_description)
        if cause in (C.CAUSE_TECHNICAL, C.CAUSE_INSUFFICIENT_FUNDS, C.CAUSE_AUTH_FAILURE):
            score += 0.05 * conf

    if invoice.due_date < now():
        days = max(0, (now().date() - invoice.due_date.date()).days)
        score += min(0.25, days / 600.0)
    return round(min(1.0, score), 3)


def days_overdue_for(invoice: Invoice) -> int:
    from app.clock import now

    return max(0, (now().date() - invoice.due_date.date()).days)


def find_case_for_invoice(db: Session, invoice_id: int) -> RiskCase | None:
    return db.scalar(select(RiskCase).where(RiskCase.invoice_id == invoice_id))


def open_or_refresh_case(db: Session, invoice: Invoice) -> RiskCase | None:
    """Create a case when revenue is at risk, or refresh an existing open one.

    Returns None when the invoice is fully paid (nothing at risk).
    """
    from app.models import Customer

    if invoice.is_paid:
        return None

    customer = db.get(Customer, invoice.customer_id)
    key = f"invoice:{invoice.rzr_invoice_id}"
    case = db.scalar(select(RiskCase).where(RiskCase.case_key == key))
    payments = db.scalars(
        select(Payment).where(Payment.invoice_id == invoice.id).order_by(Payment.attempted_at.desc())
    ).all()

    days = days_overdue_for(invoice)
    amount = invoice.outstanding_minor

    if case is None:
        case = RiskCase(
            case_key=key,
            entity_type="invoice",
            entity_id=invoice.rzr_invoice_id,
            invoice_id=invoice.id,
            customer_id=customer.id,
            amount_at_risk_minor=amount,
            currency=invoice.currency,
            state=C.STATE_OPEN,
            opened_at=now(),
            days_overdue=days,
        )
        db.add(case)
        db.flush()
        audit_event(
            db,
            actor=C.ACTOR_SYSTEM,
            action="case_opened",
            payload={
                "invoice": invoice.rzr_invoice_id,
                "outstanding_minor": amount,
                "days_overdue": days,
                "payments_failed": len([p for p in payments if p.status == "failed"]),
            },
            case_id=case.id,
        )
    else:
        case.amount_at_risk_minor = amount
        case.days_overdue = days
        if case.state in (C.STATE_OPEN, C.STATE_PENDING_APPROVAL):
            case.state = C.STATE_OPEN  # any new signal wakes the case

    case.risk_score = risk_score_for(db, invoice, payments)
    return case


def credit_received(db: Session, case: RiskCase, amount_minor: int, *, source: str) -> int:
    """Apply received money to the invoice AND credit the case for what the
    agent recovered. Returns the credited amount (0 if nothing creditable).

    This is the single place invoice.paid_amount_minor is incremented, so
    crediting and invoice state can never drift apart.
    """
    from app.models import Invoice

    invoice = db.get(Invoice, case.invoice_id)
    creditable = min(amount_minor, max(0, invoice.outstanding_minor))
    if creditable <= 0:
        return 0

    invoice.paid_amount_minor += creditable
    if invoice.is_paid:
        invoice.status = "paid"
    elif invoice.paid_amount_minor > 0:
        invoice.status = "partially_paid"

    case.recovered_amount_minor += creditable
    audit_event(
        db,
        actor=C.ACTOR_SYSTEM,
        action="payment_credited",
        payload={"amount_minor": creditable, "source": source, "invoice": invoice.rzr_invoice_id},
        case_id=case.id,
    )
    db.flush()

    if invoice.is_paid:
        set_state(db, case, C.STATE_RECOVERED, reason=f"invoice {invoice.rzr_invoice_id} fully paid")
    else:
        case.amount_at_risk_minor = invoice.outstanding_minor
    return creditable


def set_state(db: Session, case: RiskCase, state: str, *, reason: str = "") -> None:
    from app.constants import allowed_transition

    if case.state == state:
        return
    if not allowed_transition(case.state, state):
        # Defensive: log but do not crash the pipeline on unexpected ordering.
        audit_event(
            db,
            actor=C.ACTOR_SYSTEM,
            action="transition_warning",
            payload={"from": case.state, "to": state, "reason": reason},
            case_id=case.id,
        )
    case.state = state
    if state in C.TERMINAL_STATES:
        case.closed_at = now()
    case.reason = reason
    audit_event(
        db,
        actor=C.ACTOR_SYSTEM,
        action="state_changed",
        payload={"from": "", "to": state, "reason": reason},
        case_id=case.id,
    )


def escalate(db: Session, case: RiskCase, reason_code: str, context: dict | None = None) -> Escalation:
    """Create an escalation + move the case to ESCALATED (terminal)."""
    esc = Escalation(case_id=case.id, reason_code=reason_code, context=context or {}, status="open")
    db.add(esc)
    set_state(db, case, C.STATE_ESCALATED, reason=reason_code)
    audit_event(
        db,
        actor=C.ACTOR_SYSTEM,
        action="escalated",
        payload={"reason_code": reason_code, "context": context or {}},
        case_id=case.id,
    )
    db.flush()
    return esc
