"""Unstructured-context AI value: what the LLM reads that rules cannot.

Covers the upgrade's guarantees:
  1. The deterministic rules classifier ABSTAINS (insufficient_context) when the
     answer lives in free text — it never guesses "overdue -> dunning".
  2. Hard structured signals (payment error codes, compliance flags, fraud)
     still classify deterministically even with messages present.
  3. The offline NLU diagnoser reads documentation / AP / PTP / dispute intent
     from the same redacted snapshot a real model would see.
  4. Rules-only mode escalates unstructured cases without acting (no false
     dunning); AI mode performs the scenario-appropriate bounded action.
  5. The scenario sandbox pays ONLY on the correct intervention.
  6. Fraud / DNC / bankruptcy hard-stops survive unstructured context.
  7. The audit trail records the LLM reading (evidence + promised date) and the
     hash chain stays valid.
"""
from __future__ import annotations

from datetime import timedelta

import pytest
from sqlalchemy import select

from app import constants as C
from app.agent import AgentConfig, step_case
from app.audit import case_events, verify
from app.constants import MODE_AUTONOMOUS
from app.detection import open_or_refresh_case
from app.execution.sandbox import SeededSandbox
from app.models import (
    AuditEvent,
    CaseMessage,
    Diagnosis,
    Escalation,
    Intervention,
    PTPPromise,
)

from conftest import add_failed_payment, make_customer, make_invoice

DOCS_EMAIL = (
    "Hi, our AP team has approved invoice $INV but the GST details printed on it "
    "are incorrect. Please resend a corrected copy and we will release payment "
    "within 2 working days of receiving it."
)
PTP_EMAIL = (
    "Hi, we will pay invoice $INV on 20 January 2026 via NEFT. Please confirm "
    "and do not send reminders before that date."
)
DISPUTE_EMAIL = (
    "We did not receive the services billed on invoice $INV and dispute the "
    "charges. We will not pay until this is resolved."
)
AP_EMAIL = (
    "Payment for invoice $INV is with our accounts payable team and will be "
    "released next week. There is no need to escalate."
)


def _cfg(*, llm_mode: str = "rules", offline_llm: bool = False) -> AgentConfig:
    return AgentConfig(
        mode=MODE_AUTONOMOUS,
        llm_mode=llm_mode,
        provider=SeededSandbox(),
        offline_llm=offline_llm,
    )


def _add_message(db, invoice, content: str, *, clock=None, days_ago: float = 2.0):
    from app.clock import now

    at = (clock.now() if clock is not None else now()) - timedelta(hours=days_ago * 24)
    db.add(
        CaseMessage(
            invoice_id=invoice.id,
            customer_id=invoice.customer_id,
            direction="in",
            channel="email",
            content=content.replace("$INV", invoice.rzr_invoice_id),
            received_at=at,
        )
    )
    db.flush()


def _executed(db, case_id: int) -> list[str]:
    return [
        iv.action
        for iv in db.scalars(
            select(Intervention).where(
                Intervention.case_id == case_id,
                Intervention.status.in_(["SUCCEEDED", "FAILED", "EXECUTING"]),
            )
        ).all()
    ]


def _new_db():
    from app.db import init_db, make_engine, make_session

    engine = make_engine("sqlite:///:memory:")
    init_db(engine)
    return engine, make_session(engine)


# --------------------------------------------------------------------------- #
# 1. Rules classifier abstention vs hard structured signals
# --------------------------------------------------------------------------- #
def test_rules_classifier_abstains_on_documentation_email(db, clock):
    from app.diagnosis.rules_classifier import classify

    cust = make_customer(db, profile="docs_issue")
    invoice = make_invoice(db, cust, rzr="INV-DOCS", amount_minor=50_000_000, due_days_ago=30, now=clock.now())
    _add_message(db, invoice, DOCS_EMAIL, clock=clock)
    case = open_or_refresh_case(db, invoice)

    diag = classify(db, case)
    assert diag.cause == C.CAUSE_INSUFFICIENT_CONTEXT, diag
    assert diag.confidence < 0.5
    assert any(e.get("rule") == "unstructured_context_present" for e in diag.evidence)


