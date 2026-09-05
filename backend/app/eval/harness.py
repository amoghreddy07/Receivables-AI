"""Evaluation harness.

Replays the REAL agent loop (app.agent) case-by-case against the seeded
sandbox, on a per-case VIRTUAL clock, until the case settles or the horizon is
reached. Ground truth is used ONLY for scoring afterwards — never to steer the
sandbox or the agent.

Harvested rows are self-contained (case label, state, actions, violations,
money) and feed metrics.py. All timestamps come from the virtual clock, so runs
are fully reproducible for a given seed.
"""
from __future__ import annotations

from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from app import constants as C
from app.agent import AgentConfig, drain_pending_payments, ingest_events, run_tick
from app.clock import set_clock, VirtualClock
from app.detection import open_or_refresh_case
from app.models import (
    Approval,
    CaseMessage,
    ComplianceFlag,
    Customer,
    Intervention,
    Invoice,
    Payment,
    PTPPromise,
    RiskCase,
    RiskEvent,
)
from app.policy import rules as policy_rules

# --------------------------------------------------------------------------- #
# Seeding persisted case data (the "world" the agent observes)
# --------------------------------------------------------------------------- #
def seed_case(db: Session, cc: dict, t0: datetime) -> Invoice:
    cust = Customer(
        org_name=cc["org"],
        email=f"billing@{cc['org'].split()[0].lower()}.com",
        phone="+919810000000",
        behavior_profile=cc["profile"],
        meta={"eval_scenario": cc["scenario"]},
    )
    db.add(cust)
    db.flush()
    for flag in cc["flags"]:
        db.add(ComplianceFlag(customer_id=cust.id, flag_type=flag, reason_code=f"eval:{cc['scenario']}"))

    due = t0 - timedelta(days=cc["due_days_ago"]) if cc["due_days_ago"] else t0 + timedelta(days=30)
    meta = {"eval_scenario": cc["scenario"]}
    if cc.get("world_offset_hours"):
        meta["scenario_offset_hours"] = float(cc["world_offset_hours"])
    invoice = Invoice(
        rzr_invoice_id=cc["invoice_id"],
        customer_id=cust.id,
        amount_minor=cc["amount_minor"],
        paid_amount_minor=cc["paid_minor"],
        currency=cc["currency"],
        status="paid" if cc["paid_minor"] >= cc["amount_minor"] else ("partially_paid" if cc["paid_minor"] else "issued"),
        due_date=due,
        meta=meta,
    )
    db.add(invoice)
    db.flush()

    for m in cc.get("messages", []):
        db.add(
            CaseMessage(
                invoice_id=invoice.id,
                customer_id=cust.id,
                direction=m.get("direction", "in"),
                channel=m.get("channel", "email"),
                content=m.get("content", ""),
                received_at=t0 - timedelta(hours=float(m.get("days_ago", 2.0)) * 24),
                meta={"eval_scenario": cc["scenario"]},
            )
        )

    for idx, (code, days_ago, desc, method) in enumerate(cc["attempts"]):
        db.add(
            Payment(
                rzr_payment_id=f"pay_{cc['invoice_id']}_{idx}",
                invoice_id=invoice.id,
                customer_id=cust.id,
                amount_minor=invoice.outstanding_minor,
                method=method,
                status="failed",
                error_code=code,
                error_description=desc,
                attempted_at=t0 - timedelta(days=days_ago),
            )
        )
    if cc.get("promise_days") is not None:
        # PTPPromise rows are created lazily by the harness since the case does
        # not exist yet; the agent defers on a future promise.
        db.flush()
        case = _find_or_open(db, invoice, t0)
        db.add(
            PTPPromise(
                case_id=case.id,
                promised_date=t0 + timedelta(days=cc["promise_days"]),
                amount_minor=invoice.outstanding_minor,
                status="active",
            )
        )
    db.flush()
    return invoice


def _find_or_open(db: Session, invoice: Invoice, t0) -> RiskCase:
    case = db.scalar(select(RiskCase).where(RiskCase.case_key == f"invoice:{invoice.rzr_invoice_id}"))
    if case is None:
        case = open_or_refresh_case(db, invoice)
    return case


