"""Event ingestion: normalization + idempotent application to entities.

All money/revenue signals enter the system as canonical events:
  payment.failed        a payment attempt failed (creates/refreshes a case)
  payment.authorized / payment.captured / invoice.paid / invoice.partially_paid
  payment_link.paid / order.paid
                        money received -> credit the open case
Dedup happens on risk_events.event_id (unique), so duplicate webhook delivery,
replayed events, or crash-and-replay can never double-apply anything.
"""
from __future__ import annotations

from datetime import datetime
from typing import Iterable

from sqlalchemy import select
from sqlalchemy.orm import Session

from app import constants as C
from app.audit import audit_event
from app.clock import now
from app.models import Invoice, Payment, RiskCase, RiskEvent

MONEY_EVENTS = {
    "payment.authorized",
    "payment.captured",
    "payment_link.paid",
    "invoice.paid",
    "invoice.partially_paid",
    "order.paid",
}
FAILURE_EVENTS = {"payment.failed"}


def is_money_event(event_type: str) -> bool:
    return event_type in MONEY_EVENTS


def is_failure_event(event_type: str) -> bool:
    return event_type in FAILURE_EVENTS


def parse_iso(value) -> datetime:
    if value is None:
        return now()
    if isinstance(value, datetime):
        return value
    return datetime.fromisoformat(str(value))


def store_event(db: Session, event: dict) -> tuple[bool, RiskEvent | None]:
    """Store an event iff its event_id is new. Returns (is_new, event_row)."""
    event_id = event.get("event_id") or event.get("id")
    if not event_id:
        raise ValueError("event requires event_id")
    existing = db.scalar(select(RiskEvent).where(RiskEvent.event_id == event_id))
    if existing is not None:
        audit_event(
            db,
            actor=C.ACTOR_SYSTEM,
            action="event_duplicate_ignored",
            payload={"event_id": event_id, "event_type": event.get("event_type", "")},
        )
        return False, existing

    row = RiskEvent(
        event_id=event_id,
        event_type=event.get("event_type", ""),
        entity_type=event.get("entity_type", ""),
        entity_id=event.get("entity_id", ""),
        payload=event,
        verified=bool(event.get("verified", True)),
        source=event.get("source", "sim"),
        received_at=parse_iso(event.get("received_at")),
    )
    db.add(row)
    db.flush()
    return True, row


def _resolve_invoice(db: Session, event: dict) -> Invoice | None:
    rzr_id = (
        event.get("rzr_invoice_id")
        or (event.get("invoice") or {}).get("rzr_invoice_id")
        or (event.get("payload") or {}).get("invoice_rzr_id")
    )
    if not rzr_id:
        return None
    return db.scalar(select(Invoice).where(Invoice.rzr_invoice_id == rzr_id))


def apply_event_to_entities(db: Session, event: dict) -> list[RiskCase]:
    """Update Invoice/Payment rows from an event.

    Returns the list of open cases that received money and were credited.
    """
    etype = event.get("event_type", "")
    invoice = _resolve_invoice(db, event)
    touched: list[RiskCase] = []
    if invoice is None:
        audit_event(db, actor=C.ACTOR_SYSTEM, action="event_unresolved_invoice", payload={"event": etype})
        return touched

    amount = int(event.get("amount_minor") or 0)

    if is_failure_event(etype):
        payment_id = event.get("rzr_payment_id") or event.get("id")
        existing_p = db.scalar(select(Payment).where(Payment.rzr_payment_id == payment_id)) if payment_id else None
        if existing_p is None:
            db.add(
                Payment(
                    rzr_payment_id=payment_id or f"pay_{event.get('event_id')}",
                    invoice_id=invoice.id,
                    customer_id=invoice.customer_id,
                    amount_minor=amount or invoice.amount_minor,
                    method=event.get("method", "card"),
                    status="failed",
                    error_code=event.get("error_code", ""),
                    error_description=event.get("error_description", ""),
                    attempted_at=parse_iso(event.get("attempted_at")),
                )
            )
            db.flush()

    elif is_money_event(etype):
        payment_id = event.get("rzr_payment_id") or f"pay_{event.get('event_id')}"
        if db.scalar(select(Payment).where(Payment.rzr_payment_id == payment_id)) is None:
            db.add(
                Payment(
                    rzr_payment_id=payment_id,
                    invoice_id=invoice.id,
                    customer_id=invoice.customer_id,
                    amount_minor=amount,
                    method=event.get("method", "card"),
                    status="captured",
                    attempted_at=parse_iso(event.get("received_at")),
                )
            )
        db.flush()

        # Money received: credit any EXISTING open case (a case from an earlier
        # batch), which is also the single place invoice.paid_amount_minor moves.
        from app.detection import credit_received, find_case_for_invoice

        case = find_case_for_invoice(db, invoice.id)
        if case is not None and case.state not in C.TERMINAL_STATES:
            credited = credit_received(db, case, amount, source=f"event:{etype}")
            if credited > 0:
                touched.append(case)
        else:
            # No open case (self-heal, or a case created later in this batch):
            # record the payment on the invoice only.
            creditable = min(amount, max(0, invoice.outstanding_minor))
            invoice.paid_amount_minor += creditable
            if invoice.is_paid:
                invoice.status = "paid"
            elif invoice.paid_amount_minor > 0:
                invoice.status = "partially_paid"
            db.flush()
    return touched
