"""Quarantine of unverifiable or conflicting receipt credentials."""

import json

import pytest

from crawlbudget.budget import make_lease_id
from crawlbudget.lease_lifecycle import record_action
from crawlbudget.pipeline import run_pipeline
from crawlbudget.receipts import VERDICT_UNVERIFIABLE
from tests.fakes import FakeReceiptVerifier, FakeResponse, ReplaySession
from tests.receipt_fixtures import (
    RUN,
    START,
    CrashAfterResponse,
    accounting_by_job,
    c_events,
    crash_with_c_routes,
    planned_rows,
    receipt_for,
    stage_transfer,
)


@pytest.fixture
def transferred(tmp_path):
    config, storage, reports = stage_transfer(tmp_path)
    yield config, storage, reports
    storage.close()


def _resume(config, storage, reports, verifier, contract="audit"):
    session = ReplaySession({})
    result = run_pipeline(config, storage, RUN, reports_dir=reports,
                          session=session, sleeper=lambda _: None,
                          receipt_contract=contract, receipt_verifier=verifier)
    return result, session


def test_unverifiable_receipt_quarantines_amount(transferred):
    config, storage, reports = transferred
    _rows, intent = crash_with_c_routes(config, storage, reports)
    verifier = FakeReceiptVerifier(forced=VERDICT_UNVERIFIABLE)
    result, session = _resume(config, storage, reports, verifier)
    assert session.calls == []  # quarantine never re-dispatches
    row = planned_rows(storage)["c.html"]
    assert row["final_outcome"] == "receipt_quarantined"
    events = c_events(storage)
    assert len(events) == 1
    assert events[0]["outcome"] == "receipt_quarantined"
    assert events[0]["error_code"] == "EXEC_RECEIPT_UNVERIFIABLE"
    assert any(block["reason"] == "receipt_unverifiable"
               for block in storage.get_blocked(RUN))
    # the amount is held in quarantine, not treated as unconsumed
    account = accounting_by_job(storage)
    assert account["waiting"]["consumed"] == 1
    assert account["waiting"]["quarantined"] == 1
    assert account["waiting"]["available"] == 0
    assert result.summary["receipt_verification"]["receipt_quarantined"] == 1
    # re-running never reads the empty-looking ledger as unspent
    verifier2 = FakeReceiptVerifier()
    _followup, again = _resume(config, storage, reports, verifier2)
    assert again.calls == [] and verifier2.verified == []
    assert planned_rows(storage)["c.html"]["final_outcome"] == "receipt_quarantined"
    assert len(c_events(storage)) == 1


def test_conflicting_credentials_quarantine(tmp_path):
    # local ownership conflict: the persisted intent names a lease that no
    # longer owns the URL; remote conflict: a validly signed receipt vouches
    # for a different lease binding. Both quarantine without dispatch.
    for mode in ("local", "remote"):
        config, storage, reports = stage_transfer(tmp_path / mode)
        _rows, intent = crash_with_c_routes(config, storage, reports)
        priority_lease = make_lease_id(RUN, "lease-demo", "priority")
        if mode == "local":
            storage.conn.execute(
                "UPDATE dispatch_intents SET lease_id=? WHERE intent_id=?",
                (priority_lease, intent["intent_id"]))
            storage.conn.commit()
            verifier = FakeReceiptVerifier(
                receipts={intent["intent_id"]: receipt_for(intent, b"c")})
        else:
            receipt = receipt_for(intent, b"c", lease_id=priority_lease)
            verifier = FakeReceiptVerifier(
                receipts={intent["intent_id"]: receipt})
        _result, session = _resume(config, storage, reports, verifier)
        assert session.calls == []
        if mode == "local":
            assert verifier.verified == []  # detected before any site query
        else:
            assert verifier.verified == [intent["intent_id"]]
        row = planned_rows(storage)["c.html"]
        assert row["final_outcome"] == "receipt_quarantined"
        assert c_events(storage)[0]["error_code"] == "EXEC_RECEIPT_CONFLICT"
        assert any(block["reason"] == "receipt_identity_conflict"
                   for block in storage.get_blocked(RUN))
        assert accounting_by_job(storage)["waiting"]["quarantined"] == 1
        storage.close()


def test_recovery_failures_fall_back_to_audit_conclusion(tmp_path):
    bodies = {
        "unavailable": None,
        "body_mismatch": (b"tampered-body", "text/html"),
    }
    for mode, body_entry in bodies.items():
        config, storage, reports = stage_transfer(tmp_path / mode)
        _rows, intent = crash_with_c_routes(config, storage, reports)
        verifier = FakeReceiptVerifier(
            receipts={intent["intent_id"]: receipt_for(intent, b"real-body")},
            bodies={} if body_entry is None else {intent["intent_id"]: body_entry},
        )
        result, session = _resume(config, storage, reports, verifier, "recover")
        assert session.calls == []
        assert verifier.fetched == [intent["intent_id"]]
        row = planned_rows(storage)["c.html"]
        assert row["final_outcome"] == "receipt_undelivered"
        event = c_events(storage)[0]
        assert event["body_sha256"] == ""
        evidence = json.loads(event["evidence_json"])
        assert evidence["recovery_note"] == f"recovery_{mode}"
        assert evidence["delivery"] == "not_delivered"
        assert result.summary["receipt_verification"]["receipt_undelivered"] == 1
        storage.close()


def test_lease_actions_rejected_once_dispatch_intent_exists(transferred):
    config, storage, reports = transferred
    rows = planned_rows(storage)
    session = ReplaySession({rows["a.html"]["url"]: [FakeResponse(200, body=b"a")]})
    with pytest.raises(RuntimeError, match="simulated crash"):
        run_pipeline(config, CrashAfterResponse(storage, "a.html"), RUN,
                     reports_dir=reports, session=session, sleeper=lambda _: None)
    # no event landed, yet dispatch already happened: intents must veto edits
    assert storage.get_request_events(RUN) == []
    assert len(storage.get_intents(RUN, state="dispatched")) == 1
    with pytest.raises(ValueError, match="before HTTP dispatch"):
        record_action(config, storage, RUN, action_id="late-cancel", kind="cancel",
                      source_hash=rows["c.html"]["url_hash"], as_of=START,
                      reason="too late", reports_dir=reports)


def test_blocked_urls_never_gain_intents_or_verdicts(transferred):
    config, storage, reports = transferred
    _rows, intent = crash_with_c_routes(config, storage, reports)
    verifier = FakeReceiptVerifier(
        receipts={intent["intent_id"]: receipt_for(intent, b"c")})
    _resume(config, storage, reports, verifier)
    planned = planned_rows(storage)
    assert planned["secret.html"]["block_reason"] == "robots_disallow"
    assert planned["d.html"]["allowed"] == 0
    intent_urls = {item["url"] for item in storage.get_intents(RUN)}
    assert planned["secret.html"]["url"] not in intent_urls
    assert planned["d.html"]["url"] not in intent_urls
    assert planned["b.html"]["url"] not in intent_urls
