"""Remote receipt verification and lease settlement on interrupted dispatch.

The minimal scenario is the lease lifecycle fixture: the priority job's
b.html grant is transferred to the waiting job's c.html before dispatch. If
the c.html GET reaches the site but its request_event is never written, a
resume must verify the remote receipt identity before deciding whether to
re-send. Receipt, page delivery, and local quota consumption are asserted as
three separate facts.
"""

from pathlib import Path

import json
import pytest

from crawlbudget.config import load_config
from crawlbudget.lease_lifecycle import record_action
from crawlbudget.pipeline import run_pipeline
from crawlbudget.receipt import (
    DELIVERY_NOT_DELIVERED,
    DELIVERY_RECOVERED,
    INTENT_DISPATCHED,
    INTENT_QUARANTINED,
    INTENT_SETTLED,
    RECEIPT_AUDIT,
    RECEIPT_REDELIVER,
    make_dispatch_id,
)
from crawlbudget.storage import Storage
from tests.fakes import FakeResponse, ReceiptAwareSession

ROOT = Path(__file__).resolve().parent.parent
START = "2026-01-15T00:00:00Z"
RUN_ID = "lease-receipt"


class _ProcessInterrupt(RuntimeError):
    """Simulates the process dying after the site GET but before the write."""


class _InterruptingReceiptSession:
    """Wrap the receipt session and escalate the c.html loss past the executor.

    The executor treats transport errors as retryable/terminal events, but a
    real crash happens *outside* that handling: the GET was answered (or the
    site otherwise accepted it), yet no request_event ever reaches the ledger.
    """

    def __init__(self, inner: ReceiptAwareSession, interrupt_suffix: str,
                 *, force: bool = False) -> None:
        self.inner = inner
        self.interrupt_suffix = interrupt_suffix
        self.force = force
        self.trust_env = False

    def get(self, url: str, **kwargs):
        suffix = url.rstrip("/").rsplit("/", 1)[-1]
        if suffix == self.interrupt_suffix and self.force:
            # Still let the inner session record the (receipt-less) GET.
            self.inner.get(url, **kwargs)
            raise _ProcessInterrupt("process died before the event was written")
        try:
            return self.inner.get(url, **kwargs)
        except RuntimeError as exc:
            if suffix == self.interrupt_suffix:
                raise _ProcessInterrupt(str(exc)) from exc
            raise


def _make_session(faults: dict[str, str] | None = None) -> ReceiptAwareSession:
    return ReceiptAwareSession(
        pages={
            "http://127.0.0.1:5021/lease-demo/a.html": FakeResponse(200, body=b"a-body"),
            "http://127.0.0.1:5021/lease-demo/c.html": FakeResponse(200, body=b"c-body"),
        },
        faults=faults,
        site_id="lease-demo",
    )


@pytest.fixture
def transferred(tmp_path):
    """Stage the run and move b.html's grant onto c.html before dispatch."""
    config = load_config(ROOT / "fixtures/lease_lifecycle.yaml", project_root=ROOT)
    storage = Storage(":memory:")
    reports = str(tmp_path / "reports")
    run_pipeline(config, storage, RUN_ID, virtual_start=START,
                 dry_run=True, reports_dir=reports)
    rows = {row["url"].rsplit("/", 1)[-1]: row
            for row in storage.get_planned(RUN_ID)}
    record_action(
        config, storage, RUN_ID, action_id="move-b-c", kind="transfer",
        source_hash=rows["b.html"]["url_hash"],
        target_hash=rows["c.html"]["url_hash"], as_of=START,
        reason="priority job withdrew its second URL", reports_dir=reports,
    )
    rows = {row["url"].rsplit("/", 1)[-1]: row
            for row in storage.get_planned(RUN_ID)}
    yield config, storage, reports, rows
    storage.close()


def _drive_interrupt(config, storage, reports, session, *, receipt_contract=RECEIPT_AUDIT,
                     force: bool = False):
    wrapped = _InterruptingReceiptSession(session, "c.html", force=force)
    with pytest.raises(_ProcessInterrupt):
        run_pipeline(config, storage, RUN_ID, reports_dir=reports,
                     session=wrapped, sleeper=lambda _: None,
                     receipt_contract=receipt_contract,
                     receipt_sources={"lease-demo": session})


def _resume(config, storage, reports, session, *, contract=RECEIPT_AUDIT):
    return run_pipeline(config, storage, RUN_ID, reports_dir=reports,
                        session=session, sleeper=lambda _: None,
                        receipt_contract=contract,
                        receipt_sources={"lease-demo": session})


