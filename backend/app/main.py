"""ReceivablesAI operations console — FastAPI + Jinja2, no JS framework.

This dashboard is a READ-ONLY view over the existing application DB plus three
strictly-scoped POST actions that call EXISTING application logic:

  * approve / decline   -> app.agent.decide_approval()  (never UI logic)
  * demo tamper         -> mutates exactly ONE audit payload in the DEMO DB,
                           then the EXISTING chain verifier flags it
  * demo reset          -> re-runs the existing scripts/seed_demo.py as a
                           SUBPROCESS (never imported into this process)

Run (from backend/):
    RA_DB_URL=sqlite:///data/demo.db ../.venv/Scripts/python -m uvicorn app.main:app --port 8000

Pages: Overview /, Recovery Queue /cases, Case detail /cases/{id},
Approvals /approvals, Evaluation /eval, Audit /audit.

Everything is computed from persisted rows (RiskCase, Diagnosis, Intervention,
AuditEvent, EvalRun, EvalCase ...). No business rule, approval path, policy
verdict or evaluation metric is reimplemented here; demo/simulated markers come
from scripts/seed_demo.py's meta.demo / meta.simulated tags.
"""
from __future__ import annotations

import json
import re
import subprocess
import sys
import threading
from pathlib import Path
from urllib.parse import quote

from fastapi import Depends, FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy import func, select
from sqlalchemy.orm import Session, selectinload

from app import constants as C
from app.agent import AgentConfig, decide_approval
from app.audit import canonical_json, case_events, verify
from app.clock import now as clock_now
from app.config import BACKEND_DIR, settings
from app.db import get_db, init_db, make_engine, make_session
from app.eval.runner import AGENT_NAIVE, AGENT_REAL, run_evaluation
from app.execution.sandbox import SeededSandbox
from app.models import (
    Approval,
    AuditEvent,
    CaseMessage,
    ComplianceFlag,
    Customer,
    Diagnosis,
    Escalation,
    EvalCase,
    EvalRun,
    Intervention,
    Invoice,
    Payment,
    PTPPromise,
    RiskCase,
)
from app.presentation import (
    STATE_LABELS,
    STATE_CLASS,
    VERDICT_LABELS,
    VERDICT_CLASS,
    PATH_LABELS,
    cause_label,
    playbook_label,
    action_label,
    esc_label,
    event_action_label,
    inr_major,
    num_group,
    pct,
    fmt_dt,
)

BASE_DIR = Path(__file__).resolve().parent
TEMPLATES = BASE_DIR / "templates"
STATIC = BASE_DIR / "static"

app = FastAPI(title="ReceivablesAI Console", docs_url=None, redoc_url=None)
app.mount("/static", StaticFiles(directory=str(STATIC)), name="static")
templates = Jinja2Templates(directory=str(TEMPLATES))

# --------------------------------------------------------------------------- #
# Demo-mode helpers (single convention: RA_DEMO_MODE=1 AND a demo.db database)
# --------------------------------------------------------------------------- #
def _url_file_basename(url: str) -> str:
    """Best-effort basename of a sqlite:// URL (handles relative + absolute)."""
    path = url.split("sqlite:///", 1)[-1].split("?")[0]
    path = path.replace("\\", "/").rstrip("/")
    return path.rsplit("/", 1)[-1].lower()


def demo_db_active() -> bool:
    """True only when demo mode is on AND the app DB is the demo database."""
    return settings.demo_mode and _url_file_basename(settings.db_url) == "demo.db"


def _demo_guard() -> None:
    if not demo_db_active():
        raise HTTPException(
            status_code=403,
            detail=(
                "Demo actions (tamper/reset) require RA_DEMO_MODE=1 and the demo "
                "database (RA_DB_URL ending in data/demo.db)."
            ),
        )


def _flash(url: str, msg: str) -> RedirectResponse:
    return RedirectResponse(url=f"{url}?msg={quote(msg)}", status_code=303)


_APPROVAL_CFG = AgentConfig(
    mode=C.MODE_SUPERVISED,
    llm_mode="auto",
    offline_llm=True,
    provider=SeededSandbox(),
)


# --------------------------------------------------------------------------- #
# Small view helpers
# --------------------------------------------------------------------------- #
def _all_cases(db: Session) -> list[RiskCase]:
    return list(
        db.scalars(
            select(RiskCase).options(
                selectinload(RiskCase.invoice).selectinload(Invoice.customer),
                selectinload(RiskCase.customer),
            )
        ).all()
    )


