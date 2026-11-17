import json
from pathlib import Path

from crawlbudget.change_monitor import build_change_monitor
from crawlbudget.models import RequestEvent, RunRecord
from crawlbudget.storage import Storage
from crawlbudget.util import sha256_text


FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "changes" / "observations.json"


def test_change_monitor_tracks_change_interval_and_failed_capture_gap():
    fixture = json.loads(FIXTURE.read_text(encoding="utf-8"))
    with Storage(":memory:") as storage:
        for run in fixture["runs"]:
            day = run["virtual_date"]
            storage.create_run(RunRecord(
                run_id=run["run_id"], created_at=day + "T00:00:00Z",
                started_at=day + "T00:00:00Z", finished_at=day + "T01:00:00Z",
                virtual_date=day, dry_run=False, status="completed",
                config_fingerprint="config", seeds_fingerprint="seeds",
                robots_fingerprint="robots",
            ))
            for item in run["events"]:
                storage.insert_request_event(RequestEvent(
                    run_id=run["run_id"], site_id=item["site_id"], job_id="job",
                    url_hash=item["url_hash"], attempt=item["attempt"], url=item["url"],
                    status_code=200 if item["outcome"] == "success" else 503,
                    outcome=item["outcome"], elapsed_ms=1, response_bytes=1,
                    content_type="text/html", body_sha256=item["body_sha256"],
                    error_code=None, evidence={}, recorded_at=day + "T00:00:00Z",
                ))
        report = build_change_monitor(storage, ["watch-03", "watch-01", "watch-02"])
    assert report["run_ids"] == ["watch-01", "watch-02", "watch-03"]
    pages = {page["url_hash"]: page for page in report["pages"]}
    assert pages["page-a"]["intervals"] == [{
        "from_run": "watch-01", "to_run": "watch-03",
        "from_date": "2026-01-01", "to_date": "2026-01-03",
        "different_bodies": True, "intervening_gaps": ["watch-02"],
    }]
    assert all(not x["different_bodies"] for x in pages["page-b"]["intervals"])
    assert pages["page-a"]["latest_body_sha256"] == "ccc"


def test_two_state_panel_retains_long_gap_without_claiming_no_transient_change():
    panel = json.loads(FIXTURE.with_name("two_state_panel.json").read_text(encoding="utf-8"))
    with Storage(":memory:") as storage:
        for index, day in enumerate(panel["days"], start=1):
            run_id = f"panel-{index}"
            date = day["date"]
            storage.create_run(RunRecord(
                run_id=run_id, created_at=date + "T00:00:00Z",
                started_at=date + "T00:00:00Z", finished_at=date + "T01:00:00Z",
                virtual_date=date, dry_run=False, status="completed",
                config_fingerprint="config", seeds_fingerprint="seeds",
                robots_fingerprint="robots",
            ))
            for page, state in day["states"].items():
                storage.insert_request_event(RequestEvent(
                    run_id=run_id, site_id="mock-alpha", job_id="monitor",
                    url_hash=sha256_text(page), attempt=1,
                    url=f"http://127.0.0.1:5021/mock-alpha/{page}.html",
                    status_code=200 if state else 503,
                    outcome="success" if state else "transport_error_final",
                    elapsed_ms=1, response_bytes=1 if state else 0,
                    content_type="text/html",
                    body_sha256=sha256_text(page + "-" + state) if state else "",
                    error_code=None, evidence={}, recorded_at=date + "T00:00:00Z",
                ))
        report = build_change_monitor(storage, [f"panel-{i}" for i in range(1, 6)])
    pages = {page["url"].split("/")[-1]: page for page in report["pages"]}
    target = pages["target.html"]
    assert target["intervals"] == [{
        "from_run": "panel-1", "to_run": "panel-5",
        "from_date": "2026-02-01", "to_date": "2026-02-05",
        "different_bodies": False,
        "intervening_gaps": ["panel-2", "panel-3", "panel-4"],
    }]
    calibration = [pages[f"p{i}.html"] for i in range(1, 5)]
    assert sum(x["different_bodies"] for page in calibration for x in page["intervals"]) == 4
