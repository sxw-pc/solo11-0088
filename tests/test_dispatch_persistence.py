"""Persistence tests for pre-dispatch intents and receipt settlement rows."""

import pytest

from crawlbudget.storage import Storage


def _intent(state: str = "dispatched") -> dict:
    return {
        "dispatch_id": "d" * 64, "run_id": "run", "site_id": "site",
        "job_id": "job", "url_hash": "u" * 64,
        "url": "http://127.0.0.1/site/c.html", "attempt": 1,
        "lease_id": "lease-1", "state": state,
        "created_at": "2026-01-15T00:00:00Z", "updated_at": "2026-01-15T00:00:00Z",
    }


def _receipt(state: str = "recorded", *, lease_id: str = "lease-1") -> dict:
    return {
        "dispatch_id": "d" * 64, "run_id": "run", "site_id": "site",
        "job_id": "job", "url_hash": "u" * 64, "attempt": 1,
        "lease_id": lease_id, "receipt_state": state,
        "delivery_conclusion": "not_delivered", "receipt_json": "{}",
        "recorded_at": "2026-01-15T00:00:00Z",
    }


@pytest.fixture
def store():
    storage = Storage(":memory:")
    yield storage
    storage.close()


def test_intent_reactivates_when_a_stable_identity_is_redispatched(store):
    assert store.insert_dispatch_intent(_intent()) is True
    store.update_dispatch_intent_state(
        "d" * 64, "superseded", "2026-01-15T00:00:01Z")
    assert store.get_dispatch_intent("d" * 64)["state"] == "superseded"
    # The same deterministic id returning after a definite absence reopens.
    reopened = dict(_intent(), updated_at="2026-01-15T00:00:02Z")
    store.insert_dispatch_intent(reopened)
    row = store.get_dispatch_intent("d" * 64)
    assert row["state"] == "dispatched"
    assert row["updated_at"] == "2026-01-15T00:00:02Z"


def test_open_intents_only_returns_dispatched_rows(store):
    store.insert_dispatch_intent(_intent())
    other = dict(_intent(), dispatch_id="e" * 64, state="settled")
    store.insert_dispatch_intent(other)
    open_rows = store.get_open_dispatch_intents("run")
    assert [row["dispatch_id"] for row in open_rows] == ["d" * 64]


def test_receipt_upsert_moves_an_existing_row_to_quarantine(store):
    assert store.insert_dispatch_receipt(_receipt()) is True
    # A second plain insert is ignored so recorded evidence is immutable.
    assert store.insert_dispatch_receipt(_receipt("quarantined")) is False
    assert store.get_dispatch_receipts("run")[0]["receipt_state"] == "recorded"
    # The explicit upsert is the controlled state transition.
    store.upsert_dispatch_receipt(
        dict(_receipt("quarantined"), settled_at="2026-01-15T00:00:03Z",
             conflict_detail="signature mismatch"))
    row = store.get_dispatch_receipts("run")[0]
    assert row["receipt_state"] == "quarantined"
    assert row["conflict_detail"] == "signature mismatch"
    assert row["settled_at"] == "2026-01-15T00:00:03Z"


def test_reset_drops_intent_and_receipt_tables(store):
    store.insert_dispatch_intent(_intent())
    store.insert_dispatch_receipt(_receipt())
    store.reset()
    assert store.get_open_dispatch_intents("run") == []
    assert store.get_dispatch_receipts("run") == []
