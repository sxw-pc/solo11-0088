from dataclasses import replace

from crawlbudget.errors import EXEC_BODY_TOO_LARGE, EXEC_PREFLIGHT_BLOCKED
from crawlbudget.executor import ControlledExecutor
from crawlbudget.storage import Storage
from tests.conftest import prepare_plan
from tests.fakes import ReplaySession, demo_replay_session


def _run_executor(storage, config, plan, session, sleeps=None):
    sleeper = sleeps.append if sleeps is not None else (lambda delay: None)
    return ControlledExecutor(
        storage, config.sites, plan, session=session, sleeper=sleeper
    ).run()


def test_full_replayed_execution_records_everything(demo_config, demo_plan):
    with Storage(":memory:") as storage:
        prepare_plan(storage, demo_plan)
        session = demo_replay_session()
        sleeps: list[float] = []
        stats = _run_executor(storage, demo_config, demo_plan, session, sleeps)
        assert stats == {"attempts": 17, "completed": 14, "skipped": 0, "blocked": 2,
                         "receipt_verified": 0, "receipt_quarantined": 0}
        assert len(session.calls) == 17
        assert all(url.startswith("http://127.0.0.1:") for url in session.calls)
        assert all("example.invalid" not in url for url in session.calls)
        events = storage.get_request_events(demo_plan.run_id)
        assert len(events) == 17
        statuses = sorted(event["status_code"] for event in events)
        assert (statuses.count(200), statuses.count(500), statuses.count(404),
                statuses.count(302)) == (10, 4, 2, 1)
        success = next(event for event in events if event["status_code"] == 200)
        assert success["response_bytes"] > 0 and len(success["body_sha256"]) == 64
        leases = storage.get_leases(demo_plan.run_id)
        assert [lease["used_requests"] for lease in leases] == [6, 6, 2]
        assert {e["reason"] for e in storage.get_blocked(demo_plan.run_id)} == {
            "response_body_too_large", "redirect_non_loopback"
        }
        assert sleeps and all(delay > 0 for delay in sleeps)


def test_oversized_response_is_blocked(demo_config, demo_plan):
    with Storage(":memory:") as storage:
        prepare_plan(storage, demo_plan)
        _run_executor(storage, demo_config, demo_plan, demo_replay_session())
        big = next(event for event in storage.get_request_events(demo_plan.run_id)
                   if event["url"].endswith("/big.html"))
        assert big["outcome"] == "body_too_large"
        assert big["response_bytes"] == 0
        assert big["error_code"] == EXEC_BODY_TOO_LARGE
        assert any(event["url"].endswith("/big.html")
                   and event["reason"] == "response_body_too_large"
                   for event in storage.get_blocked(demo_plan.run_id))


def test_retries_stop_after_configured_attempts(demo_config, demo_plan):
    with Storage(":memory:") as storage:
        prepare_plan(storage, demo_plan)
        _run_executor(storage, demo_config, demo_plan, demo_replay_session())
        events = storage.get_request_events(demo_plan.run_id)
        fail_events = [e for e in events if e["url"].endswith("/always-fail.html")]
        assert [e["attempt"] for e in fail_events] == [1, 2, 3]
        assert fail_events[-1]["outcome"] == "http_error"
        flaky = [e for e in events if e["url"].endswith("/flaky.html")]
        assert [e["status_code"] for e in flaky] == [500, 200]
        assert flaky[-1]["outcome"] == "success"


def test_preflight_recheck_blocks_mutated_url(demo_config, demo_plan):
    single = replace(demo_plan.requests[0], url="http://example.invalid/mock-alpha/index.html")
    plan = replace(demo_plan, requests=[single], leases=[demo_plan.leases[0]])
    with Storage(":memory:") as storage:
        prepare_plan(storage, plan)
        session = ReplaySession({})
        stats = _run_executor(storage, demo_config, plan, session)
        assert stats["attempts"] == 0 and stats["blocked"] == 1 and session.calls == []
        assert storage.get_blocked("test-run")[0]["reason"] == EXEC_PREFLIGHT_BLOCKED


def test_hard_attempt_cap_blocks_retries(demo_config, demo_plan):
    plan = replace(demo_plan, attempt_limits={**demo_plan.attempt_limits, "mock-beta": 2})
    with Storage(":memory:") as storage:
        prepare_plan(storage, plan)
        _run_executor(storage, demo_config, plan, demo_replay_session())
        beta_blocks = [event for event in storage.get_blocked(plan.run_id)
                       if event["site_id"] == "mock-beta" and event["stage"] == "execute"]
        assert beta_blocks
        assert beta_blocks[0]["reason"] in {"quota_exceeded", "run_limit_exceeded"}