def _latest_diagnosis_by_case(db: Session) -> dict[int, Diagnosis]:
    out: dict[int, Diagnosis] = {}
    for d in db.scalars(select(Diagnosis).order_by(Diagnosis.id.asc())).all():
        out[d.case_id] = d  # last write wins => latest
    return out


def _latest_policy_decision(db: Session, case_ids: list[int]) -> dict[int, dict]:
    """Last audited policy verdict per case (payload of policy_decision)."""
    out: dict[int, dict] = {}
    if not case_ids:
        return out
    rows = db.scalars(
        select(AuditEvent)
        .where(AuditEvent.action == "policy_decision", AuditEvent.case_id.in_(case_ids))
        .order_by(AuditEvent.seq.asc())
    ).all()
    for ev in rows:
        if ev.case_id is not None:
            out[ev.case_id] = ev.payload or {}
    return out


def _verdict_for_case(case: RiskCase, decision_map: dict[int, dict]) -> str:
    payload = decision_map.get(case.id, {})
    verdict = payload.get("verdict") or ""
    if verdict:
        return verdict
    # Terminal states reached without a surviving decision payload.
    if case.state == C.STATE_RECOVERED:
        return "APPROVED"
    if case.state in (C.STATE_STOPPED_COMPLIANCE, C.STATE_STOPPED_FRAUD, C.STATE_STOPPED_RULE):
        return "BLOCKED"
    if case.state == C.STATE_ESCALATED:
        return "ESCALATE"
    if case.state == C.STATE_PENDING_APPROVAL:
        return "REQUIRES_APPROVAL"
    return ""


def _is_demo_case(case: RiskCase) -> bool:
    return bool((case.invoice.meta or {}).get("demo"))


def _is_simulated_case(case: RiskCase) -> bool:
    return bool((case.invoice.meta or {}).get("simulated"))


# --------------------------------------------------------------------------- #
# Overview
# --------------------------------------------------------------------------- #
def _overview_stats(db: Session) -> dict:
    cases = _all_cases(db)
    at_risk = [c for c in cases if c.state in (C.STATE_OPEN, C.STATE_PENDING_APPROVAL)]
    recovered = [c for c in cases if c.state == C.STATE_RECOVERED]
    pending = [c for c in cases if c.state == C.STATE_PENDING_APPROVAL]
    escalated = [c for c in cases if c.state == C.STATE_ESCALATED]
    stopped = [
        c
        for c in cases
        if c.state in (C.STATE_STOPPED_COMPLIANCE, C.STATE_STOPPED_FRAUD, C.STATE_STOPPED_RULE)
    ]
    open_cases = [c for c in cases if c.state == C.STATE_OPEN]
    return {
        "total_cases": len(cases),
        "at_risk_cases": len(at_risk),
        "revenue_at_risk_minor": sum(c.amount_at_risk_minor for c in at_risk),
        "recovered_cases": len(recovered),
        "recovered_minor": sum(c.recovered_amount_minor for c in cases),
        "open_count": len(open_cases),
        "pending_count": len(pending),
        "escalated_count": len(escalated),
        "hard_stop_count": len(stopped),
        "hard_stop_minor": sum(c.amount_at_risk_minor for c in stopped),
        "escalated_minor": sum(c.amount_at_risk_minor for c in escalated),
    }


def _stopped_reasons(db: Session) -> list[dict]:
    """Recent deterministic stops with their reason code (for safety strip)."""
    rows = db.scalars(
        select(Escalation).order_by(Escalation.id.desc()).limit(12)
    ).all()
    out = []
    for e in rows:
        case = db.get(RiskCase, e.case_id)
        out.append(
            {
                "case_id": e.case_id,
                "entity": case.entity_id if case else "?",
                "code": e.reason_code,
                "label": esc_label(e.reason_code),
                "status": e.status,
                "created_at": e.created_at,
            }
        )
    return out


# --------------------------------------------------------------------------- #
# Evaluation summary (from the SEPARATE report DB, settings.eval_db_url)
# --------------------------------------------------------------------------- #
_RUN_ID_RE = re.compile(r"^seed\d+-(receivablesai|naive_retry)-(rules|auto)$")

