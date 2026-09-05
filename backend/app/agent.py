"""Agent orchestrator — the closed loop.

    DETECT -> DIAGNOSE -> DECIDE -> EXECUTE -> OBSERVE -> AUDIT

Everything is synchronous and side-effect-safe:
  * Events are ingested idempotently (event_id uniqueness).
  * Actions carry idempotency keys and are executed exactly once.
  * The clock is swappable (app.clock) so eval/demo run on virtual time.
  * Mode is per-context: AUTONOMOUS executes within policy; SUPERVISED parks
    every action as PENDING_APPROVAL until a human (or timeout) decides.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import timedelta
from typing import Iterable

from sqlalchemy import select
from sqlalchemy.orm import Session

from app import constants as C
from app.audit import audit_event
from app.clock import now
from app.config import Settings, settings as global_settings
from app.detection import (  # noqa: F401  (re-exported for convenience)
    credit_received,
    escalate,
    find_case_for_invoice,
    open_or_refresh_case,
    set_state,
)
from app.execution.executors import build_idempotency_key, execute_action, record_promise
from app.execution.provider import PaymentProvider
from app.execution.razorpay_client import RazorpayClient
from app.execution.sandbox import SeededSandbox
from app.ingestion import apply_event_to_entities, is_money_event, store_event
from app.models import (
    Approval,
    Customer,
    Diagnosis,
    Intervention,
    Invoice,
    Payment,
    PTPPromise,
    RiskCase,
    RiskEvent,
)
from app.policy import engine as policy_engine
from app.policy.playbooks import PLAYBOOKS, executed_count


@dataclass
class AgentConfig:
    mode: str = global_settings.default_mode
    llm_mode: str = global_settings.llm_mode  # "rules" | "auto"
    settings: Settings = field(default_factory=lambda: global_settings)
    provider: PaymentProvider = field(default_factory=SeededSandbox)
    label: str = "receivablesai"

    # Hooks used by the evaluation baselines: a non-engine plan function and
    # flags that disable the scheduler/risk-gate so naive agents behave naively.
    plan_fn: callable | None = None        # (db, case) -> ActionPlan (bypasses engine)
    schedule_next: bool = True             # schedule follow-up ladder steps
    defer_ptp: bool = True                 # respect active promises (wait)
    risk_gate: bool = True                 # observe-only below risk threshold
    record_violations: bool = True         # eval audits actions against policy rules

    # Explicitly route llm_mode="auto" to the deterministic OFFLINE NLU
    # diagnoser when no API key is configured. Production never sets this — it
    # exists so the evaluation can compare rules-only vs AI-enabled without a
    # network model. See app/diagnosis/offline.py (honesty contract).
    offline_llm: bool = False


def make_provider(*, force_sandbox: bool = False) -> PaymentProvider:
    if not force_sandbox and RazorpayClient and global_settings.razorpay_key_id:
        try:
            return RazorpayClient()
        except Exception:
            pass
    return SeededSandbox()


# --------------------------------------------------------------------------- #
# Diagnosis (rules, optional LLM, fusion)
# --------------------------------------------------------------------------- #
def diagnose_case(db: Session, case: RiskCase, cfg: AgentConfig) -> dict:
    from app.diagnosis.context_builder import build_snapshot
    from app.diagnosis.fusion import fuse
    from app.diagnosis.llm_diagnoser import diagnose_with_llm
    from app.diagnosis.rules_classifier import classify
    from app.policy.playbooks import CAUSE_TO_PLAYBOOK

    rule = classify(db, case)
    rule_dict = rule.as_dict()
    rule_dict["proposed_playbook"] = CAUSE_TO_PLAYBOOK.get(rule.cause, "")

    llm_dict = None
    llm_ok = False
    has_key = bool(cfg.settings.openai_api_key)
    use_offline = cfg.llm_mode == "auto" and not has_key and cfg.offline_llm
    llm_enabled = cfg.llm_mode == "auto" and (has_key or use_offline)
    if llm_enabled:
        snapshot = build_snapshot(db, case)
        if use_offline:
            from app.diagnosis.offline import offline_diagnose

            llm = offline_diagnose(snapshot)
        else:
            llm = diagnose_with_llm(
                snapshot,
                api_key=cfg.settings.openai_api_key,
                model=cfg.settings.llm_model,
                timeout=cfg.settings.llm_timeout_seconds,
            )
        if llm is not None:
            llm_dict = llm.as_dict()
            llm_ok = True
            audit_event(
                db,
                actor=C.ACTOR_AGENT_LLM,
                action="llm_diagnosis",
                payload={"case": case.case_key, "diagnosis": llm_dict},
                case_id=case.id,
            )
        else:
            audit_event(
                db,
                actor=C.ACTOR_AGENT_LLM,
                action="llm_unavailable_rules_fallback",
                payload={"case": case.case_key, "flag": C.DIAG_FLAG_RULES},
                case_id=case.id,
            )

    fused = fuse(rule_dict, llm_dict, llm_enabled=llm_enabled)

    # An LLM-only diagnosis below the autonomous band is never acted on: route
    # it to the same escalate path as an abstention (policy engine decides).
    diagnosis_params: dict = {}
    if fused.path == C.DIAG_LLM and fused.flagged:
        fused.cause = C.CAUSE_INSUFFICIENT_CONTEXT
        fused.proposed_playbook = ""
    if fused.cause == C.CAUSE_PTP_OFFERED and (llm_dict or {}).get("promised_date_iso"):
        diagnosis_params["promised_date_iso"] = llm_dict["promised_date_iso"]

    row = Diagnosis(
        case_id=case.id,
        cause=fused.cause,
        confidence=fused.confidence,
        path=fused.path,
        llm_ok=llm_ok,
        flagged=fused.flagged,
        evidence=fused.rule or {},
        rule_json=rule_dict,
        llm_json=llm_dict or {},
        proposed_playbook=fused.proposed_playbook,
    )
    db.add(row)
    db.flush()

    audit_event(
        db,
        actor=C.ACTOR_POLICY if not llm_enabled else C.ACTOR_AGENT_LLM,
        action="diagnosis_fused",
        payload={
            "case": case.case_key,
            "cause": fused.cause,
            "confidence": round(fused.confidence, 3),
            "path": fused.path,
            "flagged": fused.flagged,
            "reason": fused.reason,
            "proposed_playbook": fused.proposed_playbook,
        },
        case_id=case.id,
    )

    # persist what the agent believes on the case row itself (queue UX)
    case.cause = fused.cause
    case.diagnosis_path = fused.path
    if fused.flagged:
        case.reason = f"low_confidence:{fused.reason}"
    return fused.__dict__ | {"rule": rule_dict, "llm": llm_dict, "diagnosis_params": diagnosis_params}


# --------------------------------------------------------------------------- #
# Stepping a case through one full pass
# --------------------------------------------------------------------------- #
def step_case(db: Session, case: RiskCase, cfg: AgentConfig) -> str:
    """One DETECT->DIAGNOSE->DECIDE->EXECUTE->OBSERVE pass. Returns a tag."""
    if case.state in C.TERMINAL_STATES:
        return "terminal"

    invoice = db.get(Invoice, case.invoice_id)
    if invoice is None:
        return "no_invoice"

    # refresh risk posture
    from app.detection import days_overdue_for

    case.amount_at_risk_minor = invoice.outstanding_minor
    case.days_overdue = days_overdue_for(invoice)

    if invoice.is_paid:
        executed = db.scalar(
            select(Intervention).where(Intervention.case_id == case.id).limit(1)
        )
        if case.recovered_amount_minor > 0 or executed is not None:
            set_state(db, case, C.STATE_RECOVERED, reason=f"invoice {invoice.rzr_invoice_id} paid")
        else:
            set_state(db, case, C.STATE_CLOSED_NOOP, reason="invoice paid before agent action (self-healed)")
        return "recovered" if case.recovered_amount_minor > 0 else "self_healed"

    # ---- PTP deferral: customer already promised a future date -------------- #
    active_promise = db.scalar(
        select(PTPPromise).where(
            PTPPromise.case_id == case.id, PTPPromise.status == "active"
        ).order_by(PTPPromise.id.desc())
    )
    if cfg.defer_ptp and active_promise is not None and active_promise.promised_date > now():
        defer_to = active_promise.promised_date + timedelta(hours=24)
        case.next_action_at = defer_to
        audit_event(
            db,
            actor=C.ACTOR_SYSTEM,
            action="deferred_for_promise",
            payload={
                "case": case.case_key,
                "promised_date": active_promise.promised_date.isoformat(),
                "next_action_at": defer_to.isoformat(),
            },
            case_id=case.id,
        )
        return "deferred_ptp"

    # ---- diagnose ------------------------------------------------------------- #
    fused = diagnose_case(db, case, cfg)

    # low-risk watch gate: queue but do not act (NO_ACTION by design)
    if cfg.risk_gate and case.risk_score < cfg.settings.action_risk_min and not _has_executed(db, case.id):
        audit_event(
            db,
            actor=C.ACTOR_POLICY,
            action="below_action_threshold_no_action",
            payload={"case": case.case_key, "risk_score": case.risk_score, "threshold": cfg.settings.action_risk_min},
            case_id=case.id,
        )
        return "watch"

    # ---- decide ---------------------------------------------------------------- #
    if cfg.plan_fn is not None:
        # Baseline agents bypass the policy engine (that is the point of the
        # comparison); the evaluator audits their actions afterwards.
        plan = cfg.plan_fn(db, case, now())
    else:
        # (policy engine is the only authorizer for the real agent)
        plan = policy_engine.plan_action(
            db,
            case,
            diagnosis_cause=fused.get("cause", C.CAUSE_UNKNOWN),
            proposed_playbook=fused.get("proposed_playbook", ""),
            mode=cfg.mode,
            settings=cfg.settings,
            now=now(),
            diagnosis_params=fused.get("diagnosis_params") or {},
        )
    audit_event(
        db,
        actor=C.ACTOR_POLICY,
        action="policy_decision",
        payload={
            "case": case.case_key,
            "verdict": plan.verdict,
            "playbook": plan.playbook,
            "action": plan.action,
            "reasons": plan.reasons,
            "escalate_code": plan.escalate_code,
            "defer_to": plan.defer_to.isoformat() if plan.defer_to else None,
            "policy_override": plan.policy_override,
        },
        case_id=case.id,
    )

    if plan.verdict == C.VERDICT_BLOCKED:
        _terminal_block(db, case, plan)
        return "blocked:" + (plan.stop_state or "unknown")

    if plan.verdict == "ESCALATE":
        escalate(
            db,
            case,
            plan.escalate_code or C.ESC_LOW_CONFIDENCE,
            context={"cause": fused.get("cause"), "playbook": plan.playbook, "reasons": plan.reasons},
        )
        return "escalated"

    if plan.verdict == C.VERDICT_DEFERRED:
        case.next_action_at = plan.defer_to
        audit_event(
            db,
            actor=C.ACTOR_POLICY,
            action="action_deferred",
            payload={"case": case.case_key, "next_action_at": plan.defer_to.isoformat(), "reasons": plan.reasons},
            case_id=case.id,
        )
        return "deferred"

    if plan.verdict == C.VERDICT_REQUIRES_APPROVAL:
        _park_for_approval(db, case, plan, cfg)
        return "pending_approval"

    # ---- execute ---------------------------------------------------------------- #
    intervention = execute_action(db, case, plan, cfg.provider, mode=cfg.mode, now_dt=now())
    if intervention is None:
        return "not_whitelisted"

    _observe_execution(db, case, intervention, cfg)
    return intervention.status.lower()


def _has_executed(db: Session, case_id: int) -> bool:
    return (
        db.scalar(
            select(Intervention.id)
            .where(Intervention.case_id == case_id, Intervention.status.in_(["SUCCEEDED", "FAILED", "EXECUTING"]))
            .limit(1)
        )
        is not None
    )


def _terminal_block(db: Session, case: RiskCase, plan) -> None:
    from app.models import Escalation

    db.add(Escalation(case_id=case.id, reason_code=plan.escalate_code, context={"reasons": plan.reasons}, status="open"))
    set_state(db, case, plan.stop_state or C.STATE_STOPPED_RULE, reason=plan.escalate_code or "policy_block")


def _park_for_approval(db: Session, case: RiskCase, plan, cfg: AgentConfig) -> None:
    # cancel older pending approvals for the same case (single pending action)
    for old in db.scalars(
        select(Intervention).where(
            Intervention.case_id == case.id, Intervention.status == "PENDING_APPROVAL"
        )
    ).all():
        old.status = "CANCELLED"
    key = build_idempotency_key(case, plan)
    intervention = Intervention(
        case_id=case.id,
        idempotency_key=key,
        playbook=plan.playbook,
        action=plan.action,
        params=plan.params or {},
        status="PENDING_APPROVAL",
        mode=cfg.mode,
        verdict=C.VERDICT_REQUIRES_APPROVAL,
        policy_reasons=plan.reasons,
        created_at=now(),
    )
    db.add(intervention)
    db.flush()
    audit_event(
        db,
        actor=C.ACTOR_POLICY,
        action="approval_requested",
        payload={
            "case": case.case_key,
            "action": plan.action,
            "playbook": plan.playbook,
            "params": plan.params,
            "reasons": plan.reasons,
        },
        case_id=case.id,
    )
    set_state(db, case, C.STATE_PENDING_APPROVAL, reason="awaiting human approval")
    case.next_action_at = None


def _observe_execution(db: Session, case: RiskCase, intervention: Intervention, cfg: AgentConfig) -> None:
    outcome = intervention.outcome or {}
    pending = outcome.get("pending_payment")

    if intervention.status == "SUCCEEDED":
        if pending:
            offset = float(pending.get("offset_hours", 0))
            if offset <= 0:
                _receive_money_now(db, case, pending, intervention)
            # money arriving later is drained by the tick/harness
        elif not cfg.schedule_next:
            pass  # naive baseline: single-shot action, no ladder continuation
        else:
            # dispatch succeeded but no money hint => schedule the next ladder
            # step, or escalate once the ladder is exhausted
            meta = PLAYBOOKS.get(intervention.playbook, {})
            done = executed_count(db, case.id, intervention.playbook) >= meta.get("max_attempts", 99)
            if done:
                escalate(
                    db,
                    case,
                    meta.get("exhaust_reason", C.ESC_LADDER_EXHAUSTED),
                    context={"action": intervention.action, "playbook": intervention.playbook},
                )
            else:
                cooldown = meta.get("cooldown_hours", 24)
                case.next_action_at = now() + timedelta(hours=cooldown)
                audit_event(
                    db,
                    actor=C.ACTOR_SYSTEM,
                    action="next_ladder_step_scheduled",
                    payload={"case": case.case_key, "next_action_at": case.next_action_at.isoformat(), "playbook": intervention.playbook},
                    case_id=case.id,
                )
    elif intervention.status == "FAILED":
        # failed retries (e.g. still insufficient funds) are retried once more
        # at cooldown distance; otherwise the ladder is exhausted.
        meta = PLAYBOOKS.get(intervention.playbook, {})
        done = executed_count(db, case.id, intervention.playbook) >= meta.get("max_attempts", 99)
        if done:
            escalate(
                db,
                case,
                meta.get("exhaust_reason", C.ESC_LADDER_EXHAUSTED),
                context={"action": intervention.action, "failure_code": outcome.get("failure_code")},
            )
        elif not cfg.schedule_next:
            pass  # naive baseline: one retry only
        else:
            case.next_action_at = now() + timedelta(hours=meta.get("cooldown_hours", 24))
            audit_event(
                db,
                actor=C.ACTOR_SYSTEM,
                action="retry_scheduled_after_failure",
                payload={"case": case.case_key, "next_action_at": case.next_action_at.isoformat()},
                case_id=case.id,
            )


def _receive_money_now(db: Session, case: RiskCase, pending: dict, intervention: Intervention) -> None:
    """Synthesize a money-received event for an immediately settled payment."""
    event = {
        "event_id": pending.get("event_id") or f"sim:{case.case_key}:{now().isoformat()}",
        "event_type": pending.get("event_type", "invoice.paid"),
        "rzr_invoice_id": pending.get("invoice_rzr_id") or case.entity_id,
        "rzr_payment_id": pending.get("payment_id", ""),
        "amount_minor": int(pending.get("amount_minor", 0)),
        "method": "upi",
        "source": "sim",
        "verified": True,
    }
    is_new, _row = store_event(db, event)
    if is_new:
        apply_event_to_entities(db, event)
    # mark the hint consumed so milestone loops settle
    _mark_pending_applied(intervention)
    db.flush()


# --------------------------------------------------------------------------- #
# Batch ingestion + scheduled work
# --------------------------------------------------------------------------- #
def ingest_events(db: Session, events: Iterable[dict], cfg: AgentConfig) -> dict:
    """Idempotently ingest a batch of events and step affected cases."""
    stats = {"events": 0, "new": 0, "duplicates": 0, "cases_stepped": 0}
    touched_invoice_ids: set[int] = set()
    for ev in events:
        stats["events"] += 1
        is_new, row = store_event(db, ev)
        if not is_new:
            stats["duplicates"] += 1
            continue
        stats["new"] += 1
        cases = apply_event_to_entities(db, ev)
        invoice = _invoice_for_event(db, ev)
        if invoice is not None:
            touched_invoice_ids.add(invoice.id)
        # money credited to pre-existing open cases happened inside apply
        # step cases that received money so terminal transitions settle
        for case in cases:
            if case.state not in C.TERMINAL_STATES:
                step_case(db, case, cfg)
                stats["cases_stepped"] += 1

    for invoice_id in touched_invoice_ids:
        invoice = db.get(Invoice, invoice_id)
        if invoice is None:
            continue
        existing = find_case_for_invoice(db, invoice.id)
        if existing is not None:
            continue  # already handled above
        # A NEW case may be warranted (e.g. a failure just landed)
        if not invoice.is_paid:
            case = open_or_refresh_case(db, invoice)
            if case is not None:
                step_case(db, case, cfg)
                stats["cases_stepped"] += 1
    return stats


def _invoice_for_event(db: Session, ev: dict):
    rzr = ev.get("rzr_invoice_id") or (ev.get("invoice") or {}).get("rzr_invoice_id")
    if not rzr:
        return None
    return db.scalar(select(Invoice).where(Invoice.rzr_invoice_id == rzr))


def _mark_pending_applied(inter: Intervention) -> None:
    outcome = dict(inter.outcome or {})
    pending = outcome.get("pending_payment")
    if pending:
        outcome["pending_payment_applied"] = True
        inter.outcome = outcome


def pending_due_at(inter: Intervention):
    """Datetime a pending simulated payment becomes due, or None if none/applied."""
    outcome = inter.outcome or {}
    if outcome.get("pending_payment_applied"):
        return None
    pending = outcome.get("pending_payment")
    if not pending or not pending.get("pending_at"):
        return None
    from datetime import datetime as _dt

    return _dt.fromisoformat(pending["pending_at"])


def drain_pending_payments(db: Session, cfg: AgentConfig | None = None) -> list[int]:
    """Apply simulated money that has become due (intervention pending hints)."""
    cfg = cfg or cfg_default()
    closed: list[int] = []
    now_dt = now()
    interventions = db.scalars(
        select(Intervention).where(
            Intervention.status == "SUCCEEDED",
            Intervention.outcome != None,  # noqa: E711 (JSON column compare)
        )
    ).all()
    for inter in interventions:
        pending = (inter.outcome or {}).get("pending_payment")
        due = pending_due_at(inter)
        if due is None or due > now_dt:
            continue
        event_id = pending.get("event_id")
        already = db.scalar(select(RiskEvent).where(RiskEvent.event_id == event_id)) if event_id else None
        if already:
            _mark_pending_applied(inter)
            continue
        case = db.get(RiskCase, inter.case_id)
        if case is None:
            _mark_pending_applied(inter)
            continue
        event = {
            "event_id": event_id or f"sim:{inter.idempotency_key}:drain",
            "event_type": pending.get("event_type", "invoice.paid"),
            "rzr_invoice_id": pending.get("invoice_rzr_id") or case.entity_id,
            "rzr_payment_id": pending.get("payment_id", ""),
            "amount_minor": int(pending.get("amount_minor", 0)),
            "method": "upi",
            "source": "sim",
        }
        is_new, _row = store_event(db, event)
        _mark_pending_applied(inter)
        if is_new:
            apply_event_to_entities(db, event)
            closed.append(case.id)
            if case.state == C.STATE_OPEN:
                step_case(db, case, cfg)
    return closed


def cfg_default() -> AgentConfig:
    return AgentConfig()


def process_due_scheduled(db: Session, cfg: AgentConfig) -> int:
    """Step cases whose next_action_at has arrived."""
    cases = db.scalars(
        select(RiskCase).where(
            RiskCase.state == C.STATE_OPEN,
            RiskCase.next_action_at.isnot(None),
            RiskCase.next_action_at <= now(),
        )
    ).all()
    stepped = 0
    for case in cases:
        step_case(db, case, cfg)
        stepped += 1
    return stepped


def run_tick(db: Session, cfg: AgentConfig | None = None) -> dict:
    """Hourly background tick: approvals, pending money, overdue detection,
    due scheduled actions."""
    cfg = cfg or cfg_default()
    result: dict = {"approvals_expired": 0, "pending_applied": [], "overdue_cases": 0, "scheduled_stepped": 0}

    # 1) supervised approvals past their timeout auto-decline (safe default)
    result["approvals_expired"] = _expire_stale_approvals(db, cfg)

    # 2) simulated money that arrived
    result["pending_applied"] = drain_pending_payments(db, cfg)

    # 3) overdue invoices -> cases
    from app.detection import open_or_refresh_case

    overdue_invoices = db.scalars(
        select(Invoice).where(
            Invoice.due_date < now(),
            Invoice.status.in_(["issued", "partially_paid"]),
        )
    ).all()
    due_cases: list[RiskCase] = []
    for inv in overdue_invoices:
        existing = find_case_for_invoice(db, inv.id)
        if existing is None:
            case = open_or_refresh_case(db, inv)
            if case is not None:
                due_cases.append(case)
        elif existing.state == C.STATE_OPEN and (existing.next_action_at is None or existing.next_action_at <= now()):
            # open, un-scheduled case on an overdue invoice -> (re)consider it
            due_cases.append(existing)

    # 4) due scheduled actions on any open case
    result["scheduled_stepped"] = process_due_scheduled(db, cfg)
    for case in due_cases:
        if case.state not in C.TERMINAL_STATES:
            step_case(db, case, cfg)
            result["overdue_cases"] += 1
    return result


def _expire_stale_approvals(db: Session, cfg: AgentConfig) -> int:
    timeout = timedelta(minutes=cfg.settings.approval_timeout_minutes)
    stale = db.scalars(
        select(Intervention).where(
            Intervention.status == "PENDING_APPROVAL",
            Intervention.created_at <= now() - timeout,
        )
    ).all()
    for inter in stale:
        db.add(
            Approval(
                intervention_id=inter.id,
                decided_by="system-timeout",
                decision="DECLINED",
                reason=f"approval timeout ({cfg.settings.approval_timeout_minutes} min) — safe default decline",
                decided_at=now(),
            )
        )
        inter.status = "DECLINED"
        case = db.get(RiskCase, inter.case_id)
        if case is not None:
            audit_event(
                db,
                actor=C.ACTOR_HUMAN,
                action="approval_timeout_auto_declined",
                payload={"case": case.case_key, "action": inter.action, "timeout_min": cfg.settings.approval_timeout_minutes},
                case_id=case.id,
            )
            set_state(db, case, C.STATE_CLOSED_NOOP, reason="action auto-declined: approval timeout")
    return len(stale)


# --------------------------------------------------------------------------- #
# Human decisions on supervised actions
# --------------------------------------------------------------------------- #
def decide_approval(db: Session, intervention_id: int, *, decision: str, actor: str, reason: str, cfg: AgentConfig | None = None) -> dict:
    cfg = cfg or cfg_default()
    inter = db.get(Intervention, intervention_id)
    if inter is None:
        return {"ok": False, "error": "intervention not found"}
    if inter.status != "PENDING_APPROVAL":
        return {"ok": False, "error": f"intervention not pending ({inter.status})"}
    if decision not in ("APPROVED", "DECLINED"):
        return {"ok": False, "error": "decision must be APPROVED or DECLINED"}

    case = db.get(RiskCase, inter.case_id)
    db.add(
        Approval(
            intervention_id=inter.id,
            decided_by=actor,
            decision=decision,
            reason=reason,
            decided_at=now(),
        )
    )
    audit_event(
        db,
        actor=C.ACTOR_HUMAN,
        action="human_decision",
        payload={"case": case.case_key, "action": inter.action, "decision": decision, "reason": reason, "actor": actor},
        case_id=case.id,
    )

    if decision == "DECLINED":
        inter.status = "DECLINED"
        set_state(db, case, C.STATE_CLOSED_NOOP, reason=f"action declined by {actor}")
        return {"ok": True, "status": "declined"}

    # APPROVED -> execute exactly once
    inter.status = "EXECUTING"
    db.flush()
    from app.policy.engine import ActionPlan

    plan = ActionPlan(
        verdict=C.VERDICT_APPROVED,
        playbook=inter.playbook,
        action=inter.action,
        params=inter.params or {},
        reasons=inter.policy_reasons or [],
    )
    audit_event(
        db,
        actor=C.ACTOR_POLICY,
        action="approval_granted_executing",
        payload={"case": case.case_key, "action": inter.action},
        case_id=case.id,
    )
    # Execute through the same executor path but against the parked
    # intervention (idempotency key already reserved).
    _run_parked(db, inter, plan, cfg)
    return {"ok": True, "status": inter.status}


def _run_parked(db: Session, inter: Intervention, plan, cfg: AgentConfig) -> None:
    from app.clock import now as _now

    invoice = db.get(Invoice, db.get(RiskCase, inter.case_id).invoice_id)
    case = db.get(RiskCase, inter.case_id)
    customer = db.get(Customer, case.customer_id)
    amount = case.amount_at_risk_minor
    provider = cfg.provider

    try:
        if inter.action == C.ACT_SEND_REMINDER:
            result = provider.send_reminder(
                invoice, customer,
                template=inter.params.get("template", "dunning_1"),
                channel=inter.params.get("channel", "email"),
                amount_minor=amount,
            )
        elif inter.action == C.ACT_CREATE_LINK:
            result = provider.create_payment_link(
                invoice, customer,
                amount_minor=amount,
                expire_hours=inter.params.get("expire_hours", 72),
                channel=inter.params.get("channel", "email"),
            )
        elif inter.action == C.ACT_RETRY:
            payment = db.scalar(
                select(Payment)
                .where(Payment.invoice_id == invoice.id, Payment.status == "failed")
                .order_by(Payment.attempted_at.desc())
                .limit(1)
            )
            if payment is None:
                result = {"ok": False, "provider_ref": "", "raw": {}, "pending_payment": None, "failure_code": "NO_PAYMENT_TO_RETRY"}
            else:
                result = provider.retry_payment(payment, invoice, customer, attempt=int(inter.params.get("attempt", 1)))
        elif inter.action == C.ACT_PTP_FOLLOWUP:
            result = provider.ptp_followup(
                invoice, customer,
                followup_number=int(inter.params.get("followup_number", 1)),
                channel=inter.params.get("channel", "email"),
            )
        elif inter.action == C.ACT_RESEND_CORRECTED_INVOICE:
            result = provider.resend_corrected_invoice(
                invoice, customer,
                correction_note=inter.params.get("correction_note", ""),
                channel=inter.params.get("channel", "email"),
            )
        elif inter.action == C.ACT_REQUEST_AP_UPDATE:
            result = provider.request_ap_update(
                invoice, customer,
                followup_number=int(inter.params.get("followup_number", 1)),
                channel=inter.params.get("channel", "email"),
            )
        elif inter.action == C.ACT_CONFIRM_PTP:
            promised = inter.params.get("promised_date_iso", "")
            result = provider.confirm_ptp(
                invoice, customer,
                promised_date_iso=promised,
                channel=inter.params.get("channel", "email"),
            )
            record_promise(db, case, promised, invoice)
        else:
            result = {"ok": False, "provider_ref": "", "raw": {}, "pending_payment": None, "failure_code": "UNKNOWN_ACTION"}
    except Exception as exc:
        result = {"ok": False, "provider_ref": "", "raw": {}, "pending_payment": None, "failure_code": f"PROVIDER_EXC:{type(exc).__name__}"}

    pending = result.get("pending_payment")
    if pending:
        offset = float(pending.get("offset_hours", 0))
        pending["pending_at"] = (_now() + timedelta(hours=offset)).isoformat()
        pending["event_id"] = f"sim:{inter.idempotency_key}"
        pending["invoice_rzr_id"] = invoice.rzr_invoice_id
        pending["customer_id"] = customer.id

    inter.status = "SUCCEEDED" if result.get("ok") else "FAILED"
    inter.outcome = result
    inter.executed_at = _now()
    inter.error = result.get("failure_code", "")
    db.flush()
    audit_event(
        db,
        actor=C.ACTOR_SYSTEM,
        action="action_result",
        payload={"case": case.case_key, "action": inter.action, "ok": result.get("ok"), "failure_code": result.get("failure_code")},
        case_id=case.id,
    )
    _observe_execution(db, case, inter, cfg)
