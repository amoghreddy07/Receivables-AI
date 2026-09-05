"""Deterministic fusion + fallback contract.

When the LLM is unavailable or disabled, diagnosis is pure rules (path=rules).
When both are available the two opinions are fused with EXACT semantics:

  RULES ABSTENTION (rule cause = insufficient_context):
      The deterministic rules explicitly refused to guess because unstructured
      context (emails/AP comms) carries the answer. This is NOT a disagreement:
      - LLM present    -> LLM carries (path=llm). A diagnosis below the
        autonomous band (< 0.5) is still escalated (never acted on blind).
      - LLM unavailable -> flagged rules path -> policy escalates (no guess).

  agreement (same cause):
      fused = 0.5*rule_conf + 0.5*llm_conf
      >= 0.65  -> autonomous action allowed
      0.5..0.65-> action requires human approval
      < 0.5    -> escalate (low confidence)
  disagreement (different causes, rules did NOT abstain):
      NEVER auto-act -> escalate with both diagnoses stored (audited)

These thresholds are unit-tested in tests/test_fusion_fallback.py.
"""
from __future__ import annotations

from dataclasses import dataclass

from app import constants as C


@dataclass
class FusedDiagnosis:
    cause: str
    confidence: float
    path: str            # rules | fused | llm
    flagged: bool        # low-confidence / disagreement -> never autonomous
    proposed_playbook: str = ""
    reason: str = ""
    rule: dict | None = None
    llm: dict | None = None

    @property
    def autonomous_ok(self) -> bool:
        return not self.flagged and self.confidence >= 0.65

    @property
    def requires_approval(self) -> bool:
        return not self.flagged and 0.5 <= self.confidence < 0.65


AUTONOMOUS_CONF = 0.65
APPROVAL_CONF_LOW = 0.5


def fuse(rule: dict, llm: dict | None, *, llm_enabled: bool) -> FusedDiagnosis:
    rule_cause, rule_conf = rule["cause"], float(rule["confidence"])
    rule_playbook = rule.get("proposed_playbook", "")

    abstained = rule_cause == C.CAUSE_INSUFFICIENT_CONTEXT

    if not llm_enabled or llm is None:
        flagged = rule_conf < APPROVAL_CONF_LOW or abstained
        reason = (
            "rules abstained on unstructured context; llm unavailable -> escalate, never guess"
            if abstained
            else "llm unavailable or disabled -> deterministic rules diagnosis"
        )
        return FusedDiagnosis(
            cause=rule_cause,
            confidence=rule_conf,
            path=C.DIAG_RULES,
            flagged=flagged,
            proposed_playbook="" if abstained else rule_playbook,
            reason=reason,
            rule=rule,
            llm=None,
        )

    llm_cause, llm_conf = llm["cause"], float(llm["confidence"])
    llm_playbook = llm.get("proposed_playbook", "")

    # Rules abstained -> the LLM's reading of the unstructured context carries.
    # The policy engine still runs compliance/fraud/stopping rules before any
    # action, so this is LLM-proposes / policy-disposes, never direct authority.
    if abstained:
        if llm_cause == C.CAUSE_UNKNOWN:
            # the LLM also could not read the thread -> escalate, no guess
            return FusedDiagnosis(
                cause=C.CAUSE_INSUFFICIENT_CONTEXT,
                confidence=0.35,
                path=C.DIAG_LLM,
                flagged=True,
                proposed_playbook="",
                reason="rules abstained and llm could not interpret the context -> escalate",
                rule=rule,
                llm=llm,
            )
        flagged = llm_conf < APPROVAL_CONF_LOW
        return FusedDiagnosis(
            cause=llm_cause,
            confidence=llm_conf,
            path=C.DIAG_LLM,
            flagged=flagged,
            proposed_playbook=llm_playbook,
            reason="rules abstained (unstructured context) -> llm-only diagnosis" if not flagged else "llm-only diagnosis below autonomous confidence -> escalate",
            rule=rule,
            llm=llm,
        )

    if rule_cause == llm_cause:
        conf = 0.5 * rule_conf + 0.5 * llm_conf
        flagged = conf < APPROVAL_CONF_LOW
        reason = "rule+llm agreement" if not flagged else "rule+llm agree but fused confidence too low"
        return FusedDiagnosis(
            cause=rule_cause,
            confidence=conf,
            path=C.DIAG_FUSED,
            flagged=flagged,
            proposed_playbook=llm_playbook or rule_playbook,
            reason=reason,
            rule=rule,
            llm=llm,
        )

    return FusedDiagnosis(
        cause=rule_cause,  # rules stay authoritative for labelling; no action
        confidence=min(rule_conf, llm_conf),
        path=C.DIAG_FUSED,
        flagged=True,
        proposed_playbook="",
        reason=f"rule({rule_cause}) and llm({llm_cause}) disagree -> escalate, never auto-act",
        rule=rule,
        llm=llm,
    )
