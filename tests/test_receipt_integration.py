"""End-to-end receipt verification against the real Flask mock sites."""

import pytest

from crawlbudget.budget import make_lease_id
from crawlbudget.demo_site import start_mock_servers, stop_mock_servers
from crawlbudget.pipeline import run_pipeline
from crawlbudget.receipts import sign_receipt
from crawlbudget.util import fixture_bytes, sha256_bytes
from tests.receipt_fixtures import (
    PAGES,
    RUN,
    CrashAfterResponse,
    CrashBeforeGet,
    accounting_by_job,
    c_events,
    planned_rows,
    stage_transfer,
)


@pytest.fixture
def served(tmp_path):
    hooks = {}
    config, storage, reports = stage_transfer(tmp_path)
    servers = start_mock_servers(config, PAGES, PAGES / "manifest.yaml",
                                 receipt_hooks=hooks)
    yield config, storage, reports, servers, hooks
    stop_mock_servers(servers)
    storage.close()


def _serve_with_hooks(tmp_path, hooks):
    config, storage, reports = stage_transfer(tmp_path)
    servers = start_mock_servers(config, PAGES, PAGES / "manifest.yaml",
                                 receipt_hooks=hooks)
    return config, storage, reports, servers


def _crash_c(config, storage, reports):
    with pytest.raises(RuntimeError, match="simulated crash"):
        run_pipeline(config, CrashAfterResponse(storage, "c.html"), RUN,
                     reports_dir=reports, sleeper=lambda _: None)
    intents = storage.get_intents(RUN, state="dispatched")
    assert len(intents) == 1
    return intents[0]


def test_mock_site_receipt_settles_interrupted_run_via_audit(served):
    config, storage, reports, servers, _hooks = served
    intent = _crash_c(config, storage, reports)
    # the mock site recorded signed receipts for both arrivals
    assert len(servers[0].receipts) == 2
    receipt = servers[0].receipts[intent["intent_id"]]
    assert receipt["lease_id"] == intent["lease_id"]
    result = run_pipeline(config, storage, RUN, reports_dir=reports,
                          sleeper=lambda _: None)
    assert planned_rows(storage)["c.html"]["final_outcome"] == "receipt_undelivered"
    assert len(servers[0].receipts) == 2  # no new collection happened
    assert result.summary["receipt_verification"]["receipt_undelivered"] == 1
    assert result.status == "completed"


def test_mock_site_recovery_returns_the_served_body(served):
    config, storage, reports, servers, _hooks = served
    _crash_c(config, storage, reports)
    result = run_pipeline(config, storage, RUN, reports_dir=reports,
                          sleeper=lambda _: None, receipt_contract="recover")
    assert planned_rows(storage)["c.html"]["final_outcome"] == "recovered_delivery"
    expected = fixture_bytes(PAGES / "lease-demo_page_c.html")
    event = c_events(storage)[0]
    assert event["body_sha256"] == sha256_bytes(expected)
    assert event["response_bytes"] == len(expected)
    assert len(servers[0].receipts) == 2
    assert result.summary["receipt_verification"]["recovered_delivery"] == 1


def test_mock_site_definitive_absence_allows_redispatch(served):
    config, storage, reports, servers, _hooks = served
    with pytest.raises(RuntimeError, match="simulated crash"):
        run_pipeline(config, storage, RUN, reports_dir=reports,
                     session=CrashBeforeGet("c.html"), sleeper=lambda _: None)
    # a.html arrived; c.html never left the client
    assert len(servers[0].receipts) == 1
    assert len(storage.get_intents(RUN, state="dispatched")) == 1
    result = run_pipeline(config, storage, RUN, reports_dir=reports,
                          sleeper=lambda _: None)
    assert planned_rows(storage)["c.html"]["final_outcome"] == "success"
    # exactly one arrival for c.html across both runs
    c_receipts = [receipt for receipt in servers[0].receipts.values()
                  if receipt["path"] == "c.html"]
    assert len(c_receipts) == 1
    assert result.status == "completed"


def test_mock_site_corrupted_receipt_token_quarantines(tmp_path):
    def corrupt(receipt):
        mutated = dict(receipt)
        mutated["token"] = "0" * 64
        return mutated

    config, storage, reports, servers = _serve_with_hooks(
        tmp_path, {"lease-demo": corrupt})
    try:
        _crash_c(config, storage, reports)
        run_pipeline(config, storage, RUN, reports_dir=reports,
                     sleeper=lambda _: None)
        assert planned_rows(storage)["c.html"]["final_outcome"] == "receipt_quarantined"
        assert c_events(storage)[0]["error_code"] == "EXEC_RECEIPT_UNVERIFIABLE"
        assert accounting_by_job(storage)["waiting"]["quarantined"] == 1
    finally:
        stop_mock_servers(servers)
        storage.close()


def test_mock_site_conflicting_lease_receipt_quarantines(tmp_path):
    priority_lease = make_lease_id(RUN, "lease-demo", "priority")

    def conflict(receipt):
        return sign_receipt("lease-demo", {**receipt, "lease_id": priority_lease})

    config, storage, reports, servers = _serve_with_hooks(
        tmp_path, {"lease-demo": conflict})
    try:
        _crash_c(config, storage, reports)
        run_pipeline(config, storage, RUN, reports_dir=reports,
                     sleeper=lambda _: None)
        assert planned_rows(storage)["c.html"]["final_outcome"] == "receipt_quarantined"
        assert c_events(storage)[0]["error_code"] == "EXEC_RECEIPT_CONFLICT"
        assert any(block["reason"] == "receipt_identity_conflict"
                   for block in storage.get_blocked(RUN))
    finally:
        stop_mock_servers(servers)
        storage.close()
