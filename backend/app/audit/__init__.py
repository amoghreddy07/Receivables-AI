"""Append-only, tamper-evident audit log.

Chain rule: for event *i* with canonical payload *p_i*,
    hash_i = SHA256(prev_hash_i + p_i),   prev_hash_i = hash_{i-1}
The first event links to a genesis constant. `verify()` walks the full global
chain and returns the first row whose recomputed hash does not match — that row
is the tamper point, and we surface it.

The canonical payload is `json.dumps(payload, sort_keys=True, separators=(",", ":"))`
so whitespace/ordering differences can never produce a different hash.
"""
from __future__ import annotations

import hashlib
import json

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.clock import now
from app.models import AuditEvent

GENESIS = "GENESIS"


def canonical_json(payload: dict) -> str:
    return json.dumps(payload or {}, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _hash(prev_hash: str, canonical: str) -> str:
    return hashlib.sha256((prev_hash + canonical).encode("utf-8")).hexdigest()


def audit_event(
    db: Session,
    *,
    actor: str,
    action: str,
    payload: dict | None = None,
    case_id: int | None = None,
) -> AuditEvent:
    """Append one audit event inside the caller's transaction."""
    payload = payload or {}
    canonical = canonical_json(payload)
    prev = db.scalar(select(AuditEvent).order_by(AuditEvent.seq.desc()).limit(1))
    prev_hash = prev.hash if prev is not None else GENESIS
    seq = (prev.seq + 1) if prev is not None else 1
    ev = AuditEvent(
        seq=seq,
        case_id=case_id,
        actor=actor,
        action=action,
        payload=payload,
        canonical=canonical,
        prev_hash=prev_hash,
        hash=_hash(prev_hash, canonical),
        created_at=now(),
    )
    db.add(ev)
    return ev


def verify(db: Session) -> tuple[bool, AuditEvent | None, str]:
    """Walk the whole chain. Returns (ok, first_broken_event, message)."""
    rows = db.scalars(select(AuditEvent).order_by(AuditEvent.seq.asc())).all()
    if not rows:
        return True, None, "empty chain"
    prev_hash = GENESIS
    for ev in rows:
        expected = _hash(prev_hash, ev.canonical)
        if ev.hash != expected:
            return False, ev, f"hash mismatch at seq={ev.seq} (event #{ev.id})"
        prev_hash = ev.hash
    return True, None, f"chain valid ({len(rows)} events)"


def verify_case(db: Session, case_id: int) -> tuple[bool, AuditEvent | None, str]:
    """Chain integrity for events belonging to a case (chain itself is global)."""
    ok, broken, msg = verify(db)
    if not ok:
        return False, broken, msg
    return True, None, msg


def case_events(db: Session, case_id: int):
    return db.scalars(
        select(AuditEvent).where(AuditEvent.case_id == case_id).order_by(AuditEvent.seq.asc())
    ).all()
