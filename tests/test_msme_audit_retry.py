"""Pure unit tests for the shared audited-transaction retry policy (no database)."""

from contextlib import nullcontext

import pytest

from msme.compliance import audit


class FakeDb:
    def atomic(self):
        return nullcontext()


def exc(name, message="boom"):
    return type(name, (Exception,), {})(message)


def test_classification_of_retryable_errors():
    assert audit.is_retryable(exc("UniqueViolation", "duplicate key value violates unique constraint"))
    assert audit.is_retryable(exc("IntegrityError", "UNIQUE constraint failed: x.seq"))
    assert audit.is_retryable(exc("DeadlockDetected", "deadlock detected"))
    assert audit.is_retryable(exc("SerializationFailure", "could not serialize access"))
    assert not audit.is_retryable(exc("IntegrityError", "FOREIGN KEY constraint failed"))
    assert not audit.is_retryable(exc("OperationalError", "no such table"))
    assert not audit.is_retryable(ValueError("unique"))                       # right words, wrong exception type


def test_retries_until_success_then_returns_the_result():
    calls = []

    def fn():
        calls.append(1)
        if len(calls) < 3:
            raise exc("DeadlockDetected")
        return "done"

    assert audit.run_in_transaction(FakeDb(), fn) == "done" and len(calls) == 3


def test_non_retryable_errors_propagate_immediately():
    calls = []

    def fn():
        calls.append(1)
        raise exc("IntegrityError", "FOREIGN KEY constraint failed")

    with pytest.raises(Exception, match="FOREIGN KEY"):
        audit.run_in_transaction(FakeDb(), fn)
    assert len(calls) == 1


def test_gives_up_with_audit_busy_after_the_attempt_limit():
    calls = []

    def fn():
        calls.append(1)
        raise exc("UniqueViolation", "duplicate key")

    with pytest.raises(audit.AuditBusy):
        audit.run_in_transaction(FakeDb(), fn, attempts=4)
    assert len(calls) == 4
