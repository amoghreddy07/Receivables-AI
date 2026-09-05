"""Deterministic offline NLU diagnoser (evaluation stand-in, NOT production AI).

ReceivablesAI's real AI path calls an OpenAI model through
`llm_diagnoser.diagnose_with_llm` (one-shot structured JSON over the redacted
case snapshot). Evaluations cannot depend on a network model, so `llm_mode=auto`
WITHOUT an API key routes here when the caller explicitly enables it
(`AgentConfig.offline_llm=True` — the evaluation runner does this; production
never does).

Honesty contract (docs/SANDBOX.md): this module is a deterministic emulation of
the model's intended prompt behaviour. It reads the SAME unstructured snapshot
field the real model sees (`communication`) and extracts meaning with explicit
patterns — it is keyword NLU, not an LLM, and every result is quoted against
the message text it came from. It proves the ARCHITECTURE and the EVALUATION:
unstructured cases the deterministic rules classifier cannot read (it abstains)
become interpretable here. The real demo/live path substitutes an actual model
for the same job.

The rules classifier NEVER reads message text; this module NEVER reads payment
error codes or invoice state — the two layers see disjoint evidence on purpose,
which is what makes the rules-only vs AI-enabled comparison meaningful.
"""
from __future__ import annotations

import re
from datetime import datetime

from app import constants as C
from app.diagnosis.llm_diagnoser import LLMDiagnosis

MODEL_TAG = "offline-simulated-nlu-v1"

_MONTHS = {
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
    "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12,
}
_MONTH_RE = "|".join(_MONTHS)

# documentation blocker: invoice/GST/PO details wrong -> resend corrected copy
_DOC_SUBJECTS = ("gst", "gstin", "po reference", "purchase order", "invoice number", "invoice details", "tax invoice")
_DOC_ACTIONS = ("resend", "corrected", "correction", "correct the", "fix", "update")
_DOC_FAULTS = ("incorrect", "wrong", "mismatch", "error", "incorrectly", "not matching", "does not match")
# payment is WILLING: the counterparty states they will pay once unblocked
_DOC_PAY_FLOW = ("will pay", "release payment", "release the payment", "pay once", "pay as soon", "remit", "transfer")

_DISPUTE_TOKENS = (
    "dispute", "disputing", "disputed", "did not receive", "never received",
    "not received", "wrong amount", "incorrect amount", "overcharged",
    "charged twice", "double charge", "not rendered", "not authorize",
    "will not pay", "will not be paying", "not be paying", "won't pay",
    "won't be paying", "refuse to pay", "refuse payment", "not paying",
    "no longer wish to pay", "do not intend to pay", "without our approval",
)

_AP_PEOPLE = ("accounts payable", "ap team", "ap department", "finance team", "finance dept", "procurement", "approval", "approved")
_AP_FLOW = ("release", "released", "process", "processed", "queue", "queued", "scheduled", "within", "shortly", "next week", "week of", "funded", "will pay")

_PAY_FLOW = (
    "will pay", "pay by", "pay on", "payments by", "release payment",
    "release the payment", "will transfer", "transfer the", "will remit",
    "remit", "settle by", "make payment on", "make payment by",
)


def _find_date(text: str) -> str:
    """Return the first concrete calendar date found as YYYY-MM-DD (\"\" none)."""
    m = re.search(r"\b(20\d{2})[-/.](\d{1,2})[-/.](\d{1,2})\b", text)
    if m:
        try:
            return datetime(int(m.group(1)), int(m.group(2)), int(m.group(3))).date().isoformat()
        except ValueError:
            pass
    m = re.search(rf"\b(\d{{1,2}})(?:st|nd|rd|th)?\s+({_MONTH_RE})[a-z]*\s+(20\d{{2}})\b", text, re.IGNORECASE)
    if m:
        try:
            return datetime(int(m.group(3)), _MONTHS[m.group(2).lower()], int(m.group(1))).date().isoformat()
        except ValueError:
            pass
    m = re.search(rf"\b({_MONTH_RE})[a-z]*\s+(\d{{1,2}})(?:st|nd|rd|th)?,?\s+(20\d{{2}})\b", text, re.IGNORECASE)
    if m:
        try:
            return datetime(int(m.group(3)), _MONTHS[m.group(1).lower()], int(m.group(2))).date().isoformat()
        except ValueError:
            pass
    return ""