EVAL_AGENT_LABELS = {
    (AGENT_REAL, "rules"): "Rules-only",
    (AGENT_REAL, "auto"): "AI-enabled",
    (AGENT_NAIVE, "rules"): "Naive baseline",
}

_eval_state_lock = threading.Lock()
_eval_state = {"running": False, "started_at": "", "finished_at": "", "error": ""}


def _eval_db() -> Session:
    engine = make_engine(settings.eval_db_url)
    init_db(engine)
    return make_session(engine)


def _eval_runs() -> list[EvalRun]:
    db = _eval_db()
    try:
        return list(db.scalars(select(EvalRun).order_by(EvalRun.started_at.desc())).all())
    finally:
        db.close()


def _latest_eval_comparison() -> list[dict]:
    """Newest canonical run per (agent, llm_mode): the three-way comparison."""
    selected: dict[tuple[str, str], EvalRun] = {}
    for run in _eval_runs():
        if not _RUN_ID_RE.match(run.run_id):
            continue
        key = (run.agent, run.llm_mode)
        if key not in selected or (run.started_at or run.id) > (selected[key].started_at or 0):
            selected[key] = run
    out = []
    for (agent, mode), run in selected.items():
        label = EVAL_AGENT_LABELS.get((agent, mode), f"{agent}:{mode}")
        out.append(
            {
                "label": label,
                "agent": agent,
                "llm_mode": mode,
                "run_id": run.run_id,
                "metrics": run.metrics or {},
                "started_at": run.started_at,
            }
        )
    # stable presentation order: AI-enabled, rules-only, naive baseline
    order = {(AGENT_REAL, "auto"): 0, (AGENT_REAL, "rules"): 1, (AGENT_NAIVE, "rules"): 2}
    out.sort(key=lambda x: order.get((x["agent"], x["llm_mode"]), 9))
    return out


def _scenario_unstructured(seed: int) -> set[str]:
    """Which corpus scenarios carry unstructured messages (reuses the corpus)."""
    from app.eval.corpus import build_corpus

    return {cc["scenario"] for cc in build_corpus(int(seed)) if cc.get("messages")}


def _split_stats(run: EvalRun, unstructured: set[str]) -> dict:
    """Recovery stats for structured vs unstructured subsets of one run."""
    db = _eval_db()
    try:
        rows = list(
            db.scalars(
                select(EvalCase).where(EvalCase.run_id == run.run_id)
            ).all()
        )
    finally:
        db.close()

    def _summarize(rows_sub) -> dict:
        recoverable = [r for r in rows_sub if r.expected_recovery_minor > 0]
        recovered = [r for r in rows_sub if r.recovered_amount_minor > 0]
        exp = sum(r.expected_recovery_minor for r in rows_sub)
        rec = sum(r.recovered_amount_minor for r in rows_sub)
        executed = 0
        correct = 0
        no_action = [r for r in rows_sub if (r.ground_truth or {}).get("playbook") == "NO_ACTION"]
        false_acts = 0
        for r in rows_sub:
            gt_pb = (r.ground_truth or {}).get("playbook")
            for a in r.actions_taken or []:
                if a.get("status") not in ("SUCCEEDED", "FAILED", "EXECUTING"):
                    continue
                if gt_pb == "NO_ACTION":
                    false_acts += 1
                    continue
                executed += 1
                if a.get("playbook") == gt_pb:
                    correct += 1
        return {
            "cases": len(rows_sub),
            "recoverable": len(recoverable),
            "recovered": len(recovered),
            "rate": (len(recovered) / len(recoverable)) if recoverable else None,
            "money_minor": rec,
            "expected_minor": exp,
            "pct_of_expected": (100.0 * rec / exp) if exp else None,
            "executed_actions": executed,
            "precision": (correct / executed) if executed else None,
            "false_actions": false_acts,
            "negatives": len(no_action),
        }

    structured = [r for r in rows if r.scenario not in unstructured]
    unstructured_rows = [r for r in rows if r.scenario in unstructured]
    return {
        "structured": _summarize(structured),
        "unstructured": _summarize(unstructured_rows),
    }