def _c_events(storage, rows):
    return storage.get_request_events(RUN_ID, url_hash=rows["c.html"]["url_hash"])


def _evidence(event) -> dict:
    return json.loads(event["evidence_json"])


def _c_identity(rows, storage) -> tuple[str, dict]:
    c_row = rows["c.html"]
    dispatch_id = make_dispatch_id(
        RUN_ID, c_row["site_id"], c_row["job_id"], c_row["url_hash"], 1,
        c_row["lease_id"],
    )
    intent = storage.get_dispatch_intent(dispatch_id)
    return dispatch_id, intent


def test_receipt_audit_contract_bills_waiting_lease_without_delivery(transferred):
    config, storage, reports, rows = transferred
    session = _make_session(faults={"c.html": "drop"})
    _drive_interrupt(config, storage, reports, session)

    # The interrupted state: site accepted c.html once, but no local event and
    # no completed planned row exist for it.
    dispatch_id, intent = _c_identity(rows, storage)
    assert intent is not None and intent["state"] == INTENT_DISPATCHED
    c_events = storage.get_request_events(RUN_ID, url_hash=rows["c.html"]["url_hash"])
    assert c_events == []
    assert session.calls.count(rows["c.html"]["url"]) == 1

    # Resume under the audit contract: no second GET is sent.
    result = _resume(config, storage, reports, session, contract=RECEIPT_AUDIT)
    assert session.calls.count(rows["c.html"]["url"]) == 1

    events = storage.get_request_events(RUN_ID, url_hash=rows["c.html"]["url_hash"])
    assert len(events) == 1
    event = events[0]
    assert event["outcome"] == "receipt_audit_settled"
    # A receipt must never be misreported as a locally delivered response.
    assert event["status_code"] is None
    assert event["body_sha256"] == "" and event["response_bytes"] == 0
    assert _evidence(event)["delivery"] == DELIVERY_NOT_DELIVERED
    assert _evidence(event)["remote_status_code"] == 200

    receipt = storage.get_dispatch_receipts(RUN_ID)[0]
    assert receipt["dispatch_id"] == dispatch_id
    assert receipt["receipt_state"] == "audit_settled"
    assert receipt["delivery_conclusion"] == DELIVERY_NOT_DELIVERED

    # The spend belongs to the transferred waiting lease, not priority.
    account = {row["job_id"]: row for row in storage.get_lease_accounting(RUN_ID)}
    assert account["waiting"]["consumed"] == 1
    assert account["waiting"]["receipt_pending"] == 1
    assert account["priority"]["consumed"] == 1
    assert account["priority"]["receipt_pending"] == 0
    leases = {row["job_id"]: row for row in storage.get_leases(RUN_ID)}
    assert leases["waiting"]["used_requests"] == 1
    assert leases["priority"]["used_requests"] == 1

    planned = {row["url"].rsplit("/", 1)[-1]: row
               for row in storage.get_planned(RUN_ID)}
    assert planned["c.html"]["completed"] == 1
    assert planned["c.html"]["final_outcome"] == "receipt_audit_settled"

    attestation = next(
        item for item in result.summary["receipt_attestations"]
        if item["url_hash"] == rows["c.html"]["url_hash"]
    )
    assert attestation["delivery_conclusion"] == DELIVERY_NOT_DELIVERED
    assert attestation["receipt_state"] == "audit_settled"
    assert result.summary["totals"]["remote_receipts_verified"] == 1
    assert result.summary["totals"]["receipt_recovered"] == 0

    # A further re-run stays idempotent: still exactly one c.html GET.
    _resume(config, storage, reports, session, contract=RECEIPT_AUDIT)
    assert session.calls.count(rows["c.html"]["url"]) == 1


