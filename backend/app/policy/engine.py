"""Policy engine — the ONLY authorizer of side effects.

A diagnosis (rules or LLM — it does not matter which) proposes a cause and a
playbook. The engine then decides, in this order:

  1. compliance rules      -> BLOCK (hard stop + escalation) — never overridable
  2. fraud rules           -> BLOCK (hard stop + escalation) — never overridable
  3. stopping rules        -> DEFER (cooldown/window/budget) or ESCALATE (caps)
  4. playbook capability   -> what action to plan for the diagnosed cause
  5. gates (mode, value)   -> APPROVED | REQUIRES_APPROVAL

Engine output is an ActionPlan. Execution happens ONLY after APPROVED (or human
approval for REQUIRES_APPROVAL), and only inside the execution layer.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta

from sqlalchemy.orm import Session

from app import constants as C
from app.config import Settings
from app.policy import rules as R
from app.policy.playbooks import CAUSE_TO_PLAYBOOK, PLAYBOOKS, default_action


@dataclass
class ActionPlan:
    verdict: str                      # APPROVED | BLOCKED | REQUIRES_APPROVAL | DEFERRED | ESCALATE
    playbook: str = ""
    action: str = ""
    params: dict = field(default_factory=dict)
    reasons: list[dict] = field(default_factory=list)   # matched rules (audited)
    escalate_code: str = ""
    defer_to: datetime | None = None   # when a DEFERRED plan may retry
    stop_state: str = ""               # STOPPED_COMPLIANCE | STOPPED_FRAUD | STOPPED_RULE
    policy_override: bool = False      # diagnosis proposed something else


def next_contact_window(now: datetime, hour_start: int) -> datetime:
    """Next allowed hour_start:00 (tomorrow if today's window already passed)."""
    cand = now.replace(hour=hour_start, minute=0, second=0, microsecond=0)
    if cand <= now:
        cand += timedelta(days=1)
    return cand


def plan_action(
    db: Session,
    case,
    *,
    diagnosis_cause: str,
    proposed_playbook: str,
    mode: str,
    settings: Settings,
    now: datetime,
    audit_only: bool = False,
    diagnosis_params: dict | None = None,
) -> ActionPlan:
    """Decide what (if anything) may happen next for a case.

    audit_only=True evaluates rules WITHOUT producing a plan for the execution
    layer — used by the evaluator to measure violations of baselines that act
    without a policy. Violations surface as `reasons` on the returned plan.
    """
    reasons: list[RuleHit] = []

    # ---- 1. compliance (always wins) ------------------------------------- #
    comp = R.compliance_hits(db, case)
    reasons += comp
    if comp:
        code = _pick_compliance_code(comp)
        return _block(
            plan=ActionPlan(verdict=C.VERDICT_BLOCKED, reasons=[h.as_dict() for h in reasons]),
            stop_state=C.STATE_STOPPED_COMPLIANCE,
            escalate_code=code,
        )

    # ---- 2. fraud (always wins) ------------------------------------------- #
    fraud = R.fraud_hits(db, case)
    reasons += fraud
    if fraud:
        return _block(
            plan=ActionPlan(verdict=C.VERDICT_BLOCKED, reasons=[h.as_dict() for h in reasons]),
            stop_state=C.STATE_STOPPED_FRAUD,
            escalate_code=fraud[0].code,
        )

    # ---- 3. resolve the playbook from the cause ---------------------------- #
    playbook = CAUSE_TO_PLAYBOOK.get(diagnosis_cause, C.PB_ESCALATE)
    action = default_action(playbook)

    proposed = proposed_playbook or ""
    override = bool(proposed and proposed != playbook)
    if override:
        reasons.append(
            R.RuleHit(
                "POLICY_OVERRIDE",
                f"proposed playbook '{proposed}' overridden by policy mapping -> '{playbook}'",
                hard=True,
            )
        )

    # a promise-accept needs the promised date the LLM extracted; without one it
    # is an uninformed confirmation -> escalate instead of guessing
    if playbook == C.PB_PROMISE_ACCEPT and not (diagnosis_params or {}).get("promised_date_iso"):
        return ActionPlan(
            verdict="ESCALATE",
            playbook=playbook,
            action=C.ACT_ESCALATE,
            params={"cause": diagnosis_cause, "reason": "promise date missing from diagnosis"},
            reasons=[h.as_dict() for h in reasons]
            + [R.RuleHit("PROMISE_DATE_MISSING", "ptp_offered without an extractable promised date -> human review").as_dict()],
            escalate_code=C.ESC_LOW_CONFIDENCE,
            policy_override=override,
        )

    if playbook == C.PB_ESCALATE:
        # Unknown cause with no prior rule hit => human review. A genuine
        # invoice dispute gets its own code so humans see WHY it was routed.
        esc_code = C.ESC_DISPUTE if diagnosis_cause == C.CAUSE_DISPUTE else C.ESC_LOW_CONFIDENCE
        return ActionPlan(
            verdict="ESCALATE",
            playbook=playbook,
            action=C.ACT_ESCALATE,
            params={"cause": diagnosis_cause},
            reasons=[h.as_dict() for h in reasons],
            escalate_code=esc_code,
            policy_override=override,
        )

    # ---- 4. stopping rules -------------------------------------------------- #
    stops = R.stopping_hits(
        db,
        case,
        playbook,
        contact_hour_start=settings.contact_hour_start,
        contact_hour_end=settings.contact_hour_end,
        customer_monthly_cap=settings.customer_monthly_action_cap,
        global_daily_budget=settings.global_daily_action_budget,
        now=now,
    )
    reasons += stops

    hard = [h for h in stops if h.hard]
    soft = [h for h in stops if not h.hard]

    if hard:
        # Ladder exhausted -> escalate to a human (never silently drop)
        return ActionPlan(
            verdict="ESCALATE",
            playbook=playbook,
            action=C.ACT_ESCALATE,
            params={"cause": diagnosis_cause, "playbook": playbook},
            reasons=[h.as_dict() for h in reasons],
            escalate_code=PLAYBOOKS[playbook]["exhaust_reason"],
            policy_override=override,
        )

    # soft blockers are deferrable
    defer_code = soft[0].code if soft else ""
    if soft:
        meta = PLAYBOOKS[playbook]
        if defer_code == "OUTSIDE_CONTACT_HOURS":
            defer_to = next_contact_window(now, settings.contact_hour_start)
        elif defer_code == "IN_COOLDOWN":
            defer_to = _after_cooldown(now, meta["cooldown_hours"])
        else:  # budgets
            defer_to = (now + timedelta(hours=1)).replace(minute=0, second=0, microsecond=0)
        return ActionPlan(
            verdict=C.VERDICT_DEFERRED,
            playbook=playbook,
            action=action,
            params=_action_params(db, case, playbook, action),
            reasons=[h.as_dict() for h in reasons],
            defer_to=defer_to,
            policy_override=override,
        )

    if audit_only:
        return ActionPlan(
            verdict="APPROVED",
            playbook=playbook,
            action=action,
            params=_action_params(db, case, playbook, action, extra=diagnosis_params or {}),
            reasons=[h.as_dict() for h in reasons],
            policy_override=override,
        )

    # ---- 5. gates ----------------------------------------------------------- #
    params = _action_params(db, case, playbook, action, extra=diagnosis_params or {})
    gate_reasons = []

    high_value = case.amount_at_risk_minor >= settings.high_value_auto_limit_minor
    if high_value:
        gate_reasons.append(
            R.RuleHit(
                "HIGH_VALUE_GATE",
                f"amount ₹{case.amount_at_risk_minor / 100:,.0f} exceeds autonomous limit — human approval required",
            )
        )
        reasons.append(gate_reasons[-1])
        return ActionPlan(
            verdict=C.VERDICT_REQUIRES_APPROVAL,
            playbook=playbook,
            action=action,
            params=params,
            reasons=[h.as_dict() for h in reasons],
            policy_override=override,
        )

    if mode == C.MODE_SUPERVISED:
        reasons.append(
            R.RuleHit(
                "SUPERVISED_MODE",
                "agent running in SUPERVISED mode — every action requires human approval",
                hard=True,
            )
        )
        return ActionPlan(
            verdict=C.VERDICT_REQUIRES_APPROVAL,
            playbook=playbook,
            action=action,
            params=params,
            reasons=[h.as_dict() for h in reasons],
            policy_override=override,
        )

    return ActionPlan(
        verdict=C.VERDICT_APPROVED,
        playbook=playbook,
        action=action,
        params=params,
        reasons=[h.as_dict() for h in reasons],
        policy_override=override,
    )


def _block(plan: ActionPlan, stop_state: str, escalate_code: str) -> ActionPlan:
    plan.verdict = C.VERDICT_BLOCKED
    plan.stop_state = stop_state
    plan.escalate_code = escalate_code
    return plan


def _pick_compliance_code(hits) -> str:
    codes = [h.code for h in hits]
    for pref in (C.ESC_COMPLIANCE_BANKRUPTCY, C.ESC_COMPLIANCE_DNC, C.ESC_COMPLIANCE_AGE):
        if pref in codes:
            return pref
    return codes[0]


def _after_cooldown(now: datetime, cooldown_hours: int) -> datetime:
    return now + timedelta(hours=cooldown_hours)


def _action_params(db: Session, case, playbook: str, action: str, *, extra: dict | None = None) -> dict:
    from app.policy.playbooks import CHANNEL_ORDER, executed_count

    extra = extra or {}
    params: dict = {"amount_minor": case.amount_at_risk_minor}
    if action == C.ACT_SEND_REMINDER:
        n = executed_count(db, case.id, playbook) + 1
        params.update({"reminder_number": n, "template": f"dunning_{min(n, 3)}", "channel": CHANNEL_ORDER[0]})
    elif action == C.ACT_CREATE_LINK:
        params.update({"expire_hours": 72, "channel": CHANNEL_ORDER[0]})
    elif action == C.ACT_RETRY:
        n = executed_count(db, case.id, playbook) + 1
        params.update({"attempt": n})
    elif action == C.ACT_PTP_FOLLOWUP:
        n = executed_count(db, case.id, playbook) + 1
        params.update({"followup_number": n, "template": f"ptp_{n}", "channel": CHANNEL_ORDER[0]})
    elif action == C.ACT_REQUEST_AP_UPDATE:
        n = executed_count(db, case.id, playbook) + 1
        params.update({"followup_number": n, "template": f"ap_status_{n}", "channel": CHANNEL_ORDER[0]})
    elif action == C.ACT_RESEND_CORRECTED_INVOICE:
        params.update({"channel": "email", "correction_note": str(extra.get("correction_note", ""))[:300]})
    elif action == C.ACT_CONFIRM_PTP:
        params.update({"channel": "email", "promised_date_iso": extra.get("promised_date_iso", "")})
    return params
