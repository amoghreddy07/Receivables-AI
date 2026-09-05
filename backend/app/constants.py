"""Central string constants shared across the codebase.

Using plain strings (not Python enums) keeps serialization to JSON/SQLite and
templates trivially simple while still giving us single-source-of-truth names.
"""

# --------------------------------------------------------------------------- #
# Case lifecycle states
# --------------------------------------------------------------------------- #
# OPEN            – detected, revenue at risk, agent may act (or is waiting on a
#                   scheduled next action / pending approval)
# PENDING_APPROVAL– a supervised action is awaiting a human decision
# RECOVERED       – invoice fully paid; money credited
# CLOSED_NOOP     – closed without any intervention (self-healed / below action
#                   threshold / declined by human)
# STOPPED_COMPLIANCE – hard stop because of a compliance rule (DNC, bankruptcy,
#                      >120 days) — escalated
# STOPPED_FRAUD   – hard stop because of a deterministic fraud rule — escalated
# STOPPED_RULE    – hard stop because of a stopping rule (attempt cap, budget)
# ESCALATED       – escalated to a human with context (low confidence, ladder
#                   exhausted, broken promise-to-pay)
STATE_OPEN = "OPEN"
STATE_PENDING_APPROVAL = "PENDING_APPROVAL"
STATE_RECOVERED = "RECOVERED"
STATE_CLOSED_NOOP = "CLOSED_NOOP"
STATE_STOPPED_COMPLIANCE = "STOPPED_COMPLIANCE"
STATE_STOPPED_FRAUD = "STOPPED_FRAUD"
STATE_STOPPED_RULE = "STOPPED_RULE"
STATE_ESCALATED = "ESCALATED"

TERMINAL_STATES = {
    STATE_RECOVERED,
    STATE_CLOSED_NOOP,
    STATE_STOPPED_COMPLIANCE,
    STATE_STOPPED_FRAUD,
    STATE_STOPPED_RULE,
    STATE_ESCALATED,
}

# Transitions we assert on (source -> allowed destinations). Transient states
# such as EXECUTING/OBSERVING exist only inside a synchronous step and are
# captured in the audit trail rather than persisted on the case.
_ALLOWED_TRANSITIONS = {
    STATE_OPEN: {
        STATE_OPEN,
        STATE_PENDING_APPROVAL,
        STATE_RECOVERED,
        STATE_CLOSED_NOOP,
        STATE_STOPPED_COMPLIANCE,
        STATE_STOPPED_FRAUD,
        STATE_STOPPED_RULE,
        STATE_ESCALATED,
    },
    STATE_PENDING_APPROVAL: {
        STATE_OPEN,  # declined -> back to watch (audited)
        STATE_RECOVERED,
        STATE_STOPPED_COMPLIANCE,
        STATE_STOPPED_FRAUD,
        STATE_STOPPED_RULE,
        STATE_ESCALATED,
        STATE_CLOSED_NOOP,
    },
}


def allowed_transition(frm: str, to: str) -> bool:
    return to in _ALLOWED_TRANSITIONS.get(frm, {frm, *TERMINAL_STATES})


# --------------------------------------------------------------------------- #
# Root causes (diagnosis output)
# --------------------------------------------------------------------------- #
CAUSE_TECHNICAL = "technical_failure"            # bank/network technical issue
CAUSE_INSUFFICIENT_FUNDS = "insufficient_funds"  # balance unavailable at attempt time
CAUSE_AUTH_FAILURE = "auth_failure"              # 2FA / authentication declined
CAUSE_INSTRUMENT_EXPIRED = "instrument_expired"  # card expired
CAUSE_FRAUD_SUSPECTED = "fraud_suspected"        # AVS / fraud-lock signals
CAUSE_OVERDUE = "overdue"                        # invoice past due date
CAUSE_PARTIAL_BALANCE = "partial_balance"        # invoice partially paid
CAUSE_PTP_MISSED = "ptp_missed"                  # promised date passed unpaid
CAUSE_UNKNOWN = "unknown"

# --- causes derived from UNSTRUCTURED context (email / support / AP comms) --- #
# These are reachable ONLY through natural-language diagnosis. The deterministic
# rules classifier abstains (CAUSE_INSUFFICIENT_CONTEXT) rather than pretending
# to read free text, so rules-only operation escalates these instead of acting
# on a guess.
CAUSE_DOCUMENTATION_ISSUE = "documentation_issue"      # GST/PO/invoice details wrong; payer willing once corrected
CAUSE_AWAITING_AP_APPROVAL = "awaiting_ap_approval"    # payment queued with customer's AP/finance team
CAUSE_PTP_OFFERED = "ptp_offered"                      # customer explicitly promises payment on a future date
CAUSE_DISPUTE = "invoice_dispute"                      # customer disputes/refuses the invoice itself
CAUSE_INSUFFICIENT_CONTEXT = "insufficient_context"    # rules abstention: cannot interpret unstructured context