def test_redelivery_contract_recovers_body_without_new_collection_cost(transferred):
    config, storage, reports, rows = transferred
    session = _make_session(faults={"c.html": "drop"})
    _drive_interrupt(config, storage, reports, session)
    dispatch_id, _ = _c_identity(rows, storage)

    result = _resume(config, storage, reports, session, contract=RECEIPT_REDELIVER)

    # No second collection GET; the recovery endpoint was used instead.
    assert session.calls.count(rows["c.html"]["url"]) == 1
    assert len(session.recovery_calls) == 1
    assert dispatch_id in session.recovery_calls[0]

    events = storage.get_request_events(RUN_ID, url_hash=rows["c.html"]["url_hash"])
    assert len(events) == 1
    event = events[0]
    assert event["outcome"] == "receipt_recovered"
    assert _evidence(event)["delivery"] == DELIVERY_RECOVERED
    assert _evidence(event)["new_collection_cost"] == 0
    # The recovered body is genuinely retained and hash-pinned.
    assert event["body_sha256"] and event["response_bytes"] == len(b"c-body")

    receipt = storage.get_dispatch_receipts(RUN_ID)[0]
    assert receipt["receipt_state"] == "redelivered"
    assert receipt["delivery_conclusion"] == DELIVERY_RECOVERED
    assert receipt["recovered_body_sha256"] == event["body_sha256"]

    account = {row["job_id"]: row for row in storage.get_lease_accounting(RUN_ID)}
    assert account["waiting"]["consumed"] == 1
    assert account["waiting"]["receipt_recovered"] == 1
    # The two contracts must produce genuinely different conclusions.
    assert account["waiting"]["receipt_pending"] == 0

    planned = {row["url"].rsplit("/", 1)[-1]: row
               for row in storage.get_planned(RUN_ID)}
    assert planned["c.html"]["final_outcome"] == "receipt_recovered"
    attestation = next(
        item for item in result.summary["receipt_attestations"]
        if item["url_hash"] == rows["c.html"]["url_hash"]
    )
    assert attestation["delivery_conclusion"] == DELIVERY_RECOVERED
    assert result.summary["totals"]["receipt_recovered"] == 1


def test_definite_absence_allows_one_budgeted_redispatch(transferred):
    config, storage, reports, rows = transferred
    # The GET is answered but the site keeps no receipt, so the resume probe
    # is a definite "never arrived" and a fresh dispatch is legitimate.
    session = _make_session(faults={"c.html": "omit"})
    _drive_interrupt(config, storage, reports, session, force=True)
    dispatch_id, intent = _c_identity(rows, storage)
    assert intent["state"] == INTENT_DISPATCHED

    result = _resume(config, storage, reports, session, contract=RECEIPT_AUDIT)

    # The definite-absence verdict retired the stale identity, then the same
    # deterministic id was re-dispatched and is now settled by the real event.
    assert storage.get_dispatch_intent(dispatch_id)["state"] == INTENT_SETTLED
    assert session.calls.count(rows["c.html"]["url"]) == 2
    assert len(session.receipt_calls) == 1
    events = storage.get_request_events(RUN_ID, url_hash=rows["c.html"]["url_hash"])
    assert len(events) == 1 and events[0]["outcome"] == "success"
    assert storage.get_dispatch_receipts(RUN_ID) == []
    assert result.summary["totals"]["remote_receipts_verified"] == 0
    # Two dispatches against a two-request site budget stay inside the cap.
    account = {row["job_id"]: row for row in storage.get_lease_accounting(RUN_ID)}
    assert account["waiting"]["consumed"] == 1


def test_unverifiable_receipt_isolates_the_quota(transferred):
    config, storage, reports, rows = transferred
    session = _make_session(faults={"c.html": "corrupt"})
    _drive_interrupt(config, storage, reports, session, force=True)
    dispatch_id, _ = _c_identity(rows, storage)

    result = _resume(config, storage, reports, session, contract=RECEIPT_AUDIT)

    # No re-dispatch: an unverifiable claim cannot be treated as unspent.
    assert session.calls.count(rows["c.html"]["url"]) == 1
    events = storage.get_request_events(RUN_ID, url_hash=rows["c.html"]["url_hash"])
    assert len(events) == 1 and events[0]["outcome"] == "receipt_quarantined"
    receipt = storage.get_dispatch_receipts(RUN_ID)[0]
    assert receipt["receipt_state"] == "quarantined"
    assert "signature" in receipt["conflict_detail"]
    assert storage.get_dispatch_intent(dispatch_id)["state"] == INTENT_QUARANTINED

    blocked = [row for row in storage.get_blocked(RUN_ID)
               if row["reason"] == "receipt_quarantined"]
    assert len(blocked) == 1
    account = {row["job_id"]: row for row in storage.get_lease_accounting(RUN_ID)}
    # The waiting lease is billed for the isolated spend.
    assert account["waiting"]["consumed"] == 1
    assert account["waiting"]["quarantined"] == 1
    assert result.summary["totals"]["receipt_quarantined"] == 1
    # A repeat run must keep the quarantine rather than retry the GET.
    _resume(config, storage, reports, session)
    assert session.calls.count(rows["c.html"]["url"]) == 1


