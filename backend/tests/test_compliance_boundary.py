"""Executable compliance rules: the 120-day boundary, DNC and bankruptcy.

These rules ALWAYS override AI output: they run before any decision and again
before dispatch. Nothing here involves the LLM.
"""
from __future__ import annotations

from datetime import datetime, timedelta

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


def _open_case(db, customer, rzr, amount_minor=10_000_000, due_days_ago=0):
    invoice = make_invoice(db, customer, rzr=rzr, amount_minor=amount_minor, due_days_ago=due_days_ago)
    return invoice, open_or_refresh_case(db, invoice)


def _executed(db, case_id) -> int:
    return len(
        db.scalars(
            select(Intervention).where(Intervention.case_id == case_id, Intervention.status.in_(["SUCCEEDED", "FAILED", "EXECUTING"]))
        ).all()
    )


class TestBoundary:
    def test_121_days_overdue_is_hard_blocked(self, db, clock):
        cust = make_customer(db)
        invoice, case = _open_case(db, cust, "INV-121", due_days_ago=121)
        step_case(db, case, _cfg())
        db.commit()
        assert case.state == C.STATE_STOPPED_COMPLIANCE
        esc = db.scalar(select(Escalation).where(Escalation.case_id == case.id))
        assert esc is not None and esc.reason_code == C.ESC_COMPLIANCE_AGE
        assert _executed(db, case.id) == 0, "no recovery action may run past the 120-day line"

    def test_exactly_120_days_is_allowed(self, db, clock):
        cust = make_customer(db, profile="pays_after_reminder")
        invoice, case = _open_case(db, cust, "INV-120", due_days_ago=120)
        step_case(db, case, _cfg())
        db.commit()
        assert case.state not in (C.STATE_STOPPED_COMPLIANCE, C.STATE_STOPPED_FRAUD, C.STATE_STOPPED_RULE)
        assert _executed(db, case.id) >= 1, "120-day-old invoice may still be worked"

    def test_119_days_is_allowed(self, db, clock):
        cust = make_customer(db, profile="pays_after_reminder")
        invoice, case = _open_case(db, cust, "INV-119", due_days_ago=119)
        step_case(db, case, _cfg())
        db.commit()
        assert case.state not in C.TERMINAL_STATES or case.state == C.STATE_RECOVERED

    def test_121_is_strictly_greater_than_120(self, db, clock):
        # guard against off-by-one drift in the rule implementation
        cust = make_customer(db)
        _, case121 = _open_case(db, cust, "INV-X", due_days_ago=121)
        step_case(db, case121, _cfg())
        assert case121.state == C.STATE_STOPPED_COMPLIANCE


class TestDNC:
    def test_dnc_hard_stops_all_outreach(self, db, clock):
        cust = make_customer(db, flags=[C.FLAG_DNC])
        invoice, case = _open_case(db, cust, "INV-DNC", due_days_ago=30)
        step_case(db, case, _cfg())
        db.commit()
        assert case.state == C.STATE_STOPPED_COMPLIANCE
        esc = db.scalar(select(Escalation).where(Escalation.case_id == case.id))
        assert esc.reason_code == C.ESC_COMPLIANCE_DNC
        assert _executed(db, case.id) == 0
        # even a payment failure cannot trigger outreach to a DNC customer
        add_failed_payment(db, invoice, error_code="BANK_TECHNICAL_ISSUE", rzr="pay_dnc")
        step_case(db, case, _cfg())
        assert _executed(db, case.id) == 0


class TestBankruptcy:
    def test_bankruptcy_hard_stops_and_cancels(self, db, clock):
        cust = make_customer(db, flags=[C.FLAG_BANKRUPTCY])
        invoice, case = _open_case(db, cust, "INV-BK", due_days_ago=60)
        step_case(db, case, _cfg())
        db.commit()
        assert case.state == C.STATE_STOPPED_COMPLIANCE
        esc = db.scalar(select(Escalation).where(Escalation.case_id == case.id))
        assert esc.reason_code == C.ESC_COMPLIANCE_BANKRUPTCY
        assert _executed(db, case.id) == 0