# --------------------------------------------------------------------------- #
# Page routes
# --------------------------------------------------------------------------- #
@app.get("/", response_class=HTMLResponse)
def overview(request: Request, db: Session = Depends(get_db)):
    stats = _overview_stats(db)
    latest_diag = _latest_diagnosis_by_case(db)
    decision_map = _latest_policy_decision(db, [c.id for c in _all_cases(db)])

    recent_audit = list(
        db.scalars(select(AuditEvent).order_by(AuditEvent.seq.desc()).limit(8)).all()
    )
    events = [
        {
            "seq": e.seq,
            "actor": e.actor,
            "action": e.action,
            "label": event_action_label(e.action),
            "case_id": e.case_id,
            "created_at": e.created_at,
            "summary": _event_summary(e),
        }
        for e in reversed(recent_audit)
    ]
    chain_ok, broken, chain_msg = verify(db)
    stopped = _stopped_reasons(db)

    return templates.TemplateResponse(
        request,
        "overview.html",
        {
            "active": "overview",
            "stats": stats,
            "events": events,
            "chain_valid": chain_ok,
            "chain_message": chain_msg,
            "stopped": stopped,
            "eval_runs": _latest_eval_comparison(),
            "eval_db_file": Path(settings.eval_db_url.split("sqlite:///", 1)[-1]).name,
            "counts": {
                "open": stats["open_count"],
                "pending": stats["pending_count"],
                "escalated": stats["escalated_count"],
                "stopped": stats["hard_stop_count"],
                "recovered": stats["recovered_cases"],
            },
        },
    )


def _event_summary(e: AuditEvent) -> str:
    p = e.payload or {}
    case = p.get("case") or p.get("invoice") or p.get("event_id") or ""
    parts = [str(v) for k, v in list(p.items())[:3] if k not in ("case", "invoice") and v not in (None, "")]
    if case:
        return f"{case}" + (f" · {' · '.join(parts)}" if parts else "")
    return " · ".join(parts)


# --------------------------------------------------------------------------- #
# Recovery queue
# --------------------------------------------------------------------------- #
QUEUE_FILTERS = {
    "all": "All cases",
    "action_needed": "Action needed",
    "pending_approval": "Pending approval",
    "recovered": "Recovered",
    "escalated": "Escalated",
    "blocked": "Blocked",
}


@app.get("/cases", response_class=HTMLResponse)
def queue(request: Request, filter: str = "all", db: Session = Depends(get_db)):
    if filter not in QUEUE_FILTERS:
        raise HTTPException(status_code=404, detail="unknown filter")

    cases = _all_cases(db)
    diag_map = _latest_diagnosis_by_case(db)
    decision_map = _latest_policy_decision(db, [c.id for c in cases])

    rows = []
    for case in sorted(cases, key=lambda c: (c.state != C.STATE_OPEN, c.id)):
        d = diag_map.get(case.id)
        cause = d.cause if d else case.cause
        path = d.path if d else case.diagnosis_path
        proposed_pb = d.proposed_playbook if d else ""
        verdict = _verdict_for_case(case, decision_map)
        invoice = case.invoice
        cust = invoice.customer if invoice else case.customer

        matched = True
        if filter == "action_needed":
            matched = case.state == C.STATE_OPEN
        elif filter == "pending_approval":
            matched = case.state == C.STATE_PENDING_APPROVAL
        elif filter == "recovered":
            matched = case.state == C.STATE_RECOVERED
        elif filter == "escalated":
            matched = case.state == C.STATE_ESCALATED
        elif filter == "blocked":
            matched = case.state in (
                C.STATE_STOPPED_COMPLIANCE,
                C.STATE_STOPPED_FRAUD,
                C.STATE_STOPPED_RULE,
            )
        if not matched:
            continue

        rows.append(
            {
                "id": case.id,
                "entity_id": case.entity_id,
                "org": cust.org_name if cust else "?",
                "invoice_id": invoice.rzr_invoice_id if invoice else case.entity_id,
                "amount_minor": case.amount_at_risk_minor,
                "recovered_minor": case.recovered_amount_minor,
                "days_overdue": case.days_overdue,
                "risk_score": case.risk_score,
                "cause": cause_label(cause),
                "cause_code": cause,
                "confidence": d.confidence if d else None,
                "path": PATH_LABELS.get(path, path),
                "proposed_playbook": playbook_label(proposed_pb) if proposed_pb else "",
                "verdict": verdict,
                "state": case.state,
                "state_label": STATE_LABELS.get(case.state, case.state),
                "state_class": STATE_CLASS.get(case.state, "muted"),
                "opened_at": case.opened_at,
                "demo": _is_demo_case(case),
                "simulated": _is_simulated_case(case),
            }
        )

    counts = {f: 0 for f in QUEUE_FILTERS}
    for case in cases:
        if case.state == C.STATE_OPEN:
            counts["action_needed"] += 1
        elif case.state == C.STATE_PENDING_APPROVAL:
            counts["pending_approval"] += 1
        elif case.state == C.STATE_RECOVERED:
            counts["recovered"] += 1
        elif case.state == C.STATE_ESCALATED:
            counts["escalated"] += 1
        elif case.state in (C.STATE_STOPPED_COMPLIANCE, C.STATE_STOPPED_FRAUD, C.STATE_STOPPED_RULE):
            counts["blocked"] += 1
    counts["all"] = len(cases)

    return templates.TemplateResponse(
        request,
        "cases.html",
        {
            "active": "cases",
            "rows": rows,
            "current_filter": filter,
            "filters": QUEUE_FILTERS,
            "counts": counts,
        },
    )


