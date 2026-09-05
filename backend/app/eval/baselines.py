"""Coded baseline: NaiveRetryBaseline.

A policy-less agent: when a payment failed it immediately retries once (no
diagnosis, no cause-awareness); when an invoice is overdue it fires one
reminder — regardless of compliance flags, fraud signals, time of day, or
promises. It never escalates and never respects stopping rules.

It runs through the SAME agent loop mechanics (detection, execution, sandbox,
audit) with a `plan_fn` that bypasses the policy engine. The evaluator then
audits its actions against the real policy rules (un-enforced) so the report
shows measurable violations — this is the contrast ReceivablesAI is measured
against.
"""
from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from app import constants as C
from app.models import Payment
from app.policy.engine import ActionPlan


def naive_plan(db: Session, case, now) -> ActionPlan:
    """Baseline decision: retry failed payments once; remind overdue once."""
    failed = db.scalar(
        select(Payment)
        .where(Payment.invoice_id == case.invoice_id, Payment.status == "failed")
        .limit(1)
    )
    if failed is not None:
        return ActionPlan(
            verdict=C.VERDICT_APPROVED,
            playbook=C.PB_SMART_RETRY,
            action=C.ACT_RETRY,
            params={"attempt": 1},
            reasons=[],
        )
    return ActionPlan(
        verdict=C.VERDICT_APPROVED,
        playbook=C.PB_DUNNING,
        action=C.ACT_SEND_REMINDER,
        params={"reminder_number": 1, "template": "dunning_1", "channel": "email"},
        reasons=[],
    )
