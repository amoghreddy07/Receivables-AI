"""Fraud handling must be deterministic and never retried by the agent.

Signals (all rule-based, no LLM): AVS mismatch, >= 3 consecutive auth failures
inside 7 days, FRAUD_LOCKED / SUSPICIOUS_ACTIVITY text on a recent attempt.
Each produces STOPPED_FRAUD + escalation; repeated stepping stays terminal and
creates zero interventions.
"""
from __future__ import annotations

from datetime import timedelta

from app import constants as C
from app.agent import AgentConfig, step_case
from app.constants import MODE_AUTONOMOUS
from app.detection import open_or_refresh_case
from app.execution.sandbox import SeededSandbox
from app.models import Escalation, Intervention
from sqlalchemy import select

from conftest import add_failed_payment, make_customer, make_invoice


def _cfg():
    return AgentConfig(mode=MODE_AUTONOMOUS, llm_mode="rules", provider=SeededSandbox())


def _act(db, customer, rzr, error_code, desc="", hours_ago=0):
    invoice = make_invoice(db, customer, rzr=rzr, amount_minor=8_000_000, due_days_ago=20)
    add_failed_payment(db, invoice, error_code=error_code, desc=desc, rzr=f"pay_{rzr}", hours_ago=hours_ago)
    case = open_or_refresh_case(db, invoice)
    return case


def _assert_fraud_stop(db, case, expected_code):
    step_case(db, case, _cfg())
    db.commit()
    assert case.state == C.STATE_STOPPED_FRAUD
    esc = db.scalar(select(Escalation).where(Escalation.case_id == case.id))
    assert esc is not None and esc.reason_code == expected_code
    n = len(db.scalars(select(Intervention).where(Intervention.case_id == case.id)).all())
    assert n == 0, "fraud cases must have zero interventions (never retried)"
    # stepping again changes nothing
    step_case(db, case, _cfg())
    assert case.state == C.STATE_STOPPED_FRAUD


def test_avs_mismatch_hard_stops(db, clock):
    case = _act(db, make_customer(db), "INV-AVS", "AVS_FAILED", "AVS check failed")
    _assert_fraud_stop(db, case, C.ESC_FRAUD_AVS)


def test_three_consecutive_auth_failures_hard_stop(db, clock):
    cust = make_customer(db)
    case = _act(db, cust, "INV-3A", "AUTH_FAILED", "authentication failed", hours_ago=24 * 1)
    # two more failures on the same invoice within the week
    invoice = case.invoice
    for i, h in enumerate([24 * 3, 24 * 5]):
        add_failed_payment(db, invoice, error_code="AUTH_FAILED", desc="authentication failed", rzr=f"pay_3a_{i}", hours_ago=h)
    case = open_or_refresh_case(db, invoice)
    _assert_fraud_stop(db, case, C.ESC_FRAUD_AUTH_FAILURES)


def test_suspicious_activity_text_hard_stop(db, clock):
    case = _act(db, make_customer(db), "INV-SUS", "RISK_DECLINE", "SUSPICIOUS_ACTIVITY — blocked by bank risk engine")
    _assert_fraud_stop(db, case, C.ESC_FRAUD_SUSPICIOUS)


def test_fraud_lock_code_hard_stop(db, clock):
    case = _act(db, make_customer(db), "INV-FL", "FRAUD_LOCKED", "card fraud-locked by issuer")
    _assert_fraud_stop(db, case, C.ESC_FRAUD_SUSPICIOUS)


def test_single_auth_failure_is_not_fraud(db, clock):
    """One auth failure is recoverable via a payment link — NOT a hard stop."""
    cust = make_customer(db, profile="pays_after_link")
    case = _act(db, cust, "INV-1A", "AUTH_FAILED", "2-step auth failed")
    step_case(db, case, _cfg())
    db.commit()
    assert case.state != C.STATE_STOPPED_FRAUD
    n = len(db.scalars(select(Intervention).where(Intervention.case_id == case.id)).all())
    assert n >= 1
