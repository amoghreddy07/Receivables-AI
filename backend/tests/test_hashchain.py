"""Hash-chained audit trail: append-only integrity + tamper detection.

hash_i = SHA256(prev_hash_i + canonical_payload_i); verify() recomputes the
whole chain and reports the first broken row. The dashboard demonstrates the
same logic live (valid green state, DEMO_MODE tamper -> red state).
"""
from __future__ import annotations

from app.audit import audit_event, canonical_json, verify
from app.models import AuditEvent


def test_append_and_verify(db):
    audit_event(db, actor="system", action="case_opened", payload={"a": 1})
    audit_event(db, actor="policy", action="policy_decision", payload={"b": [1, 2]})
    audit_event(db, actor="system", action="action_result", payload={"ok": True})
    db.commit()

    ok, broken, msg = verify(db)
    assert ok and broken is None
    assert "chain valid (3 events)" in msg

    rows = db.query(AuditEvent).order_by(AuditEvent.seq).all()
    assert rows[0].prev_hash == "GENESIS"
    assert rows[1].prev_hash == rows[0].hash
    assert rows[2].prev_hash == rows[1].hash


def test_tampered_payload_is_detected(db):
    audit_event(db, actor="system", action="one", payload={"v": 1})
    audit_event(db, actor="policy", action="two", payload={"v": 2})
    audit_event(db, actor="system", action="three", payload={"v": 3})
    db.commit()

    ok, broken, _ = verify(db)
    assert ok and broken is None

    # attacker edits the middle event's canonical payload (e.g. changes the
    # approved amount or drops an audit field)
    middle = db.query(AuditEvent).filter(AuditEvent.action == "two").one()
    middle.canonical = canonical_json({"v": 9999})
    db.commit()

    ok, broken, _ = verify(db)
    assert not ok
    assert broken is not None and broken.action == "two"


def test_tampered_hash_link_is_detected(db):
    audit_event(db, actor="system", action="one", payload={"v": 1})
    audit_event(db, actor="system", action="two", payload={"v": 2})
    db.commit()
    first = db.query(AuditEvent).filter(AuditEvent.action == "one").one()
    first.hash = "0" * 64  # attacker rewrites a hash to hide their edit
    db.commit()
    ok, broken, _ = verify(db)
    assert not ok
    assert broken is not None and broken.action == "one"


def test_middle_row_deletion_is_detected(db):
    """Deleting a middle event breaks the link into its successor."""
    audit_event(db, actor="system", action="one", payload={"v": 1})
    audit_event(db, actor="system", action="two", payload={"v": 2})
    audit_event(db, actor="system", action="three", payload={"v": 3})
    db.commit()
    middle = db.query(AuditEvent).filter(AuditEvent.action == "two").one()
    db.delete(middle)
    db.commit()
    ok, broken, _ = verify(db)
    assert not ok
    assert broken is not None and broken.action == "three"