# --------------------------------------------------------------------------- #
# Case detail
# --------------------------------------------------------------------------- #
@app.get("/cases/{case_id}", response_class=HTMLResponse)
def case_detail(case_id: int, request: Request, db: Session = Depends(get_db)):
    case = db.get(RiskCase, case_id)
    if case is None:
        raise HTTPException(status_code=404, detail="case not found")
    invoice = db.get(Invoice, case.invoice_id)
    customer = db.get(Customer, case.customer_id)

    flags = list(
        db.scalars(select(ComplianceFlag).where(ComplianceFlag.customer_id == customer.id)).all()
    ) if customer else []
    payments = list(
        db.scalars(
            select(Payment).where(Payment.invoice_id == invoice.id).order_by(Payment.attempted_at.desc())
        ).all()
    ) if invoice else []
    messages = list(
        db.scalars(
            select(CaseMessage).where(CaseMessage.invoice_id == invoice.id).order_by(CaseMessage.received_at.asc())
        ).all()
    ) if invoice else []
    diagnoses = list(
        db.scalars(select(Diagnosis).where(Diagnosis.case_id == case.id).order_by(Diagnosis.id.asc())).all()
    )
    interventions = list(
        db.scalars(
            select(Intervention).where(Intervention.case_id == case.id).order_by(Intervention.id.asc())
        ).all()
    )
    approvals = {
        a.intervention_id: a
        for a in db.scalars(select(Approval).order_by(Approval.id.asc())).all()
        if a.intervention_id in {i.id for i in interventions}
    }
    escalations = list(
        db.scalars(select(Escalation).where(Escalation.case_id == case.id).order_by(Escalation.id.asc())).all()
    )
    promises = list(
        db.scalars(select(PTPPromise).where(PTPPromise.case_id == case.id)).all()
    )
    events = case_events(db, case.id)

    latest_d = diagnoses[-1] if diagnoses else None

    # chain integrity (global hash chain; per-case view highlights a broken row)
    chain_ok, broken, chain_msg = verify(db)
    chain = {
        "valid": chain_ok,
        "message": chain_msg,
        "broken_event_id": broken.id if broken else None,
        "broken_on_case": bool(broken and broken.case_id == case.id),
        "broken_action": broken.action if broken else "",
    }

    # the diagnosis pipeline strip: AI PROPOSES -> POLICY AUTHORIZES -> EXECUTES
    proposal = None
    if latest_d:
        proposal = {
            "cause": latest_d.cause,
            "cause_label": cause_label(latest_d.cause),
            "confidence": latest_d.confidence,
            "path": PATH_LABELS.get(latest_d.path, latest_d.path),
            "flagged": latest_d.flagged,
            "llm_ok": latest_d.llm_ok,
            "proposed_playbook": latest_d.proposed_playbook,
            "proposed_playbook_label": playbook_label(latest_d.proposed_playbook),
            "evidence_rule": latest_d.evidence,
            "evidence_llm": (latest_d.llm_json or {}).get("evidence", []),
            "rationale": (latest_d.llm_json or {}).get("rationale", ""),
            "rule_json": latest_d.rule_json,
            "llm_json": latest_d.llm_json,
            "promised_date_iso": (latest_d.llm_json or {}).get("promised_date_iso", ""),
        }
    latest_inter = interventions[-1] if interventions else None
    decision_payload = _latest_policy_decision(db, [case.id]).get(case.id, {})

    return templates.TemplateResponse(
        request,
        "case_detail.html",
        {
            "active": "cases",
            "case": case,
            "invoice": invoice,
            "customer": customer,
            "flags": flags,
            "payments": payments,
            "messages": messages,
            "diagnoses": diagnoses,
            "latest_diagnosis": latest_d,
            "proposal": proposal,
            "interventions": interventions,
            "approvals": approvals,
            "escalations": escalations,
            "promises": promises,
            "events": events,
            "decision_payload": decision_payload,
            "latest_intervention": latest_inter,
            "verdict": _verdict_for_case(case, {case.id: decision_payload}),
            "chain": chain,
            "demo": _is_demo_case(case),
            "simulated": _is_simulated_case(case),
        },
    )