# --------------------------------------------------------------------------- #
# Playbooks (data-driven registry lives in policy/playbooks.py)
# --------------------------------------------------------------------------- #
PB_DUNNING = "dunning_reminder"
PB_PAYMENT_LINK = "payment_link_resend"
PB_SMART_RETRY = "smart_retry"
PB_PTP = "promise_to_pay"
PB_ESCALATE = "escalate_human"

# Playbooks for causes read from unstructured context (all bounded, non-monetary
# outreach / internal actions; policy engine still authorizes them).
PB_DOCUMENTATION_FIX = "documentation_fix"      # resend corrected invoice/documentation + payment link
PB_AP_COORDINATION = "ap_coordination"          # light AP-status follow-up (no dunning, no escalation)
PB_PROMISE_ACCEPT = "promise_accept"            # confirm an explicit promise-to-pay date, then wait

# --------------------------------------------------------------------------- #
# Atomic actions (whitelisted; nothing else can be executed)
# --------------------------------------------------------------------------- #
ACT_SEND_REMINDER = "send_reminder"        # dunning email/SMS w/ payment link
ACT_CREATE_LINK = "create_payment_link"    # fresh payment link + notify
ACT_RETRY = "retry_payment"                # re-attempt the failed payment
ACT_PTP_FOLLOWUP = "ptp_followup"          # follow-up on a promise-to-pay
ACT_ESCALATE = "escalate_human"            # hand to a human (never automatic money ops)

# Unstructured-context actions (whitelisted; amount is never edited by any of
# these — they are outreach/coordination/documentation, not money ops).
ACT_RESEND_CORRECTED_INVOICE = "resend_corrected_invoice"  # corrected copy + fresh payment link
ACT_REQUEST_AP_UPDATE = "request_ap_update"                # polite AP-status nudge, non-escalating
ACT_CONFIRM_PTP = "confirm_ptp"                            # confirm an explicit promise date; agent waits

# --------------------------------------------------------------------------- #
# Diagnosis paths
# --------------------------------------------------------------------------- #
DIAG_RULES = "rules"
DIAG_LLM = "llm"
DIAG_FUSED = "fused"
DIAG_FLAG_RULES = "DIAGNOSED_BY_RULES"   # audit marker when LLM unavailable
DIAG_FLAG_ABSTAINED = "RULES_ABSTAINED"  # audit marker: rules refused to guess on unstructured context
DIAG_FLAG_INSUFFICIENT = "INSUFFICIENT_CONTEXT"

# --------------------------------------------------------------------------- #
# Escalation / hard-stop reason codes (audit + escalations.reason_code)
# --------------------------------------------------------------------------- #
ESC_COMPLIANCE_DNC = "COMPLIANCE_DNC"
ESC_COMPLIANCE_BANKRUPTCY = "COMPLIANCE_BANKRUPTCY"
ESC_COMPLIANCE_AGE = "COMPLIANCE_AGE"
ESC_FRAUD_AVS = "FRAUD_AVS_MISMATCH"
ESC_FRAUD_AUTH_FAILURES = "FRAUD_REPEATED_AUTH_FAILURES"
ESC_FRAUD_SUSPICIOUS = "FRAUD_SUSPICIOUS_ACTIVITY"
ESC_LOW_CONFIDENCE = "LOW_CONFIDENCE"
ESC_LADDER_EXHAUSTED = "LADDER_EXHAUSTED"
ESC_PTP_BROKEN = "PTP_BROKEN"
ESC_DISPUTE = "INVOICE_DISPUTE"

# --------------------------------------------------------------------------- #
# Compliance flag types (customers.compliance_flags)
# --------------------------------------------------------------------------- #
FLAG_DNC = "DNC"
FLAG_BANKRUPTCY = "BANKRUPTCY"
FLAG_LEGAL_HOLD = "LEGAL_HOLD"

# --------------------------------------------------------------------------- #
# Policy verdicts
# --------------------------------------------------------------------------- #
VERDICT_APPROVED = "APPROVED"
VERDICT_BLOCKED = "BLOCKED"
VERDICT_REQUIRES_APPROVAL = "REQUIRES_APPROVAL"
VERDICT_DEFERRED = "DEFERRED"  # legal later (cooldown/window), scheduled

# --------------------------------------------------------------------------- #
# Actors for the audit trail
# --------------------------------------------------------------------------- #
ACTOR_SYSTEM = "system"
ACTOR_AGENT_LLM = "agent_llm"
ACTOR_POLICY = "policy"
ACTOR_HUMAN = "human"

# --------------------------------------------------------------------------- #
# Agent execution modes
# --------------------------------------------------------------------------- #
MODE_AUTONOMOUS = "autonomous"
MODE_SUPERVISED = "supervised"