def test_rules_classifier_keeps_hard_payment_signal_with_messages(db, clock):
    from app.diagnosis.rules_classifier import classify

    cust = make_customer(db, profile="default")
    invoice = make_invoice(db, cust, rzr="INV-TECH", amount_minor=50_000_000, due_days_ago=30, now=clock.now())
    add_failed_payment(db, invoice, error_code="BANK_TECHNICAL_ISSUE", rzr="pay_tech", now=clock.now())
    _add_message(db, invoice, "Please do not retry before we confirm — we are checking internally.", clock=clock)
    case = open_or_refresh_case(db, invoice)
    assert classify(db, case).cause == C.CAUSE_TECHNICAL  # structured signal wins


def test_rules_classifier_keeps_fraud_signal_with_messages(db, clock):
    from app.diagnosis.rules_classifier import classify

    cust = make_customer(db, profile="default")
    invoice = make_invoice(db, cust, rzr="INV-FRAUD", amount_minor=50_000_000, due_days_ago=30, now=clock.now())
    add_failed_payment(db, invoice, error_code="AVS_FAILED", rzr="pay_avs", now=clock.now())
    _add_message(db, invoice, "We suspect the card was compromised — please investigate.", clock=clock)
    case = open_or_refresh_case(db, invoice)
    assert classify(db, case).cause == C.CAUSE_FRAUD_SUSPECTED  # never LLM-negotiable


# --------------------------------------------------------------------------- #
# 2. Rules-only vs AI-enabled on one unstructured case
# --------------------------------------------------------------------------- #
def test_rules_only_escalates_unstructured_case_without_guessing(db, clock):
    cust = make_customer(db, profile="docs_issue")
    invoice = make_invoice(db, cust, rzr="INV-R1", amount_minor=50_000_000, due_days_ago=30, now=clock.now())
    _add_message(db, invoice, DOCS_EMAIL, clock=clock)
    case = open_or_refresh_case(db, invoice)

    tag = step_case(db, case, _cfg(llm_mode="rules"))
    db.commit()
    assert case.state == C.STATE_ESCALATED
    esc = db.scalar(select(Escalation).where(Escalation.case_id == case.id))
    assert esc is not None and esc.reason_code == C.ESC_LOW_CONFIDENCE
    assert _executed(db, case.id) == [], "rules-only must NOT guess a dunning action"
    diag = db.scalar(select(Diagnosis).where(Diagnosis.case_id == case.id).order_by(Diagnosis.id.desc()))
    assert diag.cause == C.CAUSE_INSUFFICIENT_CONTEXT and diag.path == C.DIAG_RULES
    assert tag == "escalated"


def test_ai_enabled_performs_documentation_fix_and_recovers(db, clock):
    from app.eval.corpus import build_one
    from app.eval.harness import simulate_case

    cc = build_one("docs_issue_gst")

    # rules-only: escalates, nothing recovered, no actions
    _e, db1 = _new_db()
    row_rules = simulate_case(db1, cc, _cfg(llm_mode="rules"))
    assert row_rules["state"] == C.STATE_ESCALATED
    assert row_rules["recovered_amount_minor"] == 0
    assert [a for a in row_rules["actions_taken"] if a["status"] in ("SUCCEEDED", "FAILED")] == []
    db1.close()

    # AI-enabled: correct bounded action, deterministic recovery
    _e2, db2 = _new_db()
    row_ai = simulate_case(db2, cc, _cfg(llm_mode="auto", offline_llm=True))
    assert row_ai["state"] == C.STATE_RECOVERED
    assert row_ai["recovered_amount_minor"] == cc["amount_minor"]
    actions = [a for a in row_ai["actions_taken"] if a["status"] == "SUCCEEDED"]
    assert [a["action"] for a in actions] == [C.ACT_RESEND_CORRECTED_INVOICE]
    diag = db2.scalar(select(Diagnosis).where(Diagnosis.path == C.DIAG_LLM).limit(1))
    assert diag is not None and diag.cause == C.CAUSE_DOCUMENTATION_ISSUE
    assert "GST" in str(diag.llm_json.get("evidence", ""))
    db2.close()


def test_ai_confirms_explicit_promise_and_waits(db, clock):
    from app.eval.corpus import build_one
    from app.eval.harness import simulate_case

    cc = build_one("ptp_offer_explicit_date")
    _e, db = _new_db()
    row = simulate_case(db, cc, _cfg(llm_mode="auto", offline_llm=True))
    assert row["state"] == C.STATE_RECOVERED, row
    promise = db.scalar(select(PTPPromise))
    assert promise is not None and promise.status == "active"
    diag = db.scalar(select(Diagnosis).order_by(Diagnosis.id.desc()))
    assert diag.llm_json.get("promised_date_iso") == "2026-01-20"
    db.close()


