from __future__ import annotations

from datetime import datetime

import pytest

from app.clock import reset_clock, set_clock, VirtualClock
from app.db import init_db, make_engine, make_session
from app.execution.sandbox import SeededSandbox
from app.models import Customer, Invoice, Payment


@pytest.fixture(autouse=True)
def _reset_clock():
    reset_clock()
    yield
    reset_clock()


@pytest.fixture
def db():
    engine = make_engine("sqlite:///:memory:")
    init_db(engine)
    session = make_session(engine)
    yield session
    session.close()


@pytest.fixture
def clock():
    c = VirtualClock(start=datetime(2026, 1, 5, 10, 0, 0))  # Monday 10:00
    set_clock(c)
    return c


def make_customer(db, *, org="Acme Exports", profile="default", flags=None, **kw):
    from app.models import ComplianceFlag

    cust = Customer(org_name=org, email="ap@acme.in", phone="+919876543210", behavior_profile=profile, **kw)
    db.add(cust)
    db.flush()
    for f in flags or []:
        db.add(ComplianceFlag(customer_id=cust.id, flag_type=f, reason_code="test"))
    db.flush()
    return cust


def make_invoice(db, customer, *, rzr="INV-T1", amount_minor=10000000, paid_minor=0, due_days_ago=None, now=None):
    from datetime import timedelta

    now = now or datetime(2026, 1, 5, 10, 0, 0)
    due = now if due_days_ago is None else now - timedelta(days=due_days_ago)
    inv = Invoice(
        rzr_invoice_id=rzr,
        customer_id=customer.id,
        amount_minor=amount_minor,
        paid_amount_minor=paid_minor,
        status="partially_paid" if paid_minor else "issued",
        due_date=due,
    )
    db.add(inv)
    db.flush()
    return inv


def add_failed_payment(db, invoice, *, error_code="BANK_TECHNICAL_ISSUE", desc="", rzr="pay_t1", hours_ago=0, now=None):
    now = now or datetime(2026, 1, 5, 10, 0, 0)
    from datetime import timedelta

    p = Payment(
        rzr_payment_id=rzr,
        invoice_id=invoice.id,
        customer_id=invoice.customer_id,
        amount_minor=invoice.amount_minor,
        status="failed",
        error_code=error_code,
        error_description=desc,
        attempted_at=now - timedelta(hours=hours_ago),
    )
    db.add(p)
    db.flush()
    return p
