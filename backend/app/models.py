"""SQLAlchemy 2.0 ORM models (SQLite for dev/eval; schema is Postgres-friendly).

Money is stored as integer minor units (paise) everywhere — never floats.
"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import JSON, Boolean, DateTime, Float, ForeignKey, Integer, String, Text
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    pass


def _now() -> datetime:
    from app.clock import now  # local import to avoid cycle

    return now()


class Customer(Base):
    __tablename__ = "customers"

    id: Mapped[int] = mapped_column(primary_key=True)
    org_name: Mapped[str] = mapped_column(String(200))
    contact_name: Mapped[str] = mapped_column(String(200), default="")
    email: Mapped[str] = mapped_column(String(200), default="")
    phone: Mapped[str] = mapped_column(String(40), default="")
    timezone: Mapped[str] = mapped_column(String(40), default="Asia/Kolkata")
    behavior_profile: Mapped[str] = mapped_column(String(60), default="default")
    # extra seeded facts used by the deterministic sandbox outcome model
    meta: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now)

    compliance_flags: Mapped[list["ComplianceFlag"]] = relationship(
        back_populates="customer", cascade="all, delete-orphan"
    )
    invoices: Mapped[list["Invoice"]] = relationship(back_populates="customer")


class ComplianceFlag(Base):
    """Executable compliance facts: DNC, BANKRUPTCY, LEGAL_HOLD."""

    __tablename__ = "compliance_flags"

    id: Mapped[int] = mapped_column(primary_key=True)
    customer_id: Mapped[int] = mapped_column(ForeignKey("customers.id"), index=True)
    flag_type: Mapped[str] = mapped_column(String(30), index=True)
    reason_code: Mapped[str] = mapped_column(String(120), default="")
    effective_from: Mapped[datetime] = mapped_column(DateTime, default=_now)
    note: Mapped[str] = mapped_column(Text, default="")

    customer: Mapped["Customer"] = relationship(back_populates="compliance_flags")


class Invoice(Base):
    __tablename__ = "invoices"

    id: Mapped[int] = mapped_column(primary_key=True)
    rzr_invoice_id: Mapped[str] = mapped_column(String(60), unique=True, index=True)
    customer_id: Mapped[int] = mapped_column(ForeignKey("customers.id"), index=True)
    amount_minor: Mapped[int] = mapped_column(Integer)
    paid_amount_minor: Mapped[int] = mapped_column(Integer, default=0)
    currency: Mapped[str] = mapped_column(String(8), default="INR")
    status: Mapped[str] = mapped_column(String(30), default="issued")
    # "issued" | "partially_paid" | "paid" | "overdue"
    due_date: Mapped[datetime] = mapped_column(DateTime)
    issued_at: Mapped[datetime] = mapped_column(DateTime, default=_now)
    notes: Mapped[str] = mapped_column(Text, default="")
    meta: Mapped[dict] = mapped_column(JSON, default=dict)  # outcome seeds etc.

    customer: Mapped["Customer"] = relationship(back_populates="invoices")
    payments: Mapped[list["Payment"]] = relationship(back_populates="invoice")

    @property
    def outstanding_minor(self) -> int:
        return max(0, self.amount_minor - self.paid_amount_minor)

    @property
    def is_paid(self) -> bool:
        return self.paid_amount_minor >= self.amount_minor


class Payment(Base):
    __tablename__ = "payments"

    id: Mapped[int] = mapped_column(primary_key=True)
    rzr_payment_id: Mapped[str] = mapped_column(String(60), unique=True, index=True)
    invoice_id: Mapped[int] = mapped_column(ForeignKey("invoices.id"), index=True)
    customer_id: Mapped[int] = mapped_column(ForeignKey("customers.id"), index=True)
    amount_minor: Mapped[int] = mapped_column(Integer)
    method: Mapped[str] = mapped_column(String(30), default="card")
    status: Mapped[str] = mapped_column(String(30), default="failed")
    error_code: Mapped[str] = mapped_column(String(120), default="")
    error_description: Mapped[str] = mapped_column(Text, default="")
    attempted_at: Mapped[datetime] = mapped_column(DateTime, default=_now)

    invoice: Mapped["Invoice"] = relationship(back_populates="payments")


class RiskCase(Base):
    """One case per invoice — the unit the agent works on."""

    __tablename__ = "risk_cases"

    id: Mapped[int] = mapped_column(primary_key=True)
    case_key: Mapped[str] = mapped_column(String(120), unique=True, index=True)
    entity_type: Mapped[str] = mapped_column(String(30), default="invoice")
    entity_id: Mapped[str] = mapped_column(String(60), default="")
    invoice_id: Mapped[int] = mapped_column(ForeignKey("invoices.id"), index=True)
    customer_id: Mapped[int] = mapped_column(ForeignKey("customers.id"), index=True)
    amount_at_risk_minor: Mapped[int] = mapped_column(Integer, default=0)
    currency: Mapped[str] = mapped_column(String(8), default="INR")
    risk_score: Mapped[float] = mapped_column(Float, default=0.0)
    state: Mapped[str] = mapped_column(String(30), default="OPEN", index=True)
    cause: Mapped[str] = mapped_column(String(40), default="unknown")
    diagnosis_path: Mapped[str] = mapped_column(String(20), default="rules")
    recovered_amount_minor: Mapped[int] = mapped_column(Integer, default=0)
    days_overdue: Mapped[int] = mapped_column(Integer, default=0)
    reason: Mapped[str] = mapped_column(Text, default="")
    next_action_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    opened_at: Mapped[datetime] = mapped_column(DateTime, default=_now)
    closed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    invoice: Mapped["Invoice"] = relationship()
    customer: Mapped["Customer"] = relationship()


class CaseMessage(Base):
    """Inbound/outbound unstructured context on an invoice (emails, support
    conversations, AP communication, payment notes, promise messages).

    This is the information the deterministic rules classifier cannot read — a
    real customer email / chat thread. It flows into the case snapshot given to
    the LLM diagnoser (redacted by app.diagnosis.context_builder) and is the
    reason rules-only operation escalates instead of guessing on these cases.
    """

    __tablename__ = "case_messages"

    id: Mapped[int] = mapped_column(primary_key=True)
    invoice_id: Mapped[int] = mapped_column(ForeignKey("invoices.id"), index=True)
    customer_id: Mapped[int] = mapped_column(ForeignKey("customers.id"), index=True)
    case_id: Mapped[int | None] = mapped_column(ForeignKey("risk_cases.id"), nullable=True, index=True)
    direction: Mapped[str] = mapped_column(String(10), default="in")  # in (customer->us) | out
    channel: Mapped[str] = mapped_column(String(30), default="email")  # email | support_chat | ptp_message | note
    content: Mapped[str] = mapped_column(Text, default="")
    received_at: Mapped[datetime] = mapped_column(DateTime, default=_now)
    meta: Mapped[dict] = mapped_column(JSON, default=dict)


class RiskEvent(Base):
    """Normalized incoming events (webhooks or internal sims). Idempotency store."""

    __tablename__ = "risk_events"

    id: Mapped[int] = mapped_column(primary_key=True)
    event_id: Mapped[str] = mapped_column(String(120), unique=True, index=True)
    event_type: Mapped[str] = mapped_column(String(60), index=True)
    entity_type: Mapped[str] = mapped_column(String(30), default="")
    entity_id: Mapped[str] = mapped_column(String(60), default="")
    payload: Mapped[dict] = mapped_column(JSON, default=dict)
    verified: Mapped[bool] = mapped_column(Boolean, default=True)
    source: Mapped[str] = mapped_column(String(20), default="sim")
    received_at: Mapped[datetime] = mapped_column(DateTime, default=_now)


class Diagnosis(Base):
    __tablename__ = "diagnoses"

    id: Mapped[int] = mapped_column(primary_key=True)
    case_id: Mapped[int] = mapped_column(ForeignKey("risk_cases.id"), index=True)
    cause: Mapped[str] = mapped_column(String(40))
    confidence: Mapped[float] = mapped_column(Float, default=0.0)
    path: Mapped[str] = mapped_column(String(20), default="rules")  # rules|llm|fused
    llm_ok: Mapped[bool] = mapped_column(Boolean, default=False)
    flagged: Mapped[bool] = mapped_column(Boolean, default=False)  # low-confidence flag
    evidence: Mapped[dict] = mapped_column(JSON, default=dict)
    rule_json: Mapped[dict] = mapped_column(JSON, default=dict)
    llm_json: Mapped[dict] = mapped_column(JSON, default=dict)
    proposed_playbook: Mapped[str] = mapped_column(String(40), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now)


class Intervention(Base):
    """Every executed (or attempted) action. Idempotency key = exactly-once."""

    __tablename__ = "interventions"

    id: Mapped[int] = mapped_column(primary_key=True)
    case_id: Mapped[int] = mapped_column(ForeignKey("risk_cases.id"), index=True)
    idempotency_key: Mapped[str] = mapped_column(String(160), unique=True)
    playbook: Mapped[str] = mapped_column(String(40))
    action: Mapped[str] = mapped_column(String(60))
    params: Mapped[dict] = mapped_column(JSON, default=dict)
    status: Mapped[str] = mapped_column(String(30), default="PENDING_APPROVAL", index=True)
    # PENDING_APPROVAL | APPROVED | DECLINED | EXECUTING | SUCCEEDED | FAILED | CANCELLED
    mode: Mapped[str] = mapped_column(String(20), default="autonomous")
    verdict: Mapped[str] = mapped_column(String(30), default="")  # policy verdict
    policy_reasons: Mapped[list] = mapped_column(JSON, default=list)
    outcome: Mapped[dict] = mapped_column(JSON, default=dict)
    error: Mapped[str] = mapped_column(Text, default="")
    recovered_amount_minor: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now)
    executed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class Approval(Base):
    """Human (or system-timeout) decision on a supervised intervention."""

    __tablename__ = "approvals"

    id: Mapped[int] = mapped_column(primary_key=True)
    intervention_id: Mapped[int] = mapped_column(ForeignKey("interventions.id"), index=True)
    decided_by: Mapped[str] = mapped_column(String(30))  # human | system-timeout
    decision: Mapped[str] = mapped_column(String(20))    # APPROVED | DECLINED
    reason: Mapped[str] = mapped_column(Text, default="")
    decided_at: Mapped[datetime] = mapped_column(DateTime, default=_now)


class Escalation(Base):
    __tablename__ = "escalations"

    id: Mapped[int] = mapped_column(primary_key=True)
    case_id: Mapped[int] = mapped_column(ForeignKey("risk_cases.id"), index=True)
    reason_code: Mapped[str] = mapped_column(String(60), index=True)
    context: Mapped[dict] = mapped_column(JSON, default=dict)
    status: Mapped[str] = mapped_column(String(20), default="open", index=True)  # open|resolved
    decision: Mapped[str] = mapped_column(Text, default="")
    decided_by: Mapped[str] = mapped_column(String(30), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now)
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class AuditEvent(Base):
    """Append-only, hash-chained audit log (see app/audit/hashchain.py)."""

    __tablename__ = "audit_events"

    id: Mapped[int] = mapped_column(primary_key=True)
    seq: Mapped[int] = mapped_column(Integer)  # global monotonic position
    case_id: Mapped[int | None] = mapped_column(ForeignKey("risk_cases.id"), nullable=True, index=True)
    actor: Mapped[str] = mapped_column(String(30))
    action: Mapped[str] = mapped_column(String(80))
    payload: Mapped[dict] = mapped_column(JSON, default=dict)
    canonical: Mapped[str] = mapped_column(Text, default="")  # canonical JSON payload
    prev_hash: Mapped[str] = mapped_column(String(64), default="")
    hash: Mapped[str] = mapped_column(String(64), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now)


class PTPPromise(Base):
    __tablename__ = "ptp_promises"

    id: Mapped[int] = mapped_column(primary_key=True)
    case_id: Mapped[int] = mapped_column(ForeignKey("risk_cases.id"), index=True)
    promised_date: Mapped[datetime] = mapped_column(DateTime)
    amount_minor: Mapped[int] = mapped_column(Integer, default=0)
    status: Mapped[str] = mapped_column(String(20), default="active")  # active|kept|broken
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now)


# --------------------------------------------------------------------------- #
# Evaluation tables
# --------------------------------------------------------------------------- #
class EvalCase(Base):
    __tablename__ = "eval_cases"

    id: Mapped[int] = mapped_column(primary_key=True)
    run_id: Mapped[str] = mapped_column(String(60), index=True)
    case_key: Mapped[str] = mapped_column(String(120), index=True)
    scenario: Mapped[str] = mapped_column(String(60))
    seed: Mapped[int] = mapped_column(Integer, default=1)
    agent: Mapped[str] = mapped_column(String(30))  # receivablesai | naive_retry
    llm_mode: Mapped[str] = mapped_column(String(10), default="rules")
    state: Mapped[str] = mapped_column(String(30), default="")
    cause: Mapped[str] = mapped_column(String(40), default="")
    ground_truth: Mapped[dict] = mapped_column(JSON, default=dict)
    actions_taken: Mapped[list] = mapped_column(JSON, default=list)
    violations: Mapped[list] = mapped_column(JSON, default=list)
    recovered_amount_minor: Mapped[int] = mapped_column(Integer, default=0)
    expected_recovery_minor: Mapped[int] = mapped_column(Integer, default=0)
    time_to_recover_hours: Mapped[float | None] = mapped_column(Float, nullable=True)
    correct_action: Mapped[bool] = mapped_column(Boolean, default=False)  # precision signal
    false_action: Mapped[bool] = mapped_column(Boolean, default=False)
    ran_at: Mapped[datetime] = mapped_column(DateTime, default=_now)


class EvalRun(Base):
    __tablename__ = "eval_runs"

    id: Mapped[int] = mapped_column(primary_key=True)
    run_id: Mapped[str] = mapped_column(String(60), unique=True)
    seed: Mapped[int] = mapped_column(Integer)
    llm_mode: Mapped[str] = mapped_column(String(10), default="rules")
    agent: Mapped[str] = mapped_column(String(30))
    started_at: Mapped[datetime] = mapped_column(DateTime, default=_now)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    metrics: Mapped[dict] = mapped_column(JSON, default=dict)
