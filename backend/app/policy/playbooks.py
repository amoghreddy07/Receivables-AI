"""Data-driven playbook registry (plain Python dicts — no YAML dependency).

Schema per playbook:
    label           human description
    causes          root causes this playbook addresses
    max_attempts    maximum executed actions of this playbook per case
    cooldown_hours  minimum hours between two executions
    channels        allowed outreach channels (whitelist)
    actions         atomic actions this playbook may take
    human_gate      True => always requires approval even in AUTONOMOUS mode
    reason          escalation code when the ladder is exhausted

Refunds, discounts, amount edits and legal threats are NOT playbooks/actions —
they do not exist in the whitelist at all.
"""
from __future__ import annotations

from app import constants as C

PLAYBOOKS: dict[str, dict] = {
    C.PB_SMART_RETRY: {
        "label": "Smart retry",
        "causes": [C.CAUSE_TECHNICAL, C.CAUSE_INSUFFICIENT_FUNDS],
        "max_attempts": 2,
        "cooldown_hours": 24,
        "channels": [],  # retry is not an outreach channel per se
        "actions": [C.ACT_RETRY],
        "human_gate": False,
        "exhaust_reason": C.ESC_LADDER_EXHAUSTED,
    },
    C.PB_PAYMENT_LINK: {
        "label": "Payment link resend (instrument switch)",
        "causes": [C.CAUSE_AUTH_FAILURE, C.CAUSE_INSTRUMENT_EXPIRED],
        "max_attempts": 1,
        "cooldown_hours": 48,
        "channels": ["email", "sms"],
        "actions": [C.ACT_CREATE_LINK],
        "human_gate": False,
        "exhaust_reason": C.ESC_LADDER_EXHAUSTED,
    },
    C.PB_DUNNING: {
        "label": "Dunning reminder",
        "causes": [C.CAUSE_OVERDUE, C.CAUSE_PARTIAL_BALANCE],
        "max_attempts": 3,
        "cooldown_hours": 48,
        "channels": ["email", "sms"],
        "actions": [C.ACT_SEND_REMINDER],
        "human_gate": False,
        "exhaust_reason": C.ESC_LADDER_EXHAUSTED,
    },
    C.PB_PTP: {
        "label": "Promise-to-pay follow-up",
        "causes": [C.CAUSE_PTP_MISSED],
        "max_attempts": 2,
        "cooldown_hours": 24,
        "channels": ["email", "sms"],
        "actions": [C.ACT_PTP_FOLLOWUP],
        "human_gate": False,
        "exhaust_reason": C.ESC_PTP_BROKEN,
    },
    C.PB_ESCALATE: {
        "label": "Escalate to human",
        "causes": [],
        "max_attempts": 1,
        "cooldown_hours": 0,
        "channels": [],
        "actions": [C.ACT_ESCALATE],
        "human_gate": False,
        "exhaust_reason": "",
    },
    # --- unstructured-context playbooks (authorized by the same policy engine) --- #
    C.PB_DOCUMENTATION_FIX: {
        "label": "Resend corrected invoice/documentation + payment link",
        "causes": [C.CAUSE_DOCUMENTATION_ISSUE],
        "max_attempts": 1,
        "cooldown_hours": 48,
        "channels": ["email"],
        "actions": [C.ACT_RESEND_CORRECTED_INVOICE],
        "human_gate": False,  # amount unchanged; correcting docs is standard AP ops
        "exhaust_reason": C.ESC_LADDER_EXHAUSTED,
    },
    C.PB_AP_COORDINATION: {
        "label": "AP coordination (light, non-escalating status follow-up)",
        "causes": [C.CAUSE_AWAITING_AP_APPROVAL],
        "max_attempts": 2,
        "cooldown_hours": 72,
        "channels": ["email"],
        "actions": [C.ACT_REQUEST_AP_UPDATE],
        "human_gate": False,
        "exhaust_reason": C.ESC_LADDER_EXHAUSTED,
    },
    C.PB_PROMISE_ACCEPT: {
        "label": "Confirm explicit promise-to-pay date, then wait",
        "causes": [C.CAUSE_PTP_OFFERED],
        "max_attempts": 1,
        "cooldown_hours": 24,
        "channels": ["email"],
        "actions": [C.ACT_CONFIRM_PTP],
        "human_gate": False,
        "exhaust_reason": C.ESC_PTP_BROKEN,
    },
}

