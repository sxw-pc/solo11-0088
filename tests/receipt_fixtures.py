"""Shared scenario builders for receipt-verification tests.

Scenario: the priority job's b.html grant is transferred to the waiting job's
c.html. The process stops after c.html's response returns but before its
request event is stored, so remote receipt, local delivery, and local budget
consumption diverge. Resume must adjudicate the persisted dispatch intent
against the site's verifiable receipts instead of blindly re-dispatching.
"""

from pathlib import Path

import pytest

from crawlbudget.config import load_config
from crawlbudget.lease_lifecycle import record_action
from crawlbudget.pipeline import run_pipeline
from crawlbudget.receipts import sign_receipt
from crawlbudget.storage import Storage
from crawlbudget.util import sha256_bytes
from tests.fakes import FakeResponse, ReplaySession

ROOT = Path(__file__).resolve().parent.parent
PAGES = ROOT / "fixtures" / "pages"
START = "2026-01-15T00:00:00Z"
RUN = "lease-case"


class CrashAfterResponse:
    """Storage proxy dying after the response, before the event write."""

    def __init__(self, inner, fragment):
        self._inner = inner
        self._fragment = fragment

    def __getattr__(self, name):
        return getattr(self._inner, name)

    def insert_request_event(self, event):
        if self._fragment in event.url:
            raise RuntimeError("simulated crash after response, before event write")
        return self._inner.insert_request_event(event)


class CrashBeforeGet:
    """Session dying before the matching GET ever leaves the client."""

    def __init__(self, fragment):
        import requests

        self._inner = requests.Session()
        self._inner.trust_env = False
        self._fragment = fragment
        self.trust_env = False

    def get(self, url, **kwargs):
        if self._fragment in url:
            raise RuntimeError("simulated crash before dispatch")
        return self._inner.get(url, **kwargs)


class CrashOnResolveIntent:
    """Storage proxy dying on the first intent settle."""

    def __init__(self, inner):
        self._inner = inner

    def __getattr__(self, name):
        return getattr(self._inner, name)

    def resolve_intent(self, *args, **kwargs):
        raise RuntimeError("simulated crash before intent settle")


def stage_transfer(tmp_path):
    """Staged run with the b.html grant transferred to the waiting c.html."""
    config = load_config(ROOT / "fixtures/lease_lifecycle.yaml", project_root=ROOT)
    storage = Storage(":memory:")
    reports = str(tmp_path / "reports")
    run_pipeline(config, storage, RUN, virtual_start=START, dry_run=True,
                 reports_dir=reports)
    rows = planned_rows(storage)
    record_action(
        config, storage, RUN, action_id="move-b-c", kind="transfer",
        source_hash=rows["b.html"]["url_hash"],
        target_hash=rows["c.html"]["url_hash"], as_of=START,
        reason="priority job withdrew its second URL", reports_dir=reports,
    )
    return config, storage, reports


def planned_rows(storage):
    return {row["url"].rsplit("/", 1)[-1]: row for row in storage.get_planned(RUN)}


def crash_after_c_response(config, storage, reports, session_routes):
    """Run until c.html's response returns but its event write crashes."""
    session = ReplaySession(session_routes)
    with pytest.raises(RuntimeError, match="simulated crash"):
        run_pipeline(config, CrashAfterResponse(storage, "c.html"), RUN,
                     reports_dir=reports, session=session, sleeper=lambda _: None)
    intents = storage.get_intents(RUN, state="dispatched")
    assert len(intents) == 1 and intents[0]["url"].endswith("c.html")
    return intents[0]


def crash_with_c_routes(config, storage, reports, body=b"c"):
    rows = planned_rows(storage)
    intent = crash_after_c_response(config, storage, reports, {
        rows["a.html"]["url"]: [FakeResponse(200, body=b"a")],
        rows["c.html"]["url"]: [FakeResponse(200, body=body)],
    })
    events = storage.get_request_events(RUN)
    assert [event["url"] for event in events] == [rows["a.html"]["url"]]
    return rows, intent


def receipt_for(intent, body, **overrides):
    """A validly signed receipt for the intent, as the mock site would vouch."""
    fields = {
        "intent_id": intent["intent_id"], "site_id": intent["site_id"],
        "run_id": intent["run_id"], "lease_id": intent["lease_id"],
        "path": "c.html", "status": 200, "body_sha256": sha256_bytes(body),
        "seq": 1,
    }
    fields.update(overrides)
    return sign_receipt(intent["site_id"], fields)


def c_events(storage):
    return [event for event in storage.get_request_events(RUN)
            if event["url"].endswith("c.html")]


def accounting_by_job(storage):
    return {row["job_id"]: row for row in storage.get_lease_accounting(RUN)}
