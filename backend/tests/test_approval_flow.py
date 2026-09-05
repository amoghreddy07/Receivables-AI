"""Supervised mode: human approval gating, audited decisions, safe timeout.

SUPERVISED: every action parks as PENDING_APPROVAL. Approve -> exactly-once
execution. Decline -> CLOSED_NOOP (audited). No decision within the timeout ->
system-timeout auto-decline (safe default).
"""
from __future__ import annotations

from datetime import timedelta

from app import constants as C
from app.agent import AgentConfig, decide_approval, run_tick, step_case
from app.constants import MODE_SUPERVISED
from app.detection import open_or_refresh_case
from app.execution.sandbox import SeededSandbox
from app.models import Approval, AuditEvent, Intervention
from sqlalchemy import select

from conftest import make_customer, make_invoice


def _cfg(clock):
    return AgentConfig(mode=MODE_SUPERVISED, llm_mode="rules", provider=SeededSandbox())


def _pending(db, case_id):
    return db.scalars(
        select(Intervention).where(Intervention.case_id == case_id, Intervention.status == "PENDING_APPROVAL")
    ).all()


def _park_case(db, rzr="INV-AP", profile="pays_after_reminder"):
    cust = make_customer(db, profile=profile)
    invoice = make_invoice(db, cust, rzr=rzr, amount_minor=9_000_000, due_days_ago=30)
    case = open_or_refresh_case(db, invoice)
    return case


def test_supervised_parks_action_and_approval_executes(db, clock):
    cfg = _cfg(clock)
    case = _park_case(db)
    tag = step_case(db, case, cfg)
    db.commit()
    assert tag == "pending_approval"
    assert case.state == C.STATE_PENDING_APPROVAL
    pending = _pending(db, case.id)
    assert len(pending) == 1

    res = decide_approval(db, pending[0].id, decision="APPROVED", actor="finance-lead", reason="approved in demo", cfg=cfg)
    db.commit()
    assert res["ok"]
    inter = db.get(Intervention, pending[0].id)
    assert inter.status == "SUCCEEDED"
    # human decision is audited
    human_evts = db.scalars(select(AuditEvent).where(AuditEvent.case_id == case.id, AuditEvent.actor == C.ACTOR_HUMAN)).all()
    assert any("human_decision" == e.action for e in human_evts)
    # simulated money arrives +2h later and closes the case
    clock.advance(hours=3)
    run_tick(db, cfg)
    db.commit()
    assert case.state == C.STATE_RECOVERED


def test_supervised_decline_is_audited_and_closes(db, clock):
    cfg = _cfg(clock)
    case = _park_case(db, rzr="INV-DEC")
    step_case(db, case, cfg)
    db.commit()
    pending = _pending(db, case.id)
    res = decide_approval(db, pending[0].id, decision="DECLINED", actor="collections-lead", reason="customer disputes invoice", cfg=cfg)
    db.commit()
    assert res["status"] == "declined"
    assert db.get(Intervention, pending[0].id).status == "DECLINED"
    assert case.state == C.STATE_CLOSED_NOOP
    appr = db.scalar(select(Approval).where(Approval.intervention_id == pending[0].id))
    assert appr.decision == "DECLINED" and appr.reason == "customer disputes invoice"


def test_approval_timeout_auto_declines(db, clock):
    cfg = _cfg(clock)
    case = _park_case(db, rzr="INV-TIMEOUT")
    step_case(db, case, cfg)
    db.commit()
    pending = _pending(db, case.id)
    assert len(pending) == 1

    clock.advance(minutes=cfg.settings.approval_timeout_minutes + 1)
    run_tick(db, cfg)  # hourly tick expires stale approvals
    db.commit()

    inter = db.get(Intervention, pending[0].id)
    assert inter.status == "DECLINED"
    appr = db.scalar(select(Approval).where(Approval.intervention_id == inter.id))
    assert appr.decided_by == "system-timeout" and appr.decision == "DECLINED"
    assert case.state == C.STATE_CLOSED_NOOP