# Canonical cause -> playbook mapping. This is the deterministic "what to do
# about this cause" table that the policy engine consults. Diagnosis output is
# advisory; this table decides.
CAUSE_TO_PLAYBOOK: dict[str, str] = {
    C.CAUSE_TECHNICAL: C.PB_SMART_RETRY,
    C.CAUSE_INSUFFICIENT_FUNDS: C.PB_SMART_RETRY,
    C.CAUSE_AUTH_FAILURE: C.PB_PAYMENT_LINK,
    C.CAUSE_INSTRUMENT_EXPIRED: C.PB_PAYMENT_LINK,
    C.CAUSE_OVERDUE: C.PB_DUNNING,
    C.CAUSE_PARTIAL_BALANCE: C.PB_DUNNING,
    C.CAUSE_PTP_MISSED: C.PB_PTP,
    C.CAUSE_FRAUD_SUSPECTED: C.PB_ESCALATE,  # escalated, never acted on
    C.CAUSE_UNKNOWN: C.PB_ESCALATE,          # unknown cause => human review
    # Unstructured-context causes (only reachable via NLU diagnosis):
    C.CAUSE_DOCUMENTATION_ISSUE: C.PB_DOCUMENTATION_FIX,
    C.CAUSE_AWAITING_AP_APPROVAL: C.PB_AP_COORDINATION,
    C.CAUSE_PTP_OFFERED: C.PB_PROMISE_ACCEPT,
    C.CAUSE_DISPUTE: C.PB_ESCALATE,           # disputes go to humans, never auto-collected
    C.CAUSE_INSUFFICIENT_CONTEXT: C.PB_ESCALATE,  # rules abstained => human review
}

# Default channel preference order for outreach actions.
CHANNEL_ORDER = ["email", "sms"]


def default_action(playbook: str) -> str:
    """The action the policy engine plans for a playbook."""
    if playbook == C.PB_DUNNING:
        return C.ACT_SEND_REMINDER
    if playbook == C.PB_PAYMENT_LINK:
        return C.ACT_CREATE_LINK
    if playbook == C.PB_SMART_RETRY:
        return C.ACT_RETRY
    if playbook == C.PB_PTP:
        return C.ACT_PTP_FOLLOWUP
    if playbook == C.PB_DOCUMENTATION_FIX:
        return C.ACT_RESEND_CORRECTED_INVOICE
    if playbook == C.PB_AP_COORDINATION:
        return C.ACT_REQUEST_AP_UPDATE
    if playbook == C.PB_PROMISE_ACCEPT:
        return C.ACT_CONFIRM_PTP
    return C.ACT_ESCALATE


def executed_count(db, case_id: int, playbook: str) -> int:
    """Number of times a playbook's actions have been executed for a case."""
    from sqlalchemy import func, select

    from app.models import Intervention

    return db.scalar(
        select(func.count(Intervention.id)).where(
            Intervention.case_id == case_id,
            Intervention.playbook == playbook,
            Intervention.status.in_(["SUCCEEDED", "FAILED"]),
        )
    ) or 0


def last_executed_at(db, case_id: int, playbook: str):
    from sqlalchemy import select

    from app.models import Intervention

    return db.scalar(
        select(Intervention.executed_at)
        .where(
            Intervention.case_id == case_id,
            Intervention.playbook == playbook,
            Intervention.status.in_(["SUCCEEDED", "FAILED"]),
            Intervention.executed_at.isnot(None),
        )
        .order_by(Intervention.executed_at.desc())
        .limit(1)
    )
