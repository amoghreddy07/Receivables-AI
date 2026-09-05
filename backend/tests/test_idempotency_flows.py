"""Idempotency + end-to-end agent flows + sandbox determinism.

Duplicate webhooks (repeated delivery), same-batch duplicates, self-healing
payments and promise-to-pay deferrals must all behave exactly once.
"""
from __future__ import annotations

from app import constants as C
from app.agent import AgentConfig, ingest_events, step_case
from app.constants import MODE_AUTONOMOUS
from app.detection import open_or_refresh_case
from app.execution.sandbox import SeededSandbox
from app.models import Intervention, Payment, PTPPromise, RiskCase
from sqlalchemy import select

from conftest import add_failed_payment, make_customer, make_invoice


def _cfg():
    return AgentConfig(mode=MODE_AUTONOMOUS, llm_mode="rules", provider=SeededSandbox())


def _fail_event(invoice, payment_id, code="BANK_TECHNICAL_ISSUE", desc=""):
    from app.clock import now

    return {
        "event_id": f"evt:{invoice.rzr_invoice_id}:fail:{payment_id}",
        "event_type": "payment.failed",
        "rzr_invoice_id": invoice.rzr_invoice_id,
        "rzr_payment_id": payment_id,
        "amount_minor": invoice.amount_minor,
        "method": "card",
        "error_code": code,
        "error_description": desc,
        "attempted_at": now().isoformat(),
        "verified": True,
        "source": "sim",
    }


def test_duplicate_webhook_delivery_is_exactly_once(db, clock):
    cust = make_customer(db)
    invoice = make_invoice(db, cust, rzr="INV-DUP", amount_minor=5_000_000)
    event = _fail_event(invoice, "pay_dup_1")
    s1 = ingest_events(db, [event], _cfg())
    db.commit()
    s2 = ingest_events(db, [event], _cfg())  # webhook redelivery
    db.commit()

    assert s1["new"] == 1 and s2["duplicates"] == 1
    cases = db.scalars(select(RiskCase)).all()
    assert len(cases) == 1
    interventions = db.scalars(select(Intervention)).all()
    assert len(interventions) == 1, "duplicate delivery must not double-execute"
    assert interventions[0].status == "SUCCEEDED"


def test_same_batch_duplicates_do_not_double_execute(db, clock):
    cust = make_customer(db)
    invoice = make_invoice(db, cust, rzr="INV-BATCH", amount_minor=5_000_000)
    event = _fail_event(invoice, "pay_batch_1")
    stats = ingest_events(db, [event, event, event], _cfg())
    db.commit()
    assert stats["new"] == 1 and stats["duplicates"] == 2
    assert len(db.scalars(select(Intervention)).all()) == 1


def test_self_healing_payment_takes_no_action(db, clock):
    """Failure + success in one batch: invoice paid before the agent acts."""
    cust = make_customer(db)
    invoice = make_invoice(db, cust, rzr="INV-SELF", amount_minor=6_000_000)
    fail = _fail_event(invoice, "pay_self_1", code="BANK_TECHNICAL_ISSUE")
    pay = {
        "event_id": "evt:INV-SELF:paid",
        "event_type": "invoice.paid",
        "rzr_invoice_id": invoice.rzr_invoice_id,
        "rzr_payment_id": "pay_self_col",
        "amount_minor": invoice.amount_minor,
        "method": "neft",
        "verified": True,
        "source": "sim",
    }
    stats = ingest_events(db, [fail, pay], _cfg())
    db.commit()
    assert len(db.scalars(select(Intervention)).all()) == 0
    assert invoice.is_paid
    case = db.scalar(select(RiskCase))
    assert case is None, "no case exists for a self-healed payment"


def test_recovered_flow_credits_money_and_closes(db, clock):
    cust = make_customer(db)
    invoice = make_invoice(db, cust, rzr="INV-REC", amount_minor=7_000_000)
    add_failed_payment(db, invoice, error_code="BANK_TECHNICAL_ISSUE", rzr="pay_rec_1")
    case = open_or_refresh_case(db, invoice)
    step_case(db, case, _cfg())
    db.commit()
    assert case.state == C.STATE_RECOVERED
    assert case.recovered_amount_minor == invoice.amount_minor
    assert invoice.is_paid


def test_ptp_future_promise_defers_until_promise_date(db, clock):
    from datetime import timedelta

    cust = make_customer(db)
    invoice = make_invoice(db, cust, rzr="INV-PTP", amount_minor=8_000_000, due_days_ago=10)
    case = open_or_refresh_case(db, invoice)
    db.add(PTPPromise(case_id=case.id, promised_date=clock.now() + timedelta(days=5), amount_minor=invoice.amount_minor, status="active"))
    db.commit()
    tag = step_case(db, case, _cfg())
    db.commit()
    assert tag == "deferred_ptp"
    assert case.next_action_at is not None and case.next_action_at > clock.now()
    assert len(db.scalars(select(Intervention)).all()) == 0, "no dunning before a promised date"


# --------------------------------------------------------------------------- #
# Sandbox determinism
# --------------------------------------------------------------------------- #
def test_sandbox_deterministic_and_seeded_from_case_data(db, clock):
    from app.execution.sandbox import SeededSandbox

    cust_a = make_customer(db, org="Alpha", profile="pays_after_link")
    inv_a = make_invoice(db, cust_a, rzr="INV-SB1", amount_minor=4_000_000)
    add_failed_payment(db, inv_a, error_code="AUTH_FAILED", rzr="pay_sb1")

    s1, s2 = SeededSandbox(), SeededSandbox()
    pay = db.scalars(select(Payment)).all()[0]

    r1 = s1.retry_payment(pay, inv_a, cust_a, attempt=1)
    r2 = s2.retry_payment(pay, inv_a, cust_a, attempt=1)
    assert r1 == r2, "two sandbox instances must agree exactly"
    assert r1["ok"] is False, "auth failures are never recoverable by retry"
    assert r1["failure_code"] == "AUTH_FAILED"
    # link dispatch yields a deterministic pending payment
    l1 = s1.create_payment_link(inv_a, cust_a, amount_minor=4_000_000, expire_hours=72, channel="email")
    l2 = s2.create_payment_link(inv_a, cust_a, amount_minor=4_000_000, expire_hours=72, channel="email")
    assert l1 == l2
    assert l1["pending_payment"]["amount_minor"] == 4_000_000


def test_sandbox_funds_retry_outcome_table(db, clock):
    from app.execution.sandbox import SeededSandbox

    cust = make_customer(db)
    inv = make_invoice(db, cust, rzr="INV-F", amount_minor=3_000_000)
    add_failed_payment(db, inv, error_code="INSUFFICIENT_FUNDS", rzr="pay_f")
    pay = db.scalars(select(Payment)).all()[0]
    s = SeededSandbox()
    assert s.retry_payment(pay, inv, cust, attempt=1)["ok"] is False
    assert s.retry_payment(pay, inv, cust, attempt=2)["ok"] is True