def _quote(text: str, span: int = 140) -> str:
    return text.strip()[:span]


def _match_evidence(messages: list[dict], tokens: tuple[str, ...]) -> str:
    """First message sentence containing any of the tokens, for evidence."""
    for m in messages:
        low = (m.get("content") or "").lower()
        for tok in tokens:
            idx = low.find(tok)
            if idx >= 0:
                return _quote(m["content"][max(0, idx - 60): idx + 120])
    return ""


def offline_diagnose(snapshot: dict) -> LLMDiagnosis | None:
    """Deterministic NLU over the snapshot's inbound communication transcript."""
    messages = [
        m for m in (snapshot.get("communication") or [])
        if m.get("direction") == "in" and len((m.get("content") or "").strip()) >= 20
    ]
    text = " \n ".join((m.get("content") or "") for m in messages)
    low = text.lower()

    if not text.strip():
        return LLMDiagnosis(
            cause=C.CAUSE_UNKNOWN, confidence=0.3, evidence=[],
            rationale="no inbound unstructured context available",
            proposed_playbook=C.PB_ESCALATE, model=MODEL_TAG,
        )

    # 1) documentation blocker (customer willing but invoice details wrong)
    if any(t in low for t in _DOC_ACTIONS) and any(t in low for t in _DOC_SUBJECTS + ("invoice", "copy")) and (
        any(t in low for t in _DOC_FAULTS) or any(t in low for t in _DOC_SUBJECTS)
    ):
        return LLMDiagnosis(
            cause=C.CAUSE_DOCUMENTATION_ISSUE, confidence=0.88,
            evidence=[f"customer asks for correction: {_quote(text)}"],
            rationale="The customer is willing to pay but blocked on incorrect invoice/documentation details and asks us to resend a corrected copy.",
            proposed_playbook=C.PB_DOCUMENTATION_FIX, model=MODEL_TAG,
        )

    # 2) dispute / refusal (never auto-collect from a disputing customer)
    if any(t in low for t in _DISPUTE_TOKENS):
        return LLMDiagnosis(
            cause=C.CAUSE_DISPUTE, confidence=0.92,
            evidence=[f"customer contests the invoice: {_match_evidence(messages, _DISPUTE_TOKENS)}"],
            rationale="The customer disputes the invoice or states they will not pay; this belongs to a human, not an automated recovery action.",
            proposed_playbook=C.PB_ESCALATE, model=MODEL_TAG,
        )

    # 3) awaiting AP / internal approval (payment queued, do not escalate)
    if any(t in low for t in _AP_PEOPLE) and any(t in low for t in _AP_FLOW):
        return LLMDiagnosis(
            cause=C.CAUSE_AWAITING_AP_APPROVAL, confidence=0.85,
            evidence=[f"AP status: {_quote(text)}"],
            rationale="Payment is with the customer's accounts-payable/finance team and expected to move; an AP-status follow-up fits better than dunning or escalation.",
            proposed_playbook=C.PB_AP_COORDINATION, model=MODEL_TAG,
        )

    # 4) explicit promise to pay on a concrete future date
    if any(t in low for t in _PAY_FLOW):
        promised = _find_date(text)
        if promised:
            as_of = (snapshot.get("as_of") or "")[:10]
            if not as_of or promised >= as_of:
                return LLMDiagnosis(
                    cause=C.CAUSE_PTP_OFFERED, confidence=0.9,
                    evidence=[f"promise in message: {_quote(text)}"],
                    rationale="The customer explicitly promises payment on a concrete future date; the agent should confirm the promise and wait until then.",
                    proposed_playbook=C.PB_PROMISE_ACCEPT, model=MODEL_TAG,
                    promised_date_iso=promised,
                )

    return LLMDiagnosis(
        cause=C.CAUSE_UNKNOWN, confidence=0.3,
        evidence=[f"context not confidently readable: {_quote(text)}"],
        rationale="No deterministic pattern confidently matches the unstructured context.",
        proposed_playbook=C.PB_ESCALATE, model=MODEL_TAG,
    )
