"""Receipt-verified settlements: audit/recovery contracts and redispatch."""

import json

import pytest

from crawlbudget.pipeline import run_pipeline
from crawlbudget.util import sha256_bytes
from tests.fakes import FakeReceiptVerifier, FakeResponse, ReplaySession
from tests.receipt_fixtures import (
    RUN,
    CrashOnResolveIntent,
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


def test_received_receipt_audit_contract_settles_without_delivery(transferred):
    config, storage, reports = transferred
    _rows, intent = crash_with_c_routes(config, storage, reports)
    body = b"c"
    verifier = FakeReceiptVerifier(
        receipts={intent["intent_id"]: receipt_for(intent, body)},
        bodies={intent["intent_id"]: (body, "text/html; charset=utf-8")},
    )
    result, resumed = _resume(config, storage, reports, verifier)
    # the verified receipt replaces any new collection for c.html
    assert resumed.calls == []
    assert verifier.verified == [intent["intent_id"]]
    assert verifier.fetched == []  # the audit contract never retrieves bodies
    row = planned_rows(storage)["c.html"]
    assert row["completed"] == 1 and row["final_outcome"] == "receipt_undelivered"
    events = c_events(storage)
    assert len(events) == 1
    event = events[0]
    assert event["outcome"] == "receipt_undelivered"
    # the receipt alone never proves delivery: no status, bytes, or body hash
    assert event["status_code"] is None
    assert event["response_bytes"] == 0
    assert event["body_sha256"] == ""
    evidence = json.loads(event["evidence_json"])
    assert evidence["delivery"] == "not_delivered"
    assert evidence["settlement"] == "audit"
    assert evidence["receipt"]["body_sha256"] == sha256_bytes(body)
    # the consumed amount belongs to the waiting lease; priority cannot regain it
    account = accounting_by_job(storage)
    assert (account["waiting"]["consumed"], account["waiting"]["transferred_in"]) == (1, 1)
    assert (account["priority"]["consumed"], account["priority"]["transferred_out"]) == (1, 1)
    assert account["waiting"]["quarantined"] == 0
    leases = {lease["job_id"]: lease for lease in storage.get_leases(RUN)}
    assert leases["waiting"]["used_requests"] == 1
    assert leases["priority"]["used_requests"] == 1
    assert result.status == "completed"
    assert result.summary["receipt_verification"]["receipt_undelivered"] == 1
    assert result.summary["receipt_verification"]["recovered_delivery"] == 0
    # a further resume is stable: no verification, no dispatch, same conclusions
    verifier2 = FakeReceiptVerifier()
    followup, again = _resume(config, storage, reports, verifier2)
    assert again.calls == [] and verifier2.verified == []
    assert planned_rows(storage)["c.html"]["final_outcome"] == "receipt_undelivered"
    assert sum(row["consumed"] for row in accounting_by_job(storage).values()) == 2
    assert followup.summary["receipt_verification"]["receipt_undelivered"] == 1


def test_received_receipt_recovery_contract_recovers_body(transferred):
    config, storage, reports = transferred
    _rows, intent = crash_with_c_routes(config, storage, reports)
    body = b"recovered c.html body"
    verifier = FakeReceiptVerifier(
        receipts={intent["intent_id"]: receipt_for(intent, body)},
        bodies={intent["intent_id"]: (body, "text/html; charset=utf-8")},
    )
    result, resumed = _resume(config, storage, reports, verifier, "recover")
    assert resumed.calls == []  # recovery adds no collection request
    assert verifier.fetched == [intent["intent_id"]]
    row = planned_rows(storage)["c.html"]
    assert row["final_outcome"] == "recovered_delivery"
    events = c_events(storage)
    assert len(events) == 1  # no new collection consumption was recorded
    event = events[0]
    assert event["outcome"] == "recovered_delivery"  # never claimed as "success"
    assert event["body_sha256"] == sha256_bytes(body)
    assert event["response_bytes"] == len(body)
    evidence = json.loads(event["evidence_json"])
    assert evidence["delivery"] == "recovered"
    assert evidence["original_response"] == "not_recorded_locally"
    # site consumption is identical to the audit route: one unit on waiting
    assert storage.attempt_count(RUN, "lease-demo") == 2
    account = accounting_by_job(storage)
    assert account["waiting"]["consumed"] == 1
    assert account["priority"]["consumed"] == 1
    assert result.summary["receipt_verification"]["recovered_delivery"] == 1
    assert result.summary["receipt_verification"]["receipt_undelivered"] == 0


def test_audit_and_recovery_contracts_give_distinct_truthful_conclusions(tmp_path):
    outcomes, accounts, summaries = {}, {}, {}
    for contract in ("audit", "recover"):
        config, storage, reports = stage_transfer(tmp_path / contract)
        _rows, intent = crash_with_c_routes(config, storage, reports)
        body = b"c"
        verifier = FakeReceiptVerifier(
            receipts={intent["intent_id"]: receipt_for(intent, body)},
            bodies={intent["intent_id"]: (body, "text/html; charset=utf-8")},
        )
        result, _session = _resume(config, storage, reports, verifier, contract)
        outcomes[contract] = planned_rows(storage)["c.html"]["final_outcome"]
        accounts[contract] = accounting_by_job(storage)
        summaries[contract] = result.summary["receipt_verification"]
        storage.close()
    # same receipt, two legal routes, different truthful delivery conclusions
    assert outcomes == {"audit": "receipt_undelivered", "recover": "recovered_delivery"}
    for contract in ("audit", "recover"):
        assert accounts[contract]["waiting"]["consumed"] == 1
        assert accounts[contract]["priority"]["consumed"] == 1
        assert accounts[contract]["waiting"]["quarantined"] == 0
    assert summaries["audit"]["receipt_undelivered"] == 1
    assert summaries["audit"]["recovered_delivery"] == 0
    assert summaries["recover"]["recovered_delivery"] == 1
    assert summaries["recover"]["receipt_undelivered"] == 0


def test_definitive_not_received_allows_redispatch_within_budget(transferred):
    config, storage, reports = transferred
    _rows, intent = crash_with_c_routes(config, storage, reports)
    verifier = FakeReceiptVerifier()  # the site vouches: nothing ever arrived
    url = planned_rows(storage)["c.html"]["url"]
    session = ReplaySession({url: [FakeResponse(200, body=b"c")]})
    result = run_pipeline(config, storage, RUN, reports_dir=reports,
                          session=session, sleeper=lambda _: None,
                          receipt_verifier=verifier)
    assert verifier.verified == [intent["intent_id"]]
    assert session.calls == [url]
    row = planned_rows(storage)["c.html"]
    assert row["final_outcome"] == "success"
    events = c_events(storage)
    assert len(events) == 1 and events[0]["attempt"] == 1
    assert events[0]["outcome"] == "success"
    intents = storage.get_intents(RUN)
    assert {item["state"] for item in intents} == {"settled"}
    assert result.summary["receipt_verification"]["intent_states"] == {"settled": 2}
    assert result.status == "completed"


def test_orphaned_intent_reconciles_without_site_query(transferred):
    config, storage, reports = transferred
    rows = planned_rows(storage)
    session = ReplaySession({
        rows["a.html"]["url"]: [FakeResponse(200, body=b"a")],
        rows["c.html"]["url"]: [FakeResponse(200, body=b"c")],
    })
    with pytest.raises(RuntimeError, match="simulated crash"):
        run_pipeline(config, CrashOnResolveIntent(storage), RUN,
                     reports_dir=reports, session=session, sleeper=lambda _: None)
    # a.html's event is recorded but its intent was never settled
    events = storage.get_request_events(RUN)
    assert [event["url"] for event in events] == [rows["a.html"]["url"]]
    assert len(storage.get_intents(RUN, state="dispatched")) == 1
    verifier = FakeReceiptVerifier()
    resumed = ReplaySession({rows["c.html"]["url"]: [FakeResponse(200, body=b"c")]})
    result = run_pipeline(config, storage, RUN, reports_dir=reports,
                          session=resumed, sleeper=lambda _: None,
                          receipt_verifier=verifier)
    assert verifier.verified == []  # reconciliation is purely local
    assert resumed.calls == [rows["c.html"]["url"]]  # a.html is not re-fetched
    assert {item["state"] for item in storage.get_intents(RUN)} == {"settled"}
    assert result.status == "completed"


def test_clean_run_settles_all_intents_and_rerun_is_stable(transferred):
    config, storage, reports = transferred
    rows = planned_rows(storage)
    session = ReplaySession({
        rows["a.html"]["url"]: [FakeResponse(200, body=b"a")],
        rows["c.html"]["url"]: [FakeResponse(200, body=b"c")],
    })
    result = run_pipeline(config, storage, RUN, reports_dir=reports,
                          session=session, sleeper=lambda _: None)
    assert result.status == "completed"
    intents = storage.get_intents(RUN)
    assert len(intents) == 2
    assert {item["state"] for item in intents} == {"settled"}
    assert {item["resolution"] for item in intents} == {"success"}
    verifier = FakeReceiptVerifier()
    followup, again = _resume(config, storage, reports, verifier)
    assert again.calls == [] and verifier.verified == []
    assert followup.summary["receipt_verification"]["intent_states"] == {"settled": 2}
    assert storage.attempt_count(RUN) == 2
    assert planned_rows(storage)["c.html"]["final_outcome"] == "success"
