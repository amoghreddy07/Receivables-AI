"""Exactly-once execution of approved actions.

Only actions enumerated in the playbook registry can ever reach a provider.
Each execution carries a deterministic idempotency key; if the same key already
exists (duplicate delivery, crash-and-resume), the executor returns the
existing intervention WITHOUT calling the provider again. Provider errors are
captured on the intervention; nothing here ever retries automatically (the
agent loop owns scheduling) and no refund/discount/amount-edit action exists.
"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from app import constants as C
from app.audit import audit_event
from app.clock import now
from app.models import Customer, Intervention, Invoice, Payment, PTPPromise
from app.policy.engine import ActionPlan
from app.policy.playbooks import PLAYBOOKS


def build_idempotency_key(case, plan: ActionPlan) -> str:
    params = plan.params or {}
    marker = params.get("attempt") or params.get("reminder_number") or params.get("followup_number") or "1"
    return f"{case.case_key}:{plan.action}:{marker}"


def _payload(case, plan: ActionPlan) -> dict:
    return {
        "case": case.case_key,
        "playbook": plan.playbook,
        "action": plan.action,
        "params": plan.params,
    }


def execute_action(
    db: Session,
    case,
    plan: ActionPlan,
    provider,
    *,
    mode: str,
    now_dt: datetime | None = None,
) -> Intervention | None:
    """Run an approved action exactly once. Returns the intervention."""
    now_dt = now_dt or now()
    action = plan.action

    # Whitelist enforcement at the last gate before a provider call.
    allowed = PLAYBOOKS.get(plan.playbook, {}).get("actions", [])
    if action not in allowed:
        audit_event(
            db,
            actor=C.ACTOR_POLICY,
            action="action_blocked_not_whitelisted",
            payload={"case": case.case_key, "action": action, "playbook": plan.playbook},
            case_id=case.id,
        )
        return None

    key = build_idempotency_key(case, plan)
    existing = db.scalar(select(Intervention).where(Intervention.idempotency_key == key))
    if existing is not None:
        return existing  # duplicate/crash-resume: never a second provider call

    intervention = Intervention(
        case_id=case.id,
        idempotency_key=key,
        playbook=plan.playbook,
        action=action,
        params=plan.params or {},
        status="EXECUTING",
        mode=mode,
        verdict="APPROVED",
        policy_reasons=plan.reasons,
        created_at=now_dt,
    )
    db.add(intervention)
    db.flush()

    audit_event(
        db,
        actor=C.ACTOR_POLICY,
        action="action_dispatched",
        payload=_payload(case, plan),
        case_id=case.id,
    )

    invoice = db.get(Invoice, case.invoice_id)
    customer = db.get(Customer, case.customer_id)
    amount = case.amount_at_risk_minor

    try:
        if action == C.ACT_SEND_REMINDER:
            result = provider.send_reminder(
                invoice, customer,
                template=plan.params.get("template", "dunning_1"),
                channel=plan.params.get("channel", "email"),
                amount_minor=amount,
            )
        elif action == C.ACT_CREATE_LINK:
            result = provider.create_payment_link(
                invoice, customer,
                amount_minor=amount,
                expire_hours=plan.params.get("expire_hours", 72),
                channel=plan.params.get("channel", "email"),
            )
        elif action == C.ACT_RETRY:
            payment = _latest_failed_payment(db, case.invoice_id)
            if payment is None:
                result = {"ok": False, "provider_ref": "", "raw": {}, "pending_payment": None, "failure_code": "NO_PAYMENT_TO_RETRY"}
            else:
                result = provider.retry_payment(payment, invoice, customer, attempt=int(plan.params.get("attempt", 1)))
        elif action == C.ACT_PTP_FOLLOWUP:
            result = provider.ptp_followup(
                invoice, customer,
                followup_number=int(plan.params.get("followup_number", 1)),
                channel=plan.params.get("channel", "email"),
            )
        elif action == C.ACT_RESEND_CORRECTED_INVOICE:
            result = provider.resend_corrected_invoice(
                invoice, customer,
                correction_note=plan.params.get("correction_note", ""),
                channel=plan.params.get("channel", "email"),
            )
        elif action == C.ACT_REQUEST_AP_UPDATE:
            result = provider.request_ap_update(
                invoice, customer,
                followup_number=int(plan.params.get("followup_number", 1)),
                channel=plan.params.get("channel", "email"),
            )
        elif action == C.ACT_CONFIRM_PTP:
            promised = plan.params.get("promised_date_iso", "")
            result = provider.confirm_ptp(
                invoice, customer,
                promised_date_iso=promised,
                channel=plan.params.get("channel", "email"),
            )
            # the agent accepts the promise: persist it so the case defers until
            # the promised date (audited as part of the intervention outcome)
            record_promise(db, case, promised, invoice)
        else:  # pragma: no cover — unreachable by whitelist above
            result = {"ok": False, "provider_ref": "", "raw": {}, "pending_payment": None, "failure_code": "UNKNOWN_ACTION"}
    except Exception as exc:  # provider-level hard failure
        result = {"ok": False, "provider_ref": "", "raw": {}, "pending_payment": None, "failure_code": f"PROVIDER_EXC:{type(exc).__name__}"}

    pending = result.get("pending_payment")
    if pending:
        offset = float(pending.get("offset_hours", 0))
        from datetime import timedelta

        pending["pending_at"] = (now_dt + timedelta(hours=offset)).isoformat()
        pending["event_id"] = f"sim:{intervention.idempotency_key}"
        pending["invoice_rzr_id"] = invoice.rzr_invoice_id
        pending["customer_id"] = customer.id

    intervention.status = "SUCCEEDED" if result.get("ok") else "FAILED"
    intervention.outcome = result
    intervention.executed_at = now_dt
    intervention.error = result.get("failure_code", "")

    audit_event(
        db,
        actor=C.ACTOR_SYSTEM,
        action="action_result",
        payload={
            "case": case.case_key,
            "action": action,
            "ok": result.get("ok"),
            "provider_ref": result.get("provider_ref"),
            "failure_code": result.get("failure_code"),
            "pending_payment": bool(pending),
        },
        case_id=case.id,
    )
    db.flush()
    return intervention


def _latest_failed_payment(db: Session, invoice_id: int) -> Payment | None:
    return db.scalar(
        select(Payment)
        .where(Payment.invoice_id == invoice_id, Payment.status == "failed")
        .order_by(Payment.attempted_at.desc())
        .limit(1)
    )


def record_promise(db: Session, case, promised_date_iso: str, invoice) -> PTPPromise | None:
    """Persist an accepted promise-to-pay (idempotent per case)."""
    from datetime import datetime as _dt

    if not promised_date_iso:
        return None
    try:
        promised = _dt.fromisoformat(promised_date_iso)
    except ValueError:
        return None
    existing = db.scalar(
        select(PTPPromise).where(
            PTPPromise.case_id == case.id,
            PTPPromise.status == "active",
        ).limit(1)
    )
    if existing is not None:
        return existing
    promise = PTPPromise(
        case_id=case.id,
        promised_date=promised,
        amount_minor=invoice.outstanding_minor,
        status="active",
    )
    db.add(promise)
    db.flush()
    return promise
