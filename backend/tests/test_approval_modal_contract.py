"""Regression tests: approval form contract + decline-reason enforcement.

Covers the demo-breaking bug where the custom approval modal submitted the
form without a `decision` field (form.submit() drops the clicked button's
name/value). The route-level tests below pin the SERVER side of the contract:

  * decision=APPROVED  -> decide_approval() executes the parked action
  * decision=DECLINED  -> with a non-blank reason, the case closes audited
  * decision=DECLINED  -> with a blank/whitespace reason is rejected with a
    rendered, human-readable page (never a raw 400/422/500) and the
    intervention STAYS pending

The frontend half (the modal appending a hidden `decision` input before
submitting) is verified against the live dashboard, not in pytest.
"""
from __future__ import annotations

from starlette.requests import Request

from app import constants as C
from app.agent import AgentConfig, step_case
from app.constants import MODE_SUPERVISED
from app.detection import open_or_refresh_case
from app.execution.sandbox import SeededSandbox
from app.main import approval_decision
from app.models import Approval, Intervention
from sqlalchemy import select

from conftest import make_customer, make_invoice


def _park_supervised(db, clock, rzr="INV-CT"):
    cfg = AgentConfig(mode=MODE_SUPERVISED, llm_mode="rules", provider=SeededSandbox())
    cust = make_customer(db, profile="pays_after_reminder")
    invoice = make_invoice(db, cust, rzr=rzr, amount_minor=9_000_000, due_days_ago=30)
    case = open_or_refresh_case(db, invoice)
    tag = step_case(db, case, cfg)
    db.commit()
    assert tag == "pending_approval"
    iv = db.scalar(select(Intervention).where(Intervention.case_id == case.id))
    return case, iv


def _post(db, iv, decision, reason):
    """Call the FastAPI route directly (no HTTP stack needed for the contract)."""
    scope = {
        "type": "http",
        "method": "POST",
        "path": f"/approvals/{iv.id}/decide",
        "headers": [],
        "query_string": b"",
        "client": ("testclient", 50000),
        "server": ("testserver", 80),
        "scheme": "http",
        "root_path": "",
    }
    return approval_decision(
        iv.id,
        Request(scope),
        decision=decision,
        reason=reason,
        actor="finance-lead@receivablesai.demo",
        next="/approvals",
        db=db,
    )


def test_approve_decision_transmitted_and_executes(db, clock):
    case, iv = _park_supervised(db, clock, rzr="INV-APPR")
    resp = _post(db, iv, "APPROVED", "customer confirmed corrected invoice")
    assert resp.status_code == 303  # redirect, not 422
    db.commit()

    inter = db.get(Intervention, iv.id)
    assert inter.status == "SUCCEEDED"
    appr = db.scalar(select(Approval).where(Approval.intervention_id == iv.id))
    assert appr.decision == "APPROVED" and appr.reason == "customer confirmed corrected invoice"


def test_approve_does_not_require_reason(db, clock):
    case, iv = _park_supervised(db, clock, rzr="INV-NOREASON")
    resp = _post(db, iv, "APPROVED", "")
    assert resp.status_code == 303
    db.commit()
    assert db.get(Intervention, iv.id).status == "SUCCEEDED"


def test_decline_with_valid_reason_succeeds(db, clock):
    case, iv = _park_supervised(db, clock, rzr="INV-DEC-OK")
    resp = _post(db, iv, "DECLINED", "customer disputes the invoice")
    assert resp.status_code == 303
    db.commit()

    inter = db.get(Intervention, iv.id)
    assert inter.status == "DECLINED"
    assert case.state == C.STATE_CLOSED_NOOP
    appr = db.scalar(select(Approval).where(Approval.intervention_id == iv.id))
    assert appr.decision == "DECLINED" and appr.reason == "customer disputes the invoice"


def test_decline_blank_reason_rejected_and_stays_pending(db, clock):
    case, iv = _park_supervised(db, clock, rzr="INV-DEC-BLANK")
    resp = _post(db, iv, "DECLINED", "")
    # Rendered approvals page (human-readable), not a bare 400/422/500.
    assert resp.status_code == 200
    body = resp.body.decode("utf-8")
    assert "Decline requires a reason" in body
    db.commit()

    # Nothing was decided: intervention still pending, no Approval row, no flash.
    assert db.get(Intervention, iv.id).status == "PENDING_APPROVAL"
    assert db.scalar(select(Approval).where(Approval.intervention_id == iv.id)) is None
    assert case.state == C.STATE_PENDING_APPROVAL


def test_decline_whitespace_reason_rejected_and_stays_pending(db, clock):
    case, iv = _park_supervised(db, clock, rzr="INV-DEC-WS")
    resp = _post(db, iv, "DECLINED", "   \t\n  ")
    assert resp.status_code == 200
    assert "Decline requires a reason" in resp.body.decode("utf-8")
    db.commit()
    assert db.get(Intervention, iv.id).status == "PENDING_APPROVAL"
    assert db.scalar(select(Approval).where(Approval.intervention_id == iv.id)) is None
    assert case.state == C.STATE_PENDING_APPROVAL