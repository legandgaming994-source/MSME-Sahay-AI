"""Tamper-evident audit trail: a SHA-256 hash chain.

Each event hash covers its own fields AND the previous event's hash, so editing, deleting, inserting or
reordering any past event breaks verification from that point on. Events are appended inside the same
transaction as the change they describe (both commit or neither does); `seq` is UNIQUE so concurrent
appenders cannot fork the chain: the loser's transaction fails and is retried by the service.

Honest limit: the chain lives in the same database. Someone able to rewrite the whole table consistently
could forge a valid chain. Anchor `head()` outside the database (it is printed on every report) to detect that.
"""

from __future__ import annotations

import hashlib
import json
import time
import uuid
from typing import Any, Callable, Optional

GENESIS = "0" * 64
_SEP = "\x1f"


def canonical(detail: dict[str, Any]) -> str:
    return json.dumps(detail, sort_keys=True, separators=(",", ":"), ensure_ascii=True, default=str)


def event_hash(seq: int, ts: str, actor: str, action: str, entity_type: str, entity_id: str,
               detail: str, prev_hash: str) -> str:
    material = _SEP.join([str(seq), ts, actor, action, entity_type, entity_id, detail, prev_hash])
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def append(db, ts: str, actor: str, action: str, entity_type: str, entity_id: str,
           detail: Optional[dict[str, Any]] = None) -> dict:
    """Append one event. MUST be called inside the caller's transaction."""
    last = db.sql('SELECT seq, hash FROM "MSME Audit Event" ORDER BY seq DESC LIMIT 1')
    seq = (last[0]["seq"] + 1) if last else 1
    prev = last[0]["hash"] if last else GENESIS
    body = canonical(detail or {})
    digest = event_hash(seq, ts, actor, action, entity_type, entity_id, body, prev)
    db.insert("MSME Audit Event", {
        "id": "MAUD-" + uuid.uuid4().hex[:12], "seq": seq, "ts": ts, "actor": actor, "action": action,
        "entity_type": entity_type, "entity_id": entity_id, "detail": body, "prev_hash": prev, "hash": digest})
    return {"seq": seq, "hash": digest}


def head(db) -> dict:
    last = db.sql('SELECT seq, hash FROM "MSME Audit Event" ORDER BY seq DESC LIMIT 1')
    return {"seq": last[0]["seq"], "hash": last[0]["hash"]} if last else {"seq": 0, "hash": GENESIS}


def verify(db) -> dict:
    """Recompute the whole chain. Returns {ok, events, first_bad_seq, reason}."""
    rows = db.sql('SELECT seq, ts, actor, action, entity_type, entity_id, detail, prev_hash, hash '
                  'FROM "MSME Audit Event" ORDER BY seq')
    prev, expected_seq = GENESIS, 1
    for r in rows:
        if r["seq"] != expected_seq:
            return {"ok": False, "events": len(rows), "first_bad_seq": expected_seq,
                    "reason": f"sequence gap or reorder: expected {expected_seq}, found {r['seq']}"}
        if r["prev_hash"] != prev:
            return {"ok": False, "events": len(rows), "first_bad_seq": r["seq"], "reason": "previous-hash link broken"}
        recomputed = event_hash(r["seq"], r["ts"], r["actor"], r["action"], r["entity_type"], r["entity_id"],
                                r["detail"], r["prev_hash"])
        if recomputed != r["hash"]:
            return {"ok": False, "events": len(rows), "first_bad_seq": r["seq"], "reason": "event content altered"}
        prev, expected_seq = r["hash"], expected_seq + 1
    return {"ok": True, "events": len(rows), "first_bad_seq": None, "reason": None}


class AuditBusy(RuntimeError):
    """Too many concurrent audit appends; the caller should report 'busy' and let the user retry."""


_RETRYABLE_NAMES = ("DeadlockDetected", "SerializationFailure")


def _is_unique_violation(exc: Exception) -> bool:
    text = str(exc).lower()
    return type(exc).__name__ in ("IntegrityError", "UniqueViolation") and ("unique" in text or "duplicate" in text)


def is_retryable(exc: Exception) -> bool:
    """Errors where re-running the whole transaction is the correct response: a concurrent writer took the same
    audit sequence number or unique key (unique violation), or PostgreSQL aborted this transaction to resolve a
    lock cycle or serialization conflict. After the retry the loser sees the winner's committed state and fails
    cleanly with a domain error (e.g. 'already matched') instead of crashing."""
    return _is_unique_violation(exc) or type(exc).__name__ in _RETRYABLE_NAMES


def run_in_transaction(db, fn: Callable[[], Any], attempts: int = 10) -> Any:
    """Run ``fn`` (domain write + audit append) in one transaction, retrying when a concurrent appender took
    the same sequence number. Any other exception propagates after the transaction rolls back."""
    for attempt in range(attempts):
        try:
            with db.atomic():
                return fn()
        except Exception as exc:  # noqa: BLE001
            if is_retryable(exc):
                time.sleep(0.005 * (attempt + 1))
                continue
            raise
    raise AuditBusy("audit log is busy")
