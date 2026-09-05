"""Executable policy rules.

All rules are DETERMINISTIC functions of persisted case data + clock. The LLM
never influences these: compliance and fraud hard-stops always override AI
output, and the policy engine consults them before every decision AND the
evaluator audits every baseline action with them (un-enforced) so naive agents
produce measurable violations.

Each rule returns a list of RuleHit(code, message, hard) dicts.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from app import constants as C
from app.models import ComplianceFlag, Intervention, Payment


@dataclass
class RuleHit:
    code: str
    message: str
    hard: bool = True

    def as_dict(self) -> dict:
        return {"code": self.code, "message": self.message, "hard": self.hard}


# --------------------------------------------------------------------------- #
# Signal vocabulary (realistic Razorpay-style error codes/descriptions)
# Shared by diagnosis rules-classifier AND fraud rules so they can never
# disagree. See docs/SIGNAL_MAP.md.
# --------------------------------------------------------------------------- #
SIGNAL_AVS_CODES = {"AVS_FAILED", "AVS_MISMATCH", "RISK_AVS_FAILED"}
SIGNAL_FRAUD_TEXTS = ("fraud", "suspicious", "risk_lock", "blocked_by_bank_risk")
SIGNAL_AUTH_CODES = {"AUTH_FAILED", "AUTHENTICATION_FAILED", "2FA_FAILED", "AUTH_DECLINED_BY_ISSUER"}
SIGNAL_TECH_CODES = {"TECHNICAL_ISSUE", "BANK_TECHNICAL_ISSUE", "TIMEOUT", "GATEWAY_TIMEOUT", "NETWORK_ERROR"}
SIGNAL_FUNDS_CODES = {"INSUFFICIENT_FUNDS", "DECLINED_INSUFFICIENT_FUNDS", "BANK_DECLINED_INSUFFICIENT"}
SIGNAL_EXPIRED_CODES = {"CARD_EXPIRED", "INSTRUMENT_EXPIRED", "CARD_EXPIRED_OR_INVALID"}


def compliance_hits(db: Session, case) -> list[RuleHit]:
    """DNC / BANKRUPTCY / LEGAL_HOLD / >120-days checks.

    120-day boundary: `days_overdue > COMPLIANCE_MAX_DAYS` is blocked.
    Exactly 120 days overdue is NOT yet blocked (boundary tests cover 120/121).
    """
    hits: list[RuleHit] = []
    flags = db.scalars(
        select(ComplianceFlag).where(ComplianceFlag.customer_id == case.customer_id)
    ).all()

    flag_types = {f.flag_type for f in flags}
    if C.FLAG_BANKRUPTCY in flag_types:
        hits.append(RuleHit(C.ESC_COMPLIANCE_BANKRUPTCY, "customer flagged BANKRUPTCY — recovery actions hard-stopped"))
    if C.FLAG_DNC in flag_types:
        hits.append(RuleHit(C.ESC_COMPLIANCE_DNC, "customer is DO-NOT-CONTACT — all outreach hard-stopped"))
    if C.FLAG_LEGAL_HOLD in flag_types:
        hits.append(RuleHit(C.ESC_COMPLIANCE_DNC, "customer under LEGAL_HOLD — all outreach hard-stopped"))

    days = getattr(case, "days_overdue", 0) or 0
    if days > 120:
        hits.append(
            RuleHit(
                C.ESC_COMPLIANCE_AGE,
                f"invoice {days} days overdue (> {120} day compliance limit)",
            )
        )
    return hits


def _recent_failed_payments(db: Session, invoice_id: int, within_hours: float = 24 * 7):
    from app.clock import now

    cutoff = now() - timedelta(hours=within_hours)
    return db.scalars(
        select(Payment)
        .where(
            Payment.invoice_id == invoice_id,
            Payment.status == "failed",
            Payment.attempted_at >= cutoff,
        )
        .order_by(Payment.attempted_at.desc())
    ).all()


def fraud_hits(db: Session, case) -> list[RuleHit]:
    """Deterministic fraud hard-stops. Fraud is NEVER retried by any agent."""
    hits: list[RuleHit] = []
    payments = _recent_failed_payments(db, case.invoice_id)

    # (a) AVS mismatch on the most recent attempt
    latest = payments[0] if payments else None
    if latest and (latest.error_code or "").upper() in SIGNAL_AVS_CODES:
        hits.append(RuleHit(C.ESC_FRAUD_AVS, f"AVS mismatch on payment {latest.rzr_payment_id}"))

    # (b) fraud-lock / suspicious-activity text on any recent attempt
    for p in payments:
        text = f"{p.error_code} {p.error_description}".lower()
        if any(tok in text for tok in SIGNAL_FRAUD_TEXTS):
            hits.append(
                RuleHit(
                    C.ESC_FRAUD_SUSPICIOUS,
                    f"fraud-lock / suspicious-activity signal on payment {p.rzr_payment_id}",
                )
            )
            break

    # (c) >= 3 consecutive authorization failures inside 7 days
    auth_failures = [p for p in payments if (p.error_code or "").upper() in SIGNAL_AUTH_CODES]
    if len(auth_failures) >= 3:
        hits.append(
            RuleHit(
                C.ESC_FRAUD_AUTH_FAILURES,
                f"{len(auth_failures)} consecutive authorization failures within 7 days",
            )
        )
    return hits


def stopping_hits(
    db: Session,
    case,
    playbook: str,
    *,
    contact_hour_start: int,
    contact_hour_end: int,
    customer_monthly_cap: int,
    global_daily_budget: int,
    now: datetime,
) -> list[RuleHit]:
    """Attempt caps, cooldowns, contact windows, per-customer + global budgets.

    Returns RuleHit(hard=False) for DEFERRABLE conditions (cooldown/window/
    budget) and hard=True only for hard caps (attempts exhausted).
    """
    from app.policy.playbooks import PLAYBOOKS, executed_count, last_executed_at

    hits: list[RuleHit] = []
    meta = PLAYBOOKS[playbook]

    executed = executed_count(db, case.id, playbook)
    if executed >= meta["max_attempts"]:
        hits.append(
            RuleHit(
                "MAX_ATTEMPTS_REACHED",
                f"playbook {playbook} executed {executed}/{meta['max_attempts']} times",
                hard=True,
            )
        )

    last_at = last_executed_at(db, case.id, playbook)
    if last_at is not None and meta.get("cooldown_hours", 0) > 0:
        elapsed_h = (now - last_at).total_seconds() / 3600
        if elapsed_h < meta["cooldown_hours"]:
            hits.append(
                RuleHit(
                    "IN_COOLDOWN",
                    f"cooldown active ({elapsed_h:.0f}h < {meta['cooldown_hours']}h)",
                    hard=False,
                )
            )

    # Conservative stopping rule: no external action (outreach OR a payment
    # retry, which still hits the issuer/bank rails) executes outside the
    # contact window. Only the internal escalate action is exempt.
    if playbook != C.PB_ESCALATE:
        hour = now.hour
        if not (contact_hour_start <= hour < contact_hour_end):
            hits.append(
                RuleHit(
                    "OUTSIDE_CONTACT_HOURS",
                    f"hour {hour} outside contact window {contact_hour_start}:00–{contact_hour_end}:00",
                    hard=False,
                )
            )

    # per-customer monthly cap
    from sqlalchemy import func

    start_of_month = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    customer_ids = db.scalars(select_for_cases(db, case.customer_id)).all()
    per_customer = db.scalar(
        select(func.count(Intervention.id)).where(
            Intervention.case_id.in_(customer_ids or [0]),
            Intervention.executed_at >= start_of_month,
            Intervention.status.in_(["SUCCEEDED", "FAILED"]),
        )
    ) or 0
    if per_customer >= customer_monthly_cap:
        hits.append(
            RuleHit("CUSTOMER_CAP", f"customer monthly action cap reached ({per_customer})", hard=False)
        )

    start_of_day = now.replace(hour=0, minute=0, second=0, microsecond=0)
    today_actions = db.scalar(
        select(func.count(Intervention.id)).where(
            Intervention.executed_at >= start_of_day,
            Intervention.status.in_(["SUCCEEDED", "FAILED"]),
        )
    ) or 0
    if today_actions >= global_daily_budget:
        hits.append(
            RuleHit("GLOBAL_BUDGET", f"global daily action budget reached ({today_actions})", hard=False)
        )
    return hits


def select_for_cases(db: Session, customer_id: int):
    from sqlalchemy import select

    from app.models import RiskCase

    return select(RiskCase.id).where(RiskCase.customer_id == customer_id)
