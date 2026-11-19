"""End-to-end receipt admission against the real loopback Flask mock site.

These tests exercise the actual socket path: the mock site records a signed
receipt, then drops the connection after site acceptance. A crashing session
wrapper models the process dying before the request_event is written. The
resume then reaches the site's ``__receipt__`` / ``__recover__`` endpoints over
real HTTP through ``UrlReceiptClient``, rather than an in-process fake.
"""

import json
import socket
from pathlib import Path

import pytest
import requests
import yaml

from crawlbudget.config import load_config
from crawlbudget.demo_site import start_mock_servers, stop_mock_servers
from crawlbudget.lease_lifecycle import record_action
from crawlbudget.pipeline import run_pipeline
from crawlbudget.receipt import DELIVERY_NOT_DELIVERED, DELIVERY_RECOVERED
from crawlbudget.storage import Storage

ROOT = Path(__file__).resolve().parent.parent
START = "2026-01-15T00:00:00Z"
RUN_ID = "http-receipt"


def _free_port() -> int:
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    return port


def _rewrite_config(tmp_path: Path, port: int):
    raw = yaml.safe_load((ROOT / "fixtures/lease_lifecycle.yaml").read_text())
    raw["sites"][0]["base_url"] = f"http://127.0.0.1:{port}/lease-demo/"
    for site in raw["sites"]:
        site["robots_file"] = str(ROOT / "fixtures" / site["robots_file"])
    seed_dir = tmp_path / "seeds"
    seed_dir.mkdir(parents=True, exist_ok=True)
    for job in raw["jobs"]:
        text = (ROOT / "fixtures" / job["seed_file"]).read_text()
        text = text.replace("127.0.0.1:5021", f"127.0.0.1:{port}")
        seed_file = seed_dir / Path(job["seed_file"]).name
        seed_file.write_text(text)
        job["seed_file"] = str(seed_file)
    config_path = tmp_path / "lease.yaml"
    config_path.write_text(yaml.safe_dump(raw, sort_keys=False))
    return load_config(config_path, project_root=ROOT)


class _CrashingSession:
    """Escalate the c.html transport fault past the executor, like a crash."""

    def __init__(self, suffix: str) -> None:
        self.inner = requests.Session()
        self.inner.trust_env = False
        self.suffix = suffix
        self.trust_env = False

    def get(self, url: str, **kwargs):
        try:
            return self.inner.get(url, **kwargs)
        except requests.RequestException as exc:
            if url.endswith(self.suffix):
                raise RuntimeError("process crash before event write") from exc
            raise


def _stage_and_interrupt(config, storage, reports):
    run_pipeline(config, storage, RUN_ID, virtual_start=START,
                 dry_run=True, reports_dir=reports)
    rows = {row["url"].rsplit("/", 1)[-1]: row
            for row in storage.get_planned(RUN_ID)}
    record_action(
        config, storage, RUN_ID, action_id="move-b-c", kind="transfer",
        source_hash=rows["b.html"]["url_hash"], target_hash=rows["c.html"]["url_hash"],
        as_of=START, reason="priority withdrew b.html", reports_dir=reports,
    )
    crashing = _CrashingSession("c.html")
    with pytest.raises(RuntimeError, match="process crash"):
        run_pipeline(config, storage, RUN_ID, reports_dir=reports, session=crashing)
    return rows


@pytest.mark.parametrize(
    "contract,outcome,delivery",
    [
        ("audit", "receipt_audit_settled", DELIVERY_NOT_DELIVERED),
        ("redeliver", "receipt_recovered", DELIVERY_RECOVERED),
    ],
)
def test_real_http_receipt_admission_settles_without_second_get(
    tmp_path, contract, outcome, delivery
):
    port = _free_port()
    config = _rewrite_config(tmp_path, port)
    storage = Storage(":memory:")
    reports = str(tmp_path / "reports")
    servers = start_mock_servers(
        config, ROOT / "fixtures/pages", ROOT / "fixtures/pages/manifest.yaml",
        policies={"lease-demo": {"drop_response_for": "c.html"}},
    )
    try:
        rows = _stage_and_interrupt(config, storage, reports)
        # The interrupted dispatch left an open intent and no c.html event.
        assert storage.get_open_dispatch_intents(RUN_ID)
        assert storage.get_request_events(
            RUN_ID, url_hash=rows["c.html"]["url_hash"]) == []

        session = requests.Session()
        session.trust_env = False
        result = run_pipeline(config, storage, RUN_ID, reports_dir=reports,
                              session=session, receipt_contract=contract)

        events = storage.get_request_events(
            RUN_ID, url_hash=rows["c.html"]["url_hash"])
        assert len(events) == 1
        event = events[0]
        assert event["outcome"] == outcome
        assert json.loads(event["evidence_json"])["delivery"] == delivery
        # The waiting lease, not priority, owns the post-transfer spend.
        account = {row["job_id"]: row
                   for row in storage.get_lease_accounting(RUN_ID)}
        assert account["waiting"]["consumed"] == 1
        assert result.status == "completed"
    finally:
        stop_mock_servers(servers)
        storage.close()


def test_real_http_receipt_endpoint_returns_signed_evidence_and_404_absence(
    tmp_path,
):
    from crawlbudget.receipt import RECEIPT_QUERY_MARK, sign_receipt

    port = _free_port()
    config = _rewrite_config(tmp_path, port)
    servers = start_mock_servers(
        config, ROOT / "fixtures/pages", ROOT / "fixtures/pages/manifest.yaml",
        policies={"lease-demo": {}},
    )
    try:
        session = requests.Session()
        session.trust_env = False
        base = config.sites["lease-demo"].base_url
        dispatch_id = "b" * 64
        session.get(base + "c.html", headers={
            "X-CB-Dispatch-ID": dispatch_id, "X-CB-Run-Id": RUN_ID,
            "X-CB-Job-Id": "waiting", "X-CB-Url-Hash": "h",
            "X-CB-Attempt": "1", "X-CB-Lease-Id": "lease-x",
        })
        found = session.get(
            base + RECEIPT_QUERY_MARK + "/",
            params={"dispatch_id": dispatch_id, "url": base + "c.html"},
        )
        assert found.status_code == 200
        payload = found.json()
        assert payload["dispatch_id"] == dispatch_id
        assert payload["signature"] == sign_receipt("lease-demo", dispatch_id)

        absent = session.get(
            base + RECEIPT_QUERY_MARK + "/",
            params={"dispatch_id": "0" * 64, "url": base + "c.html"},
        )
        assert absent.status_code == 404
        assert absent.json()["present"] is False
    finally:
        stop_mock_servers(servers)
