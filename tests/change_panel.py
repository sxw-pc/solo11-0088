"""Shared helpers for seeding the sparse two-state change panel in tests."""

import json
from pathlib import Path

from crawlbudget.change_monitor import build_change_monitor
from crawlbudget.config import load_config
from crawlbudget.models import RequestEvent, RunRecord
from crawlbudget.storage import Storage
from crawlbudget.util import sha256_text
from tests.fakes import FakeResponse, ReplaySession

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "changes" / "two_state_panel.json"
URL_TEMPLATE = "http://127.0.0.1:5021/mock-alpha/{page}.html"
MODEL = "symmetric_two_state_continuous_time_markov"


def workload(**overrides):
    declaration = {
        "declared": True, "model": MODEL, "stationary": True,
        "independent": True, "common_rate": True, "symmetric": True,
        "two_state": True, "threshold": 0.5, "min_calibration_intervals": 8,
    }
    declaration.update(overrides)
    return declaration


def panel():
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


def seed_panel(storage: Storage, states, *, events_mutator=None):
    """Write one run per synthetic day; a page state of null is a failed capture."""
    for index, day in enumerate(states["days"], start=1):
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
            events = [{
                "page": page, "state": state, "attempt": 1,
                "outcome": "success" if state else "transport_error_final",
            }]
            if events_mutator is not None:
                events = events_mutator(run_id, page, state, events)
            for item in events:
                has_state = item["state"] is not None
                storage.insert_request_event(RequestEvent(
                    run_id=run_id, site_id="mock-alpha", job_id="monitor",
                    url_hash=sha256_text(item["page"]), attempt=item["attempt"],
                    url=URL_TEMPLATE.format(page=item["page"]),
                    status_code=200 if has_state else 503,
                    outcome=item["outcome"],
                    elapsed_ms=1, response_bytes=1 if has_state else 0,
                    content_type="text/html",
                    body_sha256=(
                        sha256_text(item["page"] + "-" + item["state"])
                        if has_state else ""
                    ),
                    error_code=None, evidence={}, recorded_at="2026-02-01T00:00:00Z",
                ))


def pages(report):
    return {page["url"].rsplit("/", 1)[-1]: page for page in report["pages"]}


def panel_report(storage, declaration=None):
    return build_change_monitor(
        storage, [f"panel-{index}" for index in range(1, 6)],
        **({"workload": declaration} if declaration is not None else {}),
    )


def pipeline_config(root: Path):
    """A hermetic five-page site used for real executor-to-monitor runs."""
    names = ["p1", "p2", "p3", "p4", "target"]
    robots = root / "robots.txt"
    robots.write_text("User-agent: crawlbudget\nAllow: /mock-alpha/\n", encoding="utf-8")
    seed = root / "seed.txt"
    seed.write_text(
        "\n".join(
            f"http://127.0.0.1:6501/mock-alpha/{page}.html" for page in names
        ) + "\n", encoding="utf-8",
    )
    config_path = root / "config.yaml"
    config_path.write_text(
        f"""
sites:
  - site_id: mock-alpha
    base_url: http://127.0.0.1:6501/mock-alpha/
    robots_file: {robots}
    daily_quota: 10
    max_requests_per_run: 10
    max_rps: 50
    burst: 5
    crawl_delay_ms: 1
    max_response_bytes: 4096
    max_retries: 0
    timeout_ms: 1000
    enabled: true
jobs:
  - job_id: job-monitor
    site_id: mock-alpha
    priority: 1
    seed_file: {seed}
    enabled: true
""", encoding="utf-8",
    )
    return load_config(config_path, project_root=root)


def session_for(day):
    routes = {}
    for page, state in day["states"].items():
        url = f"http://127.0.0.1:6501/mock-alpha/{page}.html"
        if state:
            routes[url] = [FakeResponse(
                200, body=(page + "-" + state).encode(),
                headers={"Content-Type": "text/html"},
            )]
        else:
            routes[url] = [FakeResponse(503)]
    return ReplaySession(routes)