def trigger_events(cc: dict, invoice: Invoice, t0: datetime) -> list[dict]:
    """Initial webhook event(s) for payment-failure cases."""
    if cc["detection"] != "webhook":
        return []
    attempts = cc["attempts"]
    if not attempts:
        return []
    code, _days, desc, method = attempts[-1]  # the latest failure
    return [
        {
            "event_id": f"evt:{cc['invoice_id']}:fail",
            "event_type": "payment.failed",
            "rzr_invoice_id": invoice.rzr_invoice_id,
            "rzr_payment_id": f"pay_{cc['invoice_id']}_{len(attempts) - 1}",
            "amount_minor": invoice.outstanding_minor,
            "method": method,
            "error_code": code,
            "error_description": desc,
            "attempted_at": t0.isoformat(),
            "verified": True,
            "source": "sim",
        }
    ]


def _corpus_events_due(cc: dict, elapsed_hours: float) -> list[dict]:
    due = []
    for fe in cc.get("future_events", []):
        if abs(fe["offset_hours"] - elapsed_hours) < 1e-6:
            due.append(
                {
                    "event_id": f"evt:{cc['invoice_id']}:{fe['event_type']}:{int(fe['offset_hours'])}",
                    "event_type": fe["event_type"],
                    "rzr_invoice_id": fe["rzr_invoice_id"],
                    "rzr_payment_id": f"pay_{cc['invoice_id']}_col{int(fe['offset_hours'])}",
                    "amount_minor": fe["amount_minor"],
                    "method": "neft",
                    "verified": True,
                    "source": "sim",
                }
            )
    return due


def _milestones(db: Session, cc: dict, t0: datetime, horizon: datetime, approval_timeout_min: int = 30) -> list[datetime]:
    """Next points in simulated time the harness must wake up for."""
    points: list[datetime] = []
    stored_event_ids = set(
        db.scalars(select(RiskEvent.event_id).where(RiskEvent.event_id.like(f"evt:{cc['invoice_id']}:%"))).all()
    )
    for fe in cc.get("future_events", []):
        at = t0 + timedelta(hours=fe["offset_hours"])
        event_id = f"evt:{cc['invoice_id']}:{fe['event_type']}:{int(fe['offset_hours'])}"
        if at <= horizon and event_id not in stored_event_ids:
            points.append(at)
    for case in db.scalars(
        select(RiskCase).where(RiskCase.state == C.STATE_OPEN, RiskCase.next_action_at.isnot(None))
    ).all():
        if case.next_action_at <= horizon:
            points.append(case.next_action_at)
    # pending simulated money (drained by run_tick when due); already-applied
    # hints are ignored so settled cases cannot loop forever
    from app.agent import pending_due_at

    for inter in db.scalars(select(Intervention).where(Intervention.status == "SUCCEEDED")).all():
        at = pending_due_at(inter)
        if at is not None and at <= horizon:
            points.append(at)
    # approval timeouts (auto-decline at created_at + timeout)
    for inter in db.scalars(select(Intervention).where(Intervention.status == "PENDING_APPROVAL")).all():
        at = inter.created_at + timedelta(minutes=approval_timeout_min)
        if at <= horizon:
            points.append(at)
    return sorted(set(points))