def test_lease_ownership_conflict_is_quarantined(transferred):
    config, storage, reports, rows = transferred
    session = _make_session(faults={"c.html": "drop"})
    _drive_interrupt(config, storage, reports, session)
    dispatch_id, intent = _c_identity(rows, storage)

    # Tamper with the stored intent's lease binding to simulate an identity
    # whose remote claim names a different owner than the local lease.
    storage.conn.execute(
        "UPDATE dispatch_intents SET lease_id=? WHERE dispatch_id=?",
        ("lease-other-owner", dispatch_id),
    )
    storage.conn.commit()

    _resume(config, storage, reports, session, contract=RECEIPT_AUDIT)
    receipt = storage.get_dispatch_receipts(RUN_ID)[0]
    assert receipt["receipt_state"] == "quarantined"
    events = storage.get_request_events(RUN_ID, url_hash=rows["c.html"]["url_hash"])
    assert events[0]["outcome"] == "receipt_quarantined"
    assert _evidence(events[0])["verdict"] == "lease_conflict"
    # The original waiting lease still owns the isolated spend.
    assert receipt["lease_id"] == intent["lease_id"]
    assert session.calls.count(rows["c.html"]["url"]) == 1


def test_recovery_hash_mismatch_falls_back_to_quarantine(transferred):
    config, storage, reports, rows = transferred
    session = _make_session(faults={"c.html": "drop"})
    _drive_interrupt(config, storage, reports, session)

    # Mutate the recovered body so it no longer matches the receipt hash.
    original_fetch = session.fetch_recovery

    def _tampered(site_id: str, dispatch_id: str) -> dict:
        payload = original_fetch(site_id, dispatch_id)
        payload["body"] = b"different-body"
        payload["body_sha256"] = "0" * 64
        payload["response_bytes"] = len(payload["body"])
        return payload

    session.fetch_recovery = _tampered
    _resume(config, storage, reports, session, contract=RECEIPT_REDELIVER)
    receipt = storage.get_dispatch_receipts(RUN_ID)[0]
    assert receipt["receipt_state"] == "quarantined"
    events = storage.get_request_events(RUN_ID, url_hash=rows["c.html"]["url_hash"])
    assert events[0]["outcome"] == "receipt_quarantined"
    assert session.calls.count(rows["c.html"]["url"]) == 1


def test_normal_resume_without_interruption_keeps_single_authorization(transferred):
    config, storage, reports, rows = transferred
    session = _make_session()
    result = run_pipeline(config, storage, RUN_ID, reports_dir=reports,
                          session=session, sleeper=lambda _: None)
    assert result.status == "completed"
    assert session.calls == [rows["a.html"]["url"], rows["c.html"]["url"]]

    # Every settled intent is closed and no receipt ledger rows are created on
    # the happy path because the local response was retained directly.
    intents = storage.get_open_dispatch_intents(RUN_ID)
    assert intents == []
    assert storage.get_dispatch_receipts(RUN_ID) == []
    c_events = storage.get_request_events(RUN_ID, url_hash=rows["c.html"]["url_hash"])
    assert [event["outcome"] for event in c_events] == ["success"]

    # Re-running the same run_id changes nothing: one authorization, stable.
    again = run_pipeline(config, storage, RUN_ID, reports_dir=reports,
                         session=session, sleeper=lambda _: None)
    assert again.status == "completed"
    assert session.calls == [rows["a.html"]["url"], rows["c.html"]["url"]]
    account = {row["job_id"]: row for row in storage.get_lease_accounting(RUN_ID)}
    assert (account["priority"]["consumed"], account["waiting"]["consumed"]) == (1, 1)


def test_robots_blocked_url_never_becomes_a_dispatch_target(transferred):
    config, storage, reports, rows = transferred
    # secret.html is robots-disallowed; even with a forged receipt source it
    # must never reach the admission gate or the HTTP session.
    secret = rows["secret.html"]
    assert secret["allowed"] == 0
    session = _make_session()
    run_pipeline(config, storage, RUN_ID, reports_dir=reports,
                 session=session, sleeper=lambda _: None)
    assert all(secret["url"] not in call for call in session.calls)
    assert storage.get_request_events(RUN_ID, url_hash=secret["url_hash"]) == []
