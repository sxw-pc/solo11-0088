"""Offline cases for staged budget decisions and later settlement."""

from pathlib import Path

import pytest

from crawlbudget.config import load_config
from crawlbudget.lease_lifecycle import record_action
from crawlbudget.pipeline import run_pipeline
from crawlbudget.storage import Storage
from tests.fakes import FakeResponse, ReplaySession


ROOT = Path(__file__).resolve().parent.parent
START = "2026-01-15T00:00:00Z"
EXPIRED = "2026-01-16T00:00:00Z"


@pytest.fixture
def staged(tmp_path):
    config = load_config(ROOT / "fixtures/lease_lifecycle.yaml", project_root=ROOT)
    storage = Storage(":memory:")
    reports = str(tmp_path / "reports")
    run_pipeline(config, storage, "lease-case", virtual_start=START,
                 dry_run=True, reports_dir=reports)
    yield config, storage, reports
    storage.close()


def _rows(storage):
    return {row["url"].rsplit("/", 1)[-1]: row
            for row in storage.get_planned("lease-case")}


def test_transfer_replays_into_dispatch_and_settlement(staged):
    config, storage, reports = staged
    rows = _rows(storage)
    assert rows["b.html"]["allowed"] == 1
    assert rows["c.html"]["allowed"] == 0
    record_action(
        config, storage, "lease-case", action_id="move-b-c", kind="transfer",
        source_hash=rows["b.html"]["url_hash"],
        target_hash=rows["c.html"]["url_hash"], as_of=START,
        reason="priority job withdrew its second URL", reports_dir=reports,
    )
    rows = _rows(storage)
    assert rows["b.html"]["block_reason"] == "lease_transferred"
    assert rows["c.html"]["allowed"] == 1
    assert sum(row["allowed"] for row in rows.values()) == 2
    assert storage.get_lease_accounting("lease-case")[1]["transferred_in"] == 1

    session = ReplaySession({
        rows["a.html"]["url"]: [FakeResponse(200, body=b"a")],
        rows["c.html"]["url"]: [FakeResponse(200, body=b"c")],
    })
    run_pipeline(config, storage, "lease-case", reports_dir=reports,
                 session=session, sleeper=lambda _: None)
    assert session.calls == [rows["a.html"]["url"], rows["c.html"]["url"]]
    account = {row["job_id"]: row for row in storage.get_lease_accounting("lease-case")}
    assert (account["priority"]["granted"], account["priority"]["consumed"]) == (1, 1)
    assert (account["waiting"]["granted"], account["waiting"]["consumed"]) == (1, 1)
    assert all(row["state"] == "settled" for row in account.values())
    replay = ReplaySession({})
    run_pipeline(config, storage, "lease-case", reports_dir=reports,
                 session=replay, sleeper=lambda _: None)
    assert replay.calls == []
    assert sum(row["consumed"] for row in storage.get_lease_accounting("lease-case")) == 2


def test_cancel_and_expire_return_unspent_grants(staged):
    config, storage, reports = staged
    rows = _rows(storage)
    record_action(
        config, storage, "lease-case", action_id="cancel-a", kind="cancel",
        source_hash=rows["a.html"]["url_hash"], as_of=START,
        reason="content withdrawn", reports_dir=reports,
    )
    record_action(
        config, storage, "lease-case", action_id="expire-site", kind="expire",
        source_hash="lease-demo", as_of=EXPIRED,
        reason="daily lease boundary", reports_dir=reports,
    )
    rows = _rows(storage)
    assert rows["a.html"]["block_reason"] == "lease_cancelled"
    assert rows["b.html"]["block_reason"] == "lease_expired"
    assert not any(row["allowed"] for row in rows.values())
    account = {row["job_id"]: row for row in storage.get_lease_accounting("lease-case")}
    assert account["priority"]["cancelled"] == 1
    assert account["priority"]["expired"] == 1
    assert account["priority"]["available"] == 0


def test_rejects_robots_target_and_early_expiry_without_writing(staged):
    config, storage, reports = staged
    rows = _rows(storage)
    with pytest.raises(ValueError, match="different eligible job"):
        record_action(
            config, storage, "lease-case", action_id="bad-target", kind="transfer",
            source_hash=rows["a.html"]["url_hash"],
            target_hash=rows["secret.html"]["url_hash"], as_of=START,
            reason="invalid", reports_dir=reports,
        )
    with pytest.raises(ValueError, match="not expired"):
        record_action(
            config, storage, "lease-case", action_id="too-early", kind="expire",
            source_hash="lease-demo", as_of=START,
            reason="invalid", reports_dir=reports,
        )
    assert storage.get_lease_actions("lease-case") == []
