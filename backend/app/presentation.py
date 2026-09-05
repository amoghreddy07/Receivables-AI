"""Presentation-only helpers for the dashboard.

These are display copy and formatting — they never influence a decision, never
recompute a metric, and never reimplement a policy rule. Numbers stay in minor
units (paise) until a template renders them; recovery/eval figures come straight
from the persisted rows (RiskCase / EvalRun / EvalCase).

`meta.demo` / `meta.simulated` markers are written by scripts/seed_demo.py on
the invoices/customers/messages — the dashboard reads them, never guesses from
case-id strings.
"""
from __future__ import annotations

from app import constants as C
from app.policy.playbooks import PLAYBOOKS

# --------------------------------------------------------------------------- #
# State / verdict chips
# --------------------------------------------------------------------------- #
STATE_LABELS: dict[str, str] = {
    C.STATE_OPEN: "Open",
    C.STATE_PENDING_APPROVAL: "Pending approval",
    C.STATE_RECOVERED: "Recovered",
    C.STATE_CLOSED_NOOP: "Closed — no action",
    C.STATE_STOPPED_COMPLIANCE: "Hard stop · compliance",
    C.STATE_STOPPED_FRAUD: "Hard stop · fraud",
    C.STATE_STOPPED_RULE: "Hard stop · rule",
    C.STATE_ESCALATED: "Escalated to human",
}

# chip css class per state (see static/dashboard.css)
STATE_CLASS: dict[str, str] = {
    C.STATE_OPEN: "warn",
    C.STATE_PENDING_APPROVAL: "info",
    C.STATE_RECOVERED: "ok",
    C.STATE_CLOSED_NOOP: "muted",
    C.STATE_STOPPED_COMPLIANCE: "bad",
    C.STATE_STOPPED_FRAUD: "bad",
    C.STATE_STOPPED_RULE: "bad",
    C.STATE_ESCALATED: "accent",
}

VERDICT_LABELS: dict[str, str] = {
    "APPROVED": "Approved",
    "BLOCKED": "Blocked",
    "REQUIRES_APPROVAL": "Requires approval",
    "DEFERRED": "Deferred",
    "ESCALATE": "Escalated",
}

VERDICT_CLASS: dict[str, str] = {
    "APPROVED": "ok",
    "BLOCKED": "bad",
    "REQUIRES_APPROVAL": "info",
    "DEFERRED": "muted",
    "ESCALATE": "accent",
}

PATH_LABELS: dict[str, str] = {
    C.DIAG_RULES: "rules",
    C.DIAG_LLM: "llm",
    C.DIAG_FUSED: "fused",
}

# --------------------------------------------------------------------------- #
# Root-cause / playbook / action labels (raw code always shown alongside)
# --------------------------------------------------------------------------- #
CAUSE_LABELS: dict[str, str] = {
    C.CAUSE_TECHNICAL: "Technical failure",
    C.CAUSE_INSUFFICIENT_FUNDS: "Insufficient funds",
    C.CAUSE_AUTH_FAILURE: "Auth failed",
    C.CAUSE_INSTRUMENT_EXPIRED: "Instrument expired",
    C.CAUSE_FRAUD_SUSPECTED: "Fraud suspected",
    C.CAUSE_OVERDUE: "Overdue",
    C.CAUSE_PARTIAL_BALANCE: "Partial balance",
    C.CAUSE_PTP_MISSED: "Promise-to-pay missed",
    C.CAUSE_UNKNOWN: "Unknown",
    C.CAUSE_DOCUMENTATION_ISSUE: "Documentation blocker",
    C.CAUSE_AWAITING_AP_APPROVAL: "Awaiting AP approval",
    C.CAUSE_PTP_OFFERED: "Promise to pay",
    C.CAUSE_DISPUTE: "Invoice dispute",
    C.CAUSE_INSUFFICIENT_CONTEXT: "Insufficient context",
}

ACTION_LABELS: dict[str, str] = {
    C.ACT_SEND_REMINDER: "Send dunning reminder",
    C.ACT_CREATE_LINK: "Create payment link + notify",
    C.ACT_RETRY: "Retry payment",
    C.ACT_PTP_FOLLOWUP: "Promise-to-pay follow-up",
    C.ACT_ESCALATE: "Escalate to human",
    C.ACT_RESEND_CORRECTED_INVOICE: "Resend corrected invoice + link",
    C.ACT_REQUEST_AP_UPDATE: "Request AP status update",
    C.ACT_CONFIRM_PTP: "Confirm promised date",
}