# --------------------------------------------------------------------------- #
# 3. Offline NLU reads the same snapshot a model would see
# --------------------------------------------------------------------------- #
def test_offline_diagnoser_reads_unstructured_intents(db, clock):
    from app.diagnosis.context_builder import build_snapshot
    from app.diagnosis.offline import offline_diagnose

    cases = [
        (DOCS_EMAIL, C.CAUSE_DOCUMENTATION_ISSUE, C.PB_DOCUMENTATION_FIX, None),
        (PTP_EMAIL, C.CAUSE_PTP_OFFERED, C.PB_PROMISE_ACCEPT, "2026-01-20"),
        (DISPUTE_EMAIL, C.CAUSE_DISPUTE, C.PB_ESCALATE, None),
        (AP_EMAIL, C.CAUSE_AWAITING_AP_APPROVAL, C.PB_AP_COORDINATION, None),
    ]
    for idx, (content, cause, playbook, promised) in enumerate(cases):
        cust = make_customer(db, org=f"Org {idx}")
        invoice = make_invoice(db, cust, rzr=f"INV-OFF{idx}", amount_minor=20_000_000, due_days_ago=20, now=clock.now())
        _add_message(db, invoice, content, clock=clock)
        case = open_or_refresh_case(db, invoice)
        diag = offline_diagnose(build_snapshot(db, case))
        assert diag is not None, content
        assert diag.cause == cause, (content, diag.cause)
        assert diag.proposed_playbook == playbook
        assert diag.evidence, "evidence must quote the message it read"
        if promised:
            assert diag.promised_date_iso == promised


def test_offline_diagnoser_is_deterministic(db, clock):
    from app.diagnosis.context_builder import build_snapshot
    from app.diagnosis.offline import offline_diagnose

    cust = make_customer(db)
    invoice = make_invoice(db, cust, rzr="INV-DET", amount_minor=20_000_000, due_days_ago=20, now=clock.now())
    _add_message(db, invoice, PTP_EMAIL, clock=clock)
    case = open_or_refresh_case(db, invoice)
    snap = build_snapshot(db, case)
    d1 = offline_diagnose(snap)
    d2 = offline_diagnose(snap)
    assert d1 is not None and d1.as_dict() == d2.as_dict()


# --------------------------------------------------------------------------- #
# 4. Scenario sandbox gates on the intervention, not the profile
# --------------------------------------------------------------------------- #
def test_scenario_world_pays_only_for_the_right_intervention(db, clock):
    sbox = SeededSandbox()
    scenarios = [
        ("docs_issue", "resend_corrected_invoice"),
        ("ap_queue", "request_ap_update"),
        ("ptp_offer_will_pay", "confirm_ptp"),
    ]
    for profile, right_method in scenarios:
        cust = make_customer(db, org=f"W-{profile}", profile=profile)
        invoice = make_invoice(db, cust, rzr=f"INV-W-{profile}", amount_minor=10_000_000, due_days_ago=25, now=clock.now())

        if right_method == "resend_corrected_invoice":
            res = sbox.resend_corrected_invoice(invoice, cust, correction_note="fix", channel="email")
        elif right_method == "request_ap_update":
            res = sbox.request_ap_update(invoice, cust, followup_number=1, channel="email")
        else:
            res = sbox.confirm_ptp(invoice, cust, promised_date_iso="2026-01-20", channel="email")
        assert res["pending_payment"] is not None, f"{profile}: correct action must settle"

        wrong_link = sbox.create_payment_link(invoice, cust, amount_minor=10_000_000, expire_hours=72, channel="email")
        assert wrong_link["pending_payment"] is None, f"{profile}: payment link must not settle"
        wrong_reminder = sbox.send_reminder(invoice, cust, template="dunning_1", channel="email", amount_minor=10_000_000)
        assert wrong_reminder["pending_payment"] is None, f"{profile}: dunning reminder must not settle"


def test_dispute_world_never_pays(db, clock):
    sbox = SeededSandbox()
    cust = make_customer(db, profile="never_pays")
    invoice = make_invoice(db, cust, rzr="INV-DISP", amount_minor=10_000_000, due_days_ago=30, now=clock.now())
    for op in (
        lambda: sbox.send_reminder(invoice, cust, template="dunning_1", channel="email", amount_minor=10_000_000),
        lambda: sbox.create_payment_link(invoice, cust, amount_minor=10_000_000, expire_hours=72, channel="email"),
    ):
        assert op()["pending_payment"] is None


