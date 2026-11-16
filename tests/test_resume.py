from dataclasses import replace

import pytest

from crawlbudget.pipeline import run_pipeline
from crawlbudget.storage import Storage
from tests.fakes import demo_replay_session


class _InterruptingSession:
    def __init__(self, fail_after: int) -> None:
        self.inner = demo_replay_session()
        self.fail_after = fail_after
        self.calls = self.inner.calls
        self.trust_env = False
    def get(self, url, **kwargs):
        if len(self.calls) >= self.fail_after:
            raise RuntimeError("simulated interruption")
        return self.inner.get(url, **kwargs)


def test_interrupted_pipeline_resumes_without_refetching_completed(demo_config, tmp_path):
    with Storage(tmp_path / "ledger.sqlite3") as storage:
        interrupting = _InterruptingSession(fail_after=4)
        with pytest.raises(RuntimeError):
            run_pipeline(demo_config, storage, "resume-run",
                         reports_dir=tmp_path / "reports", session=interrupting,
                         sleeper=lambda delay: None)
        assert storage.get_run("resume-run")["status"] == "running"
        assert storage.attempt_count("resume-run") == 4
        completed = [row for row in storage.get_planned("resume-run") if row["completed"]]
        assert len(completed) == 4
        assert storage.get_report("resume-run") is None
        resumed = demo_replay_session()
        result = run_pipeline(demo_config, storage, "resume-run",
                              reports_dir=tmp_path / "reports", session=resumed,
                              sleeper=lambda delay: None)
        assert result.status == "completed"
        assert storage.attempt_count("resume-run") == 17
        assert len(resumed.calls) == 13
        assert all(url not in resumed.calls for url in interrupting.calls)
        assert storage.get_run("resume-run")["status"] == "completed"
        assert storage.get_report("resume-run") is not None
        assert result.summary["totals"]["executed_requests"] == 14
        assert [l["used_requests"] for l in storage.get_leases("resume-run")] == [6, 6, 2]
        assert result.report_paths["json"].exists()


def test_resume_requires_matching_fingerprints(demo_config, tmp_path):
    with Storage(tmp_path / "ledger.sqlite3") as storage:
        run_pipeline(demo_config, storage, "fingerprint-run",
                     reports_dir=tmp_path / "reports", dry_run=True)
        changed = replace(demo_config, seeds_fingerprint="f" * 64)
        with pytest.raises(ValueError, match="different config/seed/robots"):
            run_pipeline(changed, storage, "fingerprint-run",
                         reports_dir=tmp_path / "reports", dry_run=True)


def test_dry_run_never_calls_transport(demo_config, tmp_path):
    class _ForbiddenSession:
        trust_env = False
        def get(self, *args, **kwargs):
            raise AssertionError("dry-run must not perform HTTP")
    with Storage(tmp_path / "ledger.sqlite3") as storage:
        result = run_pipeline(demo_config, storage, "dry-run",
                              reports_dir=tmp_path / "reports", dry_run=True,
                              session=_ForbiddenSession())
        assert result.status == "dry_run"
        assert result.summary["run"]["dry_run"] is True
        assert result.summary["request_status_counts"] == {}
        assert storage.attempt_count("dry-run") == 0
        assert result.summary["totals"]["executed_requests"] == 0
        assert result.summary["totals"]["granted_requests"] == 14