def simulate_case(
    db: Session,
    cc: dict,
    cfg: AgentConfig,
    *,
    horizon_days: int = 45,
    t0: datetime | None = None,
) -> dict:
    """Run ONE corpus case through the real agent loop. Returns a harvest row."""
    start = (t0 or datetime(2026, 1, 5, 10, 0, 0)).replace(hour=cc["start_hour"], minute=0, second=0)
    clock = VirtualClock(start=start)
    set_clock(clock)
    horizon = start + timedelta(days=horizon_days)

    invoice = seed_case(db, cc, start)
    db.commit()

    # initial events (webhook trigger + any offset-0 money events, one batch)
    batch = trigger_events(cc, invoice, start) + _corpus_events_due(cc, 0.0)
    if batch:
        ingest_events(db, batch, cfg)
    run_tick(db, cfg)
    db.commit()

    # advance through milestones until the case settles or the horizon ends
    guard = 0
    while guard < 2000:
        guard += 1
        nxt = _milestones(db, cc, start, horizon, approval_timeout_min=cfg.settings.approval_timeout_minutes)
        if not nxt:
            break
        target = min(nxt)
        clock.advance(to=target)
        due_events = _corpus_events_due(cc, (clock.now() - start).total_seconds() / 3600)
        if due_events:
            ingest_events(db, due_events, cfg)
        run_tick(db, cfg)
        db.commit()
        if (clock.now() - start).total_seconds() / 3600 >= horizon_days * 24:
            break

    harvest = _harvest(db, cc, start)
    db.commit()
    return harvest


def _harvest(db: Session, cc: dict, start: datetime) -> dict:
    case = db.scalar(select(RiskCase).where(RiskCase.case_key == f"invoice:{cc['invoice_id']}"))
    gt = cc["ground_truth"]

    interventions = (
        db.scalars(select(Intervention).where(Intervention.case_id == case.id).order_by(Intervention.id))
        if case
        else []
    )
    actions = []
    violations: list[str] = []
    for iv in interventions:
        if iv.status in ("SUCCEEDED", "FAILED", "EXECUTING", "PENDING_APPROVAL", "DECLINED"):
            actions.append({"playbook": iv.playbook, "action": iv.action, "status": iv.status})
        if iv.status in ("SUCCEEDED", "FAILED", "EXECUTING") and case is not None:
            violations += _audit_intervention(db, case, iv)

    state = case.state if case is not None else "NO_CASE"
    executed_actions = [a for a in actions if a["status"] in ("SUCCEEDED", "FAILED", "EXECUTING")]

    ttr_hours = None
    if case is not None and case.state == C.STATE_RECOVERED and case.recovered_amount_minor > 0 and case.closed_at:
        ttr_hours = round((case.closed_at - case.opened_at).total_seconds() / 3600, 1)

    correct = any(a["playbook"] == gt["playbook"] for a in executed_actions) if gt["playbook"] != "NO_ACTION" else True
    false_action = gt["playbook"] == "NO_ACTION" and len(executed_actions) > 0
    escalated = case is not None and case.state in (C.STATE_ESCALATED, C.STATE_STOPPED_COMPLIANCE, C.STATE_STOPPED_FRAUD, C.STATE_STOPPED_RULE)

    return {
        "case_key": f"invoice:{cc['invoice_id']}",
        "scenario": cc["scenario"],
        "org": cc["org"],
        "state": state,
        "cause": case.cause if case else "",
        "amount_minor": cc["amount_minor"],
        "ground_truth": gt,
        "actions_taken": actions,
        "violations": sorted(set(violations)),
        "recovered_amount_minor": case.recovered_amount_minor if case else 0,
        "expected_recovery_minor": gt["expected_recovery_minor"],
        "time_to_recover_hours": ttr_hours,
        "correct_action": correct,
        "false_action": false_action,
        "escalated": escalated,
        "should_escalate": gt["should_escalate"],
        "no_case": case is None,
    }


def _audit_intervention(db: Session, case: RiskCase, iv: Intervention) -> list[str]:
    """Audit one executed action against the real policy rules (un-enforced)."""
    codes: list[str] = []
    comp = [h.code for h in policy_rules.compliance_hits(db, case)]
    fraud = [h.code for h in policy_rules.fraud_hits(db, case)]
    codes += comp + fraud

    if iv.playbook != C.PB_ESCALATE and iv.executed_at is not None:
        hour = iv.executed_at.hour
        if not (9 <= hour < 21):
            codes.append("OUTSIDE_CONTACT_HOURS")
    if iv.playbook != C.PB_ESCALATE and case is not None:
        flags = [f.flag_type for f in db.scalars(select(ComplianceFlag).where(ComplianceFlag.customer_id == case.customer_id)).all()]
        if flags:
            pass  # compliance_hits above already covers flag-based codes
    return codes