# --------------------------------------------------------------------------- #
# 5. Hard stops survive unstructured context (both modes)
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("flags,error_code,expected_state,expected_code", [
    ([C.FLAG_DNC], "", C.STATE_STOPPED_COMPLIANCE, C.ESC_COMPLIANCE_DNC),
    ([C.FLAG_BANKRUPTCY], "", C.STATE_STOPPED_COMPLIANCE, C.ESC_COMPLIANCE_BANKRUPTCY),
    ([], "AVS_FAILED", C.STATE_STOPPED_FRAUD, C.ESC_FRAUD_AVS),
])
def test_hard_stops_with_unstructured_context_both_modes(db, clock, flags, error_code, expected_state, expected_code):
    for llm_mode, offline in (("rules", False), ("auto", True)):
        cust = make_customer(db, profile="default", flags=flags)
        invoice = make_invoice(db, cust, rzr=f"INV-HS-{llm_mode}-{expected_code}", amount_minor=60_000_000, due_days_ago=45, now=clock.now())
        if error_code:
            add_failed_payment(db, invoice, error_code=error_code, rzr=f"pay_hs_{llm_mode}", now=clock.now())
        _add_message(db, invoice, "We are disputing this and ask you not to take any further action.", clock=clock)
        case = open_or_refresh_case(db, invoice)
        step_case(db, case, _cfg(llm_mode=llm_mode, offline_llm=offline))
        db.commit()
        assert case.state == expected_state, (llm_mode, case.state)
        assert _executed(db, case.id) == [], (llm_mode, "hard stop must prevent every action")
        esc = db.scalar(select(Escalation).where(Escalation.case_id == case.id))
        assert esc is not None and esc.reason_code == expected_code


def test_dispute_diagnosis_escalates_with_dispute_code(db, clock):
    cust = make_customer(db, profile="never_pays")
    invoice = make_invoice(db, cust, rzr="INV-DIS", amount_minor=40_000_000, due_days_ago=25, now=clock.now())
    _add_message(db, invoice, DISPUTE_EMAIL, clock=clock)
    case = open_or_refresh_case(db, invoice)
    step_case(db, case, _cfg(llm_mode="auto", offline_llm=True))
    db.commit()
    assert case.state == C.STATE_ESCALATED
    esc = db.scalar(select(Escalation).where(Escalation.case_id == case.id))
    assert esc.reason_code == C.ESC_DISPUTE
    assert _executed(db, case.id) == [], "a disputing customer is never auto-collected"


# --------------------------------------------------------------------------- #
# 6. Audit trail preserves the AI reading + abstention
# --------------------------------------------------------------------------- #
def test_audit_trail_records_llm_diagnosis_and_chain_stays_valid(db, clock):
    from app.eval.corpus import build_one
    from app.eval.harness import simulate_case

    cc = build_one("ptp_offer_explicit_date")
    _e, db = _new_db()
    simulate_case(db, cc, _cfg(llm_mode="auto", offline_llm=True))

    ok, broken, msg = verify(db)
    assert ok, f"audit chain broken: {msg} {broken}"

    evts = db.scalars(select(AuditEvent).where(AuditEvent.action == "llm_diagnosis")).all()
    assert len(evts) >= 1
    llm_payload = evts[0].payload.get("diagnosis", {})
    assert llm_payload.get("cause") == C.CAUSE_PTP_OFFERED
    assert llm_payload.get("promised_date_iso") == "2026-01-20"
    decisions = db.scalars(select(AuditEvent).where(AuditEvent.action == "policy_decision")).all()
    assert decisions and decisions[0].payload.get("action") == C.ACT_CONFIRM_PTP
    db.close()


def test_audit_trail_records_rules_abstention(db, clock):
    cust = make_customer(db, profile="docs_issue")
    invoice = make_invoice(db, cust, rzr="INV-ABS", amount_minor=50_000_000, due_days_ago=30, now=clock.now())
    _add_message(db, invoice, DOCS_EMAIL, clock=clock)
    case = open_or_refresh_case(db, invoice)
    step_case(db, case, _cfg(llm_mode="rules"))
    db.commit()
    events = case_events(db, case.id)
    actions = [ev.action for ev in events]
    assert "diagnosis_fused" in actions
    fused = next(ev for ev in events if ev.action == "diagnosis_fused")
    assert fused.payload["cause"] == C.CAUSE_INSUFFICIENT_CONTEXT
