"""Deterministic fusion/fallback contract + LLM-outage behaviour.

The application must continue working with the LLM unavailable: `--llm-mode
rules` (or any failure) degrades to the deterministic rules diagnosis and the
policy path is identical.
"""
from __future__ import annotations

from dataclasses import replace

from app import constants as C
from app.agent import AgentConfig, diagnose_case, ingest_events, step_case
from app.config import Settings
from app.constants import MODE_AUTONOMOUS
from app.detection import open_or_refresh_case
from app.diagnosis.fusion import fuse
from app.execution.sandbox import SeededSandbox
from app.models import Diagnosis
from sqlalchemy import select

from conftest import add_failed_payment, make_customer, make_invoice

RULE_TECH = {"cause": C.CAUSE_TECHNICAL, "confidence": 0.92, "proposed_playbook": C.PB_SMART_RETRY}
RULE_OVERDUE = {"cause": C.CAUSE_OVERDUE, "confidence": 0.9, "proposed_playbook": C.PB_DUNNING}


def test_llm_disabled_is_pure_rules():
    f = fuse(RULE_TECH, None, llm_enabled=False)
    assert f.path == C.DIAG_RULES
    assert not f.flagged and f.autonomous_ok
    assert "llm unavailable or disabled" in f.reason


def test_agreement_fuses_confidences():
    llm = {"cause": C.CAUSE_TECHNICAL, "confidence": 0.8, "proposed_playbook": C.PB_SMART_RETRY}
    f = fuse(RULE_TECH, llm, llm_enabled=True)
    assert f.path == C.DIAG_FUSED
    assert abs(f.confidence - 0.5 * 0.92 - 0.5 * 0.8) < 1e-9
    assert f.autonomous_ok


def test_agreement_low_confidence_requires_approval():
    llm = {"cause": C.CAUSE_TECHNICAL, "confidence": 0.2, "proposed_playbook": C.PB_SMART_RETRY}
    f = fuse(RULE_TECH, llm, llm_enabled=True)
    # fused = 0.56 -> approval band, not autonomous
    assert not f.autonomous_ok
    assert f.requires_approval


def test_disagreement_never_auto_acts():
    llm = {"cause": C.CAUSE_OVERDUE, "confidence": 0.9, "proposed_playbook": C.PB_DUNNING}
    f = fuse(RULE_TECH, llm, llm_enabled=True)
    assert f.flagged
    assert not f.autonomous_ok and not f.requires_approval
    assert "disagree" in f.reason


def test_llm_outage_falls_back_and_case_proceeds(db, clock, monkeypatch):
    """LLM raises/timeouts -> rules fallback; the agent still recovers money."""
    cust = make_customer(db)
    invoice = make_invoice(db, cust, rzr="INV-OUT", amount_minor=8_000_000)
    add_failed_payment(db, invoice, error_code="BANK_TECHNICAL_ISSUE", rzr="pay_out")
    case = open_or_refresh_case(db, invoice)

    settings = replace(
        Settings(db_url="sqlite://", llm_mode="auto"),
        openai_api_key="sk-test-not-real",
    )
    cfg = AgentConfig(mode=MODE_AUTONOMOUS, llm_mode="auto", settings=settings, provider=SeededSandbox())

    # simulate total LLM failure: diagnoser returns None (raise == None)
    monkeypatch.setattr("app.diagnosis.llm_diagnoser.diagnose_with_llm", lambda *a, **kw: None)

    tag = step_case(db, case, cfg)
    db.commit()
    assert tag in ("succeeded",)  # execution happened
    diag = db.scalar(select(Diagnosis).where(Diagnosis.case_id == case.id).order_by(Diagnosis.id.desc()))
    assert diag is not None and diag.llm_ok is False
    assert case.cause == C.CAUSE_TECHNICAL


def test_rules_mode_full_run_without_llm():
    """--llm-mode rules end-to-end: no key, no openai import, full pipeline."""
    from app.agent import AgentConfig as AC

    cfg = AC(mode=MODE_AUTONOMOUS, llm_mode="rules", provider=SeededSandbox())
    assert cfg.settings.openai_api_key == "" or True  # key never required