# --------------------------------------------------------------------------- #
# Approvals (read) + decide (thin POST calling the existing mechanism)
# --------------------------------------------------------------------------- #
def _approval_rows(db: Session) -> tuple[list[dict], list[dict]]:
    """Build the pending + recent approval rows shared by the GET page and the
    decline-rejection re-render (keeps one source of truth for the template)."""
    pending = list(
        db.scalars(
            select(Intervention)
            .where(Intervention.status == "PENDING_APPROVAL")
            .order_by(Intervention.created_at.asc())
        ).all()
    )
    pending_rows = []
    for i in pending:
        case = db.get(RiskCase, i.case_id)
        invoice = db.get(Invoice, case.invoice_id) if case else None
        cust = db.get(Customer, case.customer_id) if case else None
        d = db.scalar(
            select(Diagnosis)
            .where(Diagnosis.case_id == i.case_id)
            .order_by(Diagnosis.id.desc())
            .limit(1)
        )
        pending_rows.append(
            {
                "intervention": i,
                "case": case,
                "invoice": invoice,
                "customer": cust,
                "diagnosis": d,
                "reason_hits": [
                    {"code": r.get("code", ""), "message": r.get("message", "")}
                    for r in (i.policy_reasons or [])
                ],
            }
        )

    recent = list(
        db.scalars(select(Approval).order_by(Approval.id.desc()).limit(12)).all()
    )
    recent_rows = []
    for a in recent:
        i = db.get(Intervention, a.intervention_id)
        case = db.get(RiskCase, i.case_id) if i else None
        recent_rows.append(
            {
                "approval": a,
                "intervention": i,
                "entity": case.entity_id if case else "?",
                "case_id": case.id if case else None,
            }
        )
    return pending_rows, recent_rows


@app.get("/approvals", response_class=HTMLResponse)
def approvals(request: Request, db: Session = Depends(get_db)):
    pending_rows, recent_rows = _approval_rows(db)
    return templates.TemplateResponse(
        request,
        "approvals.html",
        {
            "active": "approvals",
            "pending_rows": pending_rows,
            "recent_rows": recent_rows,
        },
    )


@app.post("/approvals/{intervention_id}/decide")
def approval_decision(
    intervention_id: int,
    request: Request,
    decision: str = Form(...),
    reason: str = Form(""),
    actor: str = Form("finance-lead@receivablesai.demo"),
    next: str = Form("/approvals"),
    db: Session = Depends(get_db),
):
    decision = decision.upper()
    if decision not in ("APPROVED", "DECLINED"):
        raise HTTPException(status_code=400, detail="decision must be APPROVED or DECLINED")
    if not next.startswith(("/approvals", "/cases/")):
        next = "/approvals"

    if decision == "DECLINED" and not (reason or "").strip():
        # Reject cleanly: re-render the approvals inbox with an inline message,
        # leaving the intervention PENDING_APPROVAL. Never a bare 400/422 page.
        pending_rows, recent_rows = _approval_rows(db)
        return templates.TemplateResponse(
            request,
            "approvals.html",
            {
                "active": "approvals",
                "pending_rows": pending_rows,
                "recent_rows": recent_rows,
                "decline_error": {
                    "intervention_id": intervention_id,
                    "message": (
                        "Decline requires a reason — the intervention is still pending. "
                        "Enter why this action should not run (it is recorded in the audit trail) and try again."
                    ),
                },
            },
        )

    res = decide_approval(
        db,
        intervention_id,
        decision=decision,
        actor=(actor or "finance-lead@receivablesai.demo")[:80],
        reason=(reason or "")[:400],
        cfg=_APPROVAL_CFG,
    )
    db.commit()
    if not res.get("ok"):
        raise HTTPException(status_code=400, detail=res.get("error", "decision failed"))

    verb = "approved" if decision == "APPROVED" else "declined"
    msg = f"Intervention #{intervention_id} {verb} via decide_approval() — status {res.get('status')}"
    return _flash(next, msg)


