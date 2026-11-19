import copy
import json

import pytest

from crawlbudget.pipeline import run_pipeline
from crawlbudget.report import build_summary
from crawlbudget.storage import Storage
from crawlbudget.util import fingerprint
from tests.fakes import demo_replay_session


@pytest.fixture
def executed(tmp_path, demo_config):
    storage = Storage(tmp_path / "ledger.sqlite3")
    result = run_pipeline(demo_config, storage, "demo-run",
                          reports_dir=tmp_path / "reports",
                          session=demo_replay_session(), sleeper=lambda delay: None)
    yield storage, result, tmp_path
    storage.close()


def test_summary_contract_and_self_referential_fingerprint(executed):
    _storage, result, _tmp = executed
    summary = result.summary
    assert summary["schema_version"] == "1.0"
    assert summary["run"]["status"] == "completed"
    assert summary["totals"] == {
        "sites": 3, "jobs": 3, "planned_requests": 21, "granted_requests": 14,
        "executed_requests": 14, "http_attempts": 17, "blocked_events": 9,
        "response_bytes": 3189,
        "remote_receipts_verified": 0, "receipt_quarantined": 0,
        "receipt_recovered": 0,
    }
    assert summary["receipt_attestations"] == []
    assert summary["request_status_counts"] == {"200": 10, "302": 1, "404": 2, "500": 4}
    assert summary["top_blocked_reasons"][0] == {"reason": "robots_disallow", "count": 3}
    assert len(summary["robots_matches"]) == 5
    assert summary["warnings"] == []
    clone = copy.deepcopy(summary)
    stored = clone["fingerprints"]["report"]
    clone["fingerprints"]["report"] = ""
    assert fingerprint(clone) == stored
    assert len(stored) == 64


def test_report_files_are_stable_and_self_contained(executed):
    storage, result, _tmp = executed
    paths = result.report_paths
    assert all(path.exists() for path in paths.values())
    json_bytes = paths["json"].read_bytes()
    md_bytes = paths["markdown"].read_bytes()
    html_bytes = paths["html"].read_bytes()
    assert b"\r\n" not in json_bytes and b"\r\n" not in md_bytes and b"\r\n" not in html_bytes
    assert b"http" not in html_bytes.replace(b"http://127.0.0.1", b"")
    assert b"<script" not in html_bytes.lower()
    assert b"<link" not in html_bytes.lower()
    assert b"@import" not in html_bytes.lower()
    json.loads(json_bytes)
    assert "# crawlbudget run demo-run" in md_bytes.decode()
    report = storage.get_report("demo-run")
    assert report["summary_md"].encode() == md_bytes
    assert report["summary_html"].encode() == html_bytes


def test_summary_is_rebuilt_from_ledger(executed, demo_config):
    storage, result, _tmp = executed
    rebuilt = build_summary(demo_config, result.plan, storage, storage.get_run("demo-run"))
    assert rebuilt == result.summary
