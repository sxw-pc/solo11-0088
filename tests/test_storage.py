import pytest

from crawlbudget.models import (
    BlockedEvent, Lease, PlannedRequest, RequestEvent, RunRecord, WarningRecord,
)
from crawlbudget.storage import Storage


def _run(run_id: str = "run", day: str = "2026-01-15", status: str = "running") -> RunRecord:
    stamp = f"{day}T00:00:00Z"
    return RunRecord(run_id=run_id, created_at=stamp, started_at=stamp,
                     finished_at=f"{day}T00:00:01Z", virtual_date=day, dry_run=False,
                     status=status, config_fingerprint="c" * 64,
                     seeds_fingerprint="s" * 64, robots_fingerprint="r" * 64)


def _lease(run_id: str = "run") -> Lease:
    return Lease(lease_id="lease-1", run_id=run_id, site_id="mock-alpha", job_id="job-1",
                 granted_requests=1, used_requests=0, remaining_requests=1,
                 expires_at="2026-01-16T00:00:00Z", reason="granted")


def _planned(run_id: str = "run") -> PlannedRequest:
    url = "http://127.0.0.1:1/mock-alpha/a"
    return PlannedRequest(run_id=run_id, site_id="mock-alpha", job_id="job-1",
                          request_order=1, url=url, url_hash="a" * 64, lease_id="lease-1",
                          allowed=True, block_reason=None, block_evidence={},
                          virtual_offset_ms=0, robots_evidence={}, seeded_url=url)


def _event(run_id: str = "run", attempt: int = 1) -> RequestEvent:
    return RequestEvent(run_id=run_id, site_id="mock-alpha", job_id="job-1",
                        url_hash="a" * 64, attempt=attempt,
                        url="http://127.0.0.1:1/mock-alpha/a", status_code=200,
                        outcome="success", elapsed_ms=1, response_bytes=10,
                        content_type="text/html", body_sha256="b" * 64, error_code=None,
                        evidence={}, recorded_at="2026-01-15T00:00:00Z")


@pytest.fixture
def store():
    storage = Storage(":memory:")
    yield storage
    storage.close()


def test_run_lifecycle_lease_idempotency_and_empty_queries(store):
    store.create_run(_run())
    store.upsert_lease(_lease())
    store.insert_planned(_planned())
    assert store.complete_planned("run", "mock-alpha", "a" * 64, "success") is True
    assert store.complete_planned("run", "mock-alpha", "a" * 64, "success") is False
    lease = store.get_leases("run")[0]
    assert (lease["used_requests"], lease["remaining_requests"]) == (1, 0)
    assert store.get_planned("run")[0]["completed"] == 1
    store.update_run("run", status="completed")
    assert store.get_run("run")["status"] == "completed"
    assert store.list_runs()[0]["run_id"] == "run"
    assert store.get_run("missing") is None
    assert store.get_report("missing") is None
    assert store.get_planned("missing") == []
    with pytest.raises(ValueError):
        store.count_rows("runs", "run")


def test_request_blocked_and_warning_writes_are_idempotent(store):
    store.create_run(_run())
    assert store.insert_request_event(_event()) is True
    assert store.insert_request_event(_event()) is False
    assert store.insert_request_event(_event(attempt=2)) is True
    assert store.attempt_count("run") == 2
    assert len(store.get_request_events("run", url_hash="a" * 64)) == 2
    blocked = BlockedEvent(
        run_id="run", site_id="mock-alpha", job_id="job-1", url_hash="a" * 64,
        url="http://127.0.0.1:1/a", stage="plan", reason="robots_disallow",
        evidence={"rule": "R001"}, recorded_at="2026-01-15T00:00:00Z",
    )
    store.insert_blocked(blocked)
    store.insert_blocked(blocked)
    assert store.count_rows("blocked_events", "run") == 1
    warning = WarningRecord(run_id="run", code="W", message="message",
                            context={"x": 1}, recorded_at="2026-01-15T00:00:00Z")
    store.add_warning(warning)
    store.add_warning(warning)
    assert store.count_rows("warnings", "run") == 1


def test_daily_quota_usage_spans_runs_but_can_exclude_current(store):
    store.create_run(_run("run-1"))
    store.create_run(_run("run-2"))
    store.insert_request_event(_event("run-1"))
    store.insert_request_event(_event("run-1", attempt=2))
    store.insert_request_event(_event("run-2"))
    assert store.daily_quota_used("mock-alpha", "2026-01-15") == 3
    assert store.daily_quota_used("mock-alpha", "2026-01-15", exclude_run_id="run-2") == 2
    assert store.daily_quota_used("mock-alpha", "2026-01-16") == 0


def test_reports_persist_and_update(store):
    store.create_run(_run())
    store.save_report("run", '{"a":1}', "# md", "<html></html>", "f" * 64,
                      "2026-01-15T00:00:00Z")
    assert store.get_report("run")["summary_json"] == '{"a":1}'
    assert store.get_run("run")["report_fingerprint"] == "f" * 64
    store.save_report("run", '{"a":2}', "# md2", "<html>2</html>", "e" * 64,
                      "2026-01-15T00:00:01Z")
    assert store.get_report("run")["summary_md"] == "# md2"
    assert store.get_run("run")["report_fingerprint"] == "e" * 64


def test_reset_and_create_run_preserve_started_at(store):
    store.create_run(_run())
    store.upsert_lease(_lease())
    store.insert_planned(_planned())
    store.reset()
    assert store.list_runs() == [] and store.get_leases("run") == []
    assert store.get_planned("run") == []
    store.create_run(_run())
    store.create_run(_run(day="2026-02-01", status="completed"))
    row = store.get_run("run")
    assert row["started_at"] == "2026-01-15T00:00:00Z"
    assert row["status"] == "completed"