# --------------------------------------------------------------------------- #
# Evaluation
# --------------------------------------------------------------------------- #
@app.get("/eval", response_class=HTMLResponse)
def evaluation(request: Request):
    runs = _eval_runs()
    comparison = _latest_eval_comparison()
    splits = {}
    for item in comparison:
        run = next((r for r in runs if r.run_id == item["run_id"]), None)
        if run is not None:
            splits[item["run_id"]] = _split_stats(run, _scenario_unstructured(run.seed))

    history = [
        {
            "run_id": r.run_id,
            "agent": r.agent,
            "llm_mode": r.llm_mode,
            "started_at": r.started_at,
            "metrics": r.metrics or {},
            "canonical": bool(_RUN_ID_RE.match(r.run_id)),
        }
        for r in runs
    ]

    with _eval_state_lock:
        state = dict(_eval_state)

    return templates.TemplateResponse(
        request,
        "evaluation.html",
        {
            "active": "eval",
            "comparison": comparison,
            "splits": splits,
            "history": history,
            "run_state": state,
            "db_file": Path(settings.eval_db_url.split("sqlite:///", 1)[-1]).name,
        },
    )


@app.get("/eval/status", response_class=HTMLResponse)
def evaluation_status(request: Request):
    """Small status fragment polled by HTMX on the evaluation page.

    Renders the same ``_eval_status.html`` partial the page includes server-side.
    While the batch runs, the fragment keeps its hx-get/hx-trigger attributes so the
    browser polls every ~2s; once the batch completes or fails the fragment carries no
    polling attributes and HTMX stops after the next swap — no reload, no loop.
    """
    with _eval_state_lock:
        state = dict(_eval_state)
    return templates.TemplateResponse(request, "_eval_status.html", {"run_state": state})


def _eval_worker(seed: int) -> None:
    try:
        db = _eval_db()
        try:
            run_evaluation(db, seed=seed, llm_mode="rules", agents=(AGENT_REAL, AGENT_NAIVE))
            run_evaluation(db, seed=seed, llm_mode="auto", agents=(AGENT_REAL,))
        finally:
            db.close()
        with _eval_state_lock:
            _eval_state["running"] = False
            _eval_state["finished_at"] = fmt_dt(clock_now())
            _eval_state["error"] = ""
    except Exception as exc:  # noqa: BLE001 - surfaced on the page
        with _eval_state_lock:
            _eval_state["running"] = False
            _eval_state["finished_at"] = fmt_dt(clock_now())
            _eval_state["error"] = f"{type(exc).__name__}: {exc}"


@app.post("/eval/run")
def evaluation_run():
    with _eval_state_lock:
        if _eval_state["running"]:
            return _flash("/eval", "A batch is already running — the status indicator updates automatically.")
        _eval_state.update(
            {"running": True, "started_at": fmt_dt(clock_now()), "error": ""}
        )
    seed = int(settings.eval_seed)
    threading.Thread(target=_eval_worker, args=(seed,), daemon=True).start()
    return _flash("/eval", "Evaluation started — the status indicator updates automatically as the batch runs.")


