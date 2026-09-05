"""Builds a redacted, read-only snapshot of a case for the diagnoser.

PII minimization: contact numbers are truncated, emails are masked, and
payment card data never enters the snapshot. Free-text messages (customer
emails, support conversations, AP communication) are included so the LLM can
extract meaning the deterministic rules cannot; email addresses, phone numbers
and long digit runs are scrubbed from them. The LLM sees exactly this dict —
nothing more, nothing less.
"""
from __future__ import annotations

import re

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import CaseMessage, ComplianceFlag, Customer, Diagnosis, Escalation, Invoice, Payment, PTPPromise


_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
_PHONE_RE = re.compile(r"\+?\d[\d\s.-]{8,}\d")


def _mask_email(email: str) -> str:
    if "@" not in email:
        return email
    local, _, domain = email.partition("@")
    return f"{local[:2]}***@{domain}"


def _mask_phone(phone: str) -> str:
    return phone[:3] + "****" + phone[-2:] if len(phone) >= 7 else "****"


def _redact_text(text: str) -> str:
    """Scrub contact PII out of free text while keeping the substance."""
    text = _EMAIL_RE.sub("[email]", text)
    text = _PHONE_RE.sub("[phone]", text)
    return text[:1200]

def build_snapshot(db: Session, case) -> dict:
    invoice = db.get(Invoice, case.invoice_id)
    customer = db.get(Customer, case.customer_id)
    payments = db.scalars(
        select(Payment)
        .where(Payment.invoice_id == invoice.id)
        .order_by(Payment.attempted_at.desc())
        .limit(6)
    ).all()
    flags = db.scalars(
        select(ComplianceFlag).where(ComplianceFlag.customer_id == customer.id)
    ).all()
    promises = db.scalars(select(PTPPromise).where(PTPPromise.case_id == case.id)).all()
    messages = db.scalars(
        select(CaseMessage)
        .where(CaseMessage.invoice_id == invoice.id)
        .order_by(CaseMessage.received_at.asc())
        .limit(8)
    ).all()

    from app.clock import now

    return {
        "as_of": now().isoformat(),
        "case_id": case.case_key,
        "customer": {
            "org_name": customer.org_name,
            "contact": _mask_email(customer.email),
            "phone": _mask_phone(customer.phone),
            "behavior_profile": customer.behavior_profile,  # deterministic sandbox seed
            "compliance_flags": [f.flag_type for f in flags],
        },
        "invoice": {
            "rzr_invoice_id": invoice.rzr_invoice_id,
            "amount_inr": round(invoice.amount_minor / 100, 2),
            "paid_inr": round(invoice.paid_amount_minor / 100, 2),
            "outstanding_inr": round(invoice.outstanding_minor / 100, 2),
            "due_date": invoice.due_date.isoformat(),
            "days_overdue": max(0, (case.days_overdue)),
            "status": invoice.status,
        },
        "recent_payment_attempts": [
            {
                "payment_id": p.rzr_payment_id,
                "method": p.method,
                "amount_inr": round(p.amount_minor / 100, 2),
                "status": p.status,
                "error_code": p.error_code,
                "error_description": p.error_description[:200],
                "attempted_at": p.attempted_at.isoformat(),
            }
            for p in payments
        ],
        "promises": [
            {
                "promised_date": pr.promised_date.isoformat(),
                "status": pr.status,
                "amount_inr": round(pr.amount_minor / 100, 2),
            }
            for pr in promises
        ],
        "communication": [
            {
                "direction": m.direction,
                "channel": m.channel,
                "received_at": m.received_at.isoformat(),
                "content": _redact_text(m.content),
            }
            for m in messages
        ],
        "case_state": case.state,
        "recovered_inr": round(case.recovered_amount_minor / 100, 2),
    }
