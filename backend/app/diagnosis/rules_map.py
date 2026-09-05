"""Error-code -> root-cause signal map (single source of truth).

This table is mirrored in docs/SIGNAL_MAP.md. The fraud rules in
app/policy/rules.py read the SAME code sets (imported here) so diagnosis and
safety can never contradict each other.

Rules evaluate in priority order; the first matching rule wins.
"""
from __future__ import annotations

from app import constants as C
from app.policy import rules as R  # noqa: F401  (kept for doc reference)

# code-set -> (cause, base confidence)
CODE_RULES: list[tuple[frozenset, str, float]] = [
    (R.SIGNAL_AVS_CODES, C.CAUSE_FRAUD_SUSPECTED, 0.98),
    (R.SIGNAL_AUTH_CODES, C.CAUSE_AUTH_FAILURE, 0.9),
    (R.SIGNAL_TECH_CODES, C.CAUSE_TECHNICAL, 0.92),
    (R.SIGNAL_FUNDS_CODES, C.CAUSE_INSUFFICIENT_FUNDS, 0.9),
    (R.SIGNAL_EXPIRED_CODES, C.CAUSE_INSTRUMENT_EXPIRED, 0.95),
]

TEXT_RULES: list[tuple[tuple, str, float]] = [
    (R.SIGNAL_FRAUD_TEXTS, C.CAUSE_FRAUD_SUSPECTED, 0.9),
]


def classify_error_code(error_code: str, error_description: str = "") -> tuple[str, float, str]:
    """Returns (cause, confidence, matched-rule-name) for one failed attempt."""
    code = (error_code or "").upper()
    for codes, cause, conf in CODE_RULES:
        if code in codes:
            return cause, conf, f"code:{code}"
    text = f"{code} {error_description or ''}".lower()
    for tokens, cause, conf in TEXT_RULES:
        if any(tok in text for tok in tokens):
            return cause, conf, f"text:{text[:80]}"
    return C.CAUSE_UNKNOWN, 0.4, "no-signal-match"


def infer_cause_from_state(invoice, case) -> tuple[str, float, str]:
    if invoice.paid_amount_minor > 0 and not invoice.is_paid:
        return C.CAUSE_PARTIAL_BALANCE, 0.85, "partial_payment_balance"
    if invoice.status == "overdue" or case.days_overdue > 0:
        return C.CAUSE_OVERDUE, 0.9, "overdue"
    return C.CAUSE_OVERDUE, 0.5, "unpaid_invoice"