# --------------------------------------------------------------------------- #
# Audit + demo tamper/reset
# --------------------------------------------------------------------------- #
@app.get("/audit", response_class=HTMLResponse)
def audit(request: Request, db: Session = Depends(get_db)):
    ok, broken, msg = verify(db)
    total = db.scalar(select(func.count(AuditEvent.id))) or 0
    rows = list(db.scalars(select(AuditEvent).order_by(AuditEvent.seq.desc()).limit(150)).all())
    events = []
    for e in reversed(rows):
        case = db.get(RiskCase, e.case_id) if e.case_id else None
        events.append(
            {
                "id": e.id,
                "seq": e.seq,
                "actor": e.actor,
                "action": e.action,
                "label": event_action_label(e.action),
                "case_id": e.case_id,
                "entity": case.entity_id if case else "",
                "created_at": e.created_at,
                "payload": e.payload or {},
                "hash_short": e.hash[:10],
                "prev_short": (e.prev_hash[:10] if e.prev_hash != "GENESIS" else "genesis"),
                "broken": bool(broken and broken.id == e.id),
            }
        )
    return templates.TemplateResponse(
        request,
        "audit.html",
        {
            "active": "audit",
            "chain_valid": ok,
            "chain_message": msg,
            "chain_total": total,
            "broken": (
                {
                    "id": broken.id,
                    "seq": broken.seq,
                    "action": broken.action,
                    "case_id": broken.case_id,
                }
                if broken
                else None
            ),
            "events": events,
            "db_file": Path(settings.db_url.split("sqlite:///", 1)[-1]).name,
        },
    )


@app.post("/demo/tamper")
def demo_tamper(db: Session = Depends(get_db)):
    """DEMO ONLY: modify exactly one audit payload (hash untouched) so the
    EXISTING chain verifier reports VALID -> INVALID and names the event."""
    _demo_guard()
    ok, broken, _msg = verify(db)
    if not ok:
        raise HTTPException(
            status_code=409,
            detail=f"chain already invalid at audit event #{broken.id if broken else '?'} — reset the demo to restore it",
        )

    victim = db.scalar(
        select(AuditEvent)
        .where(AuditEvent.action == "payment_credited", AuditEvent.case_id.isnot(None))
        .order_by(AuditEvent.seq.asc())
        .limit(1)
    )
    if victim is None:
        victim = db.scalar(select(AuditEvent).order_by(AuditEvent.seq.asc()).limit(1))
    if victim is None:
        raise HTTPException(status_code=409, detail="no audit events to tamper")

    payload = dict(victim.payload or {})
    amount = payload.get("amount_minor")
    if isinstance(amount, int):
        payload["amount_minor"] = amount + 1
    payload["tamper_demo"] = True
    payload["tamper_note"] = "modified by DEMO tamper test — hash intentionally left stale"

    victim.payload = payload
    victim.canonical = canonical_json(payload)  # recomputed text; hash NOT updated
    db.commit()

    ok2, broken2, msg2 = verify(db)
    return _flash(
        "/audit",
        f"Tampered audit event #{victim.id} (seq {victim.seq}, {victim.action}). "
        f"Verify Integrity now: {'VALID' if ok2 else 'INVALID'} — {msg2}",
    )


@app.post("/demo/reset")
def demo_reset():
    """DEMO ONLY: re-seed by invoking the EXISTING seeder as a subprocess."""
    _demo_guard()
    url = settings.db_url
    if _url_file_basename(url) != "demo.db":
        raise HTTPException(status_code=403, detail="reset requires the demo database (demo.db)")

    try:
        proc = subprocess.run(
            [sys.executable, "-m", "scripts.seed_demo", "--db-url", url],
            cwd=str(BACKEND_DIR),
            capture_output=True,
            text=True,
            timeout=600,
        )
    except subprocess.TimeoutExpired:
        raise HTTPException(status_code=500, detail="demo reset timed out")
    if proc.returncode != 0:
        tail = (proc.stderr or proc.stdout or "")[-600:]
        raise HTTPException(status_code=500, detail=f"seed_demo failed: {tail}")

    last = "\n".join((proc.stdout or "").strip().splitlines()[-4:])
    return _flash("/audit", f"Demo data reset OK (subprocess seed_demo). {last}")


# --------------------------------------------------------------------------- #
# Template globals (resolved at render time; display-only helpers)
# --------------------------------------------------------------------------- #
def _pp_json(d) -> str:
    try:
        return json.dumps(d or {}, indent=1, ensure_ascii=False, default=str)
    except Exception:
        return str(d)


import app.presentation as _P  # noqa: E402  (registered as template namespace)

templates.env.globals["P"] = _P
templates.env.globals["demo_active"] = demo_db_active
templates.env.globals["db_file_name"] = lambda: _url_file_basename(settings.db_url)
templates.env.globals["pp_json"] = _pp_json
templates.env.globals["playbook_label"] = playbook_label
templates.env.globals["action_label"] = action_label