ESC_LABELS: dict[str, str] = {
    C.ESC_COMPLIANCE_DNC: "Do-not-contact (compliance)",
    C.ESC_COMPLIANCE_BANKRUPTCY: "Bankruptcy flag (compliance)",
    C.ESC_COMPLIANCE_AGE: "Over 120-day compliance limit",
    C.ESC_FRAUD_AVS: "AVS mismatch",
    C.ESC_FRAUD_AUTH_FAILURES: "Repeated auth failures",
    C.ESC_FRAUD_SUSPICIOUS: "Fraud lock / suspicious activity",
    C.ESC_LOW_CONFIDENCE: "Low-confidence diagnosis",
    C.ESC_LADDER_EXHAUSTED: "Recovery ladder exhausted",
    C.ESC_PTP_BROKEN: "Promise-to-pay broken",
    C.ESC_DISPUTE: "Invoice dispute",
}

FLAG_LABELS: dict[str, str] = {
    C.FLAG_DNC: "Do-not-contact",
    C.FLAG_BANKRUPTCY: "Bankruptcy",
    C.FLAG_LEGAL_HOLD: "Legal hold",
}

EVENT_ACTION_LABELS: dict[str, str] = {
    "case_opened": "Case opened",
    "payment_credited": "Payment credited",
    "state_changed": "State changed",
    "diagnosis_fused": "Diagnosis",
    "llm_diagnosis": "LLM diagnosis",
    "llm_unavailable_rules_fallback": "LLM unavailable — rules fallback",
    "policy_decision": "Policy decision",
    "action_dispatched": "Action dispatched",
    "action_result": "Action result",
    "action_blocked_not_whitelisted": "Blocked — not whitelisted",
    "approval_requested": "Approval requested",
    "approval_granted_executing": "Approval granted — executing",
    "approval_timeout_auto_declined": "Approval timeout — auto-declined",
    "human_decision": "Human decision",
    "escalated": "Escalated",
    "deferred_for_promise": "Deferred for promise",
    "action_deferred": "Action deferred",
    "next_ladder_step_scheduled": "Next ladder step scheduled",
    "retry_scheduled_after_failure": "Retry scheduled after failure",
    "event_duplicate_ignored": "Duplicate event ignored",
    "event_unresolved_invoice": "Event — unresolved invoice",
    "below_action_threshold_no_action": "Below action threshold — watch only",
    "transition_warning": "Transition warning",
}


def cause_label(cause: str) -> str:
    return CAUSE_LABELS.get(cause, cause)


def playbook_label(pb: str) -> str:
    return PLAYBOOKS.get(pb, {}).get("label", pb)


def action_label(action: str) -> str:
    return ACTION_LABELS.get(action, action)


def esc_label(code: str) -> str:
    return ESC_LABELS.get(code, code)


def event_action_label(action: str) -> str:
    return EVENT_ACTION_LABELS.get(action, action)


# --------------------------------------------------------------------------- #
# Formatting (Indian digit grouping; money in minor units -> "₹4,20,000")
# --------------------------------------------------------------------------- #
def _indian_group(n: int) -> str:
    s = str(int(n))
    if len(s) <= 3:
        return s
    last3 = s[-3:]
    rest = s[:-3]
    groups = []
    while rest:
        groups.insert(0, rest[-2:])
        rest = rest[:-2]
    return ",".join(groups + [last3])


def inr_major(minor: int) -> str:
    """Render a minor-unit (paise) amount as whole rupees, Indian grouping."""
    try:
        major = int(minor) // 100
    except (TypeError, ValueError):
        return "—"
    return f"₹{_indian_group(major)}"


def num_group(n) -> str:
    try:
        return _indian_group(int(n))
    except (TypeError, ValueError):
        return "—"


def pct(x) -> str:
    try:
        return f"{100 * float(x):.1f}%"
    except (TypeError, ValueError):
        return "—"


def fmt_dt(dt) -> str:
    if dt is None:
        return "—"
    s = str(dt)
    if "." in s:
        s = s.split(".", 1)[0]
    return s.replace("T", " ")
