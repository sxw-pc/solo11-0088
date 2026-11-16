from dataclasses import replace
from urllib.parse import urlsplit

import pytest

from crawlbudget.executor import ControlledExecutor
from crawlbudget.pipeline import run_pipeline
from crawlbudget.review_server import create_review_app, serve_review
from crawlbudget.storage import Storage
from tests.conftest import FIXTURES, prepare_plan
from tests.fakes import FakeResponse, ReplaySession


def test_default_session_and_seeds_stay_loopback_only(demo_config, demo_plan):
    with Storage(":memory:") as storage:
        executor = ControlledExecutor(storage, demo_config.sites, demo_plan)
        assert executor.session.trust_env is False
    allowed_hosts = {"127.0.0.1", "::1", "example.invalid"}
    for path in sorted((FIXTURES / "seeds").glob("*.txt")):
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#"):
                parsed = urlsplit(line)
                assert parsed.hostname in allowed_hosts and parsed.scheme == "http"


def test_redirect_to_loopback_is_still_not_followed(demo_config, demo_plan):
    request = replace(demo_plan.requests[0])
    plan = replace(demo_plan, requests=[request], leases=[demo_plan.leases[0]])
    with Storage(":memory:") as storage:
        prepare_plan(storage, plan)
        session = ReplaySession({request.url: [FakeResponse(
            302, headers={"Location": "http://127.0.0.1:5021/mock-alpha/other"})]})
        ControlledExecutor(
            storage, demo_config.sites, plan, session=session, sleeper=lambda delay: None
        ).run()
        assert session.calls == [request.url]
        assert storage.get_blocked(plan.run_id)[0]["reason"] == "redirect_blocked"


def test_review_console_is_read_only_and_refuses_external_bind(demo_config, tmp_path):
    database = tmp_path / "review.sqlite3"
    with Storage(database) as storage:
        run_pipeline(demo_config, storage, "review-run",
                     reports_dir=tmp_path / "reports", dry_run=True)
    client = create_review_app(database).test_client()
    index = client.get("/")
    assert index.status_code == 200
    assert b"No mutation" not in index.data and b"<form" not in index.data.lower()
    detail = client.get("/runs/review-run")
    assert detail.status_code == 200
    assert b"Budget leases" in detail.data and b"read-only" in detail.data
    assert client.post("/runs/review-run").status_code == 405
    assert "default-src 'none'" in detail.headers["Content-Security-Policy"]
    with pytest.raises(ValueError, match="loopback"):
        serve_review(database, host="0.0.0.0", port=8765)
