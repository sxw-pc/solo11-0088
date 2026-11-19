"""Unit tests for stable dispatch identities and offline receipt signatures."""

from crawlbudget.receipt import (
    RECEIPT_DEFINITELY_ABSENT,
    RECEIPT_IDENTITY_CONFLICT,
    RECEIPT_LEASE_CONFLICT,
    RECEIPT_MATCHED,
    RECEIPT_UNVERIFIABLE,
    DispatchIntent,
    make_dispatch_id,
    receipt_endpoint,
    sign_receipt,
    verify_intent,
    verify_receipt_claim,
)


def _intent(**overrides):
    base = dict(
        dispatch_id="d" * 64, run_id="run-1", site_id="site-1", job_id="job-1",
        url_hash="u" * 64, url="http://127.0.0.1/site-1/c.html", attempt=1,
        lease_id="lease-1",
    )
    base.update(overrides)
    return DispatchIntent(**base)


def test_dispatch_id_is_stable_and_sensitive_to_each_binding():
    kwargs = dict(run_id="r", site_id="s", job_id="j", url_hash="h",
                  attempt=1, lease_id="l")
    first = make_dispatch_id(**kwargs)
    # Deterministic across calls.
    assert make_dispatch_id(**kwargs) == first
    assert len(first) == 64
    # Every binding field participates in the identity.
    for field_name, changed in (
        ("run_id", "r2"), ("site_id", "s2"), ("job_id", "j2"),
        ("url_hash", "h2"), ("attempt", 2), ("lease_id", "l2"),
    ):
        altered = dict(kwargs)
        altered[field_name] = changed
        assert make_dispatch_id(**altered) != first


def test_receipt_signature_verifies_offline_and_rejects_tampering():
    url_hash = "h" * 64
    dispatch_id = make_dispatch_id("r", "s", "j", url_hash, 1, "l")
    signature = sign_receipt("s", dispatch_id)
    intent = _intent(dispatch_id=dispatch_id, site_id="s",
                     run_id="r", job_id="j", url_hash=url_hash, lease_id="l")
    payload = {
        "dispatch_id": dispatch_id, "site_id": "s", "run_id": "r",
        "job_id": "j", "url_hash": url_hash, "url": intent.url, "attempt": 1,
        "lease_id": "l", "status_code": 200, "body_sha256": "b" * 64,
        "response_bytes": 10, "received_at": "2026-01-15T00:00:00Z",
        "signature": signature,
    }
    assert verify_receipt_claim(
        payload, site_id="s", expected_dispatch_id=dispatch_id, expected=intent
    ).decision == RECEIPT_MATCHED

    tampered = dict(payload, signature="0" * 64)
    assert verify_receipt_claim(
        tampered, site_id="s", expected_dispatch_id=dispatch_id, expected=intent
    ).decision == RECEIPT_UNVERIFIABLE

    # A signature from another site must not validate.
    foreign = dict(payload, signature=sign_receipt("other-site", dispatch_id))
    assert verify_receipt_claim(
        foreign, site_id="s", expected_dispatch_id=dispatch_id, expected=intent
    ).decision == RECEIPT_UNVERIFIABLE


def test_identity_and_lease_conflicts_are_distinct_verdicts():
    url_hash = "h" * 64
    dispatch_id = make_dispatch_id("r", "s", "j", url_hash, 1, "l")
    signature = sign_receipt("s", dispatch_id)
    good = {
        "dispatch_id": dispatch_id, "site_id": "s", "run_id": "r",
        "job_id": "j", "url_hash": url_hash, "url": "u", "attempt": 1,
        "lease_id": "l", "status_code": 200, "body_sha256": "b",
        "response_bytes": 1, "received_at": "t", "signature": signature,
    }
    intent = _intent(dispatch_id=dispatch_id, site_id="s", run_id="r",
                     job_id="j", url_hash=url_hash, lease_id="l")

    identity_bad = dict(good, run_id="different-run")
    assert verify_receipt_claim(
        identity_bad, site_id="s", expected_dispatch_id=dispatch_id, expected=intent
    ).decision == RECEIPT_IDENTITY_CONFLICT

    lease_bad = dict(good, lease_id="lease-someone-else")
    verdict = verify_receipt_claim(
        lease_bad, site_id="s", expected_dispatch_id=dispatch_id, expected=intent
    )
    assert verdict.decision == RECEIPT_LEASE_CONFLICT


def test_none_payload_is_a_definite_absence_not_an_unspent_grant():
    intent = _intent()
    verdict = verify_receipt_claim(
        None, site_id=intent.site_id,
        expected_dispatch_id=intent.dispatch_id, expected=intent,
    )
    assert verdict.decision == RECEIPT_DEFINITELY_ABSENT


class _StaticSource:
    def __init__(self, payload):
        self.payload = payload

    def lookup_receipt(self, site_id, dispatch_id):
        if isinstance(self.payload, Exception):
            raise self.payload
        return self.payload

    def fetch_recovery(self, site_id, dispatch_id):  # pragma: no cover
        raise NotImplementedError


def test_verify_intent_translates_broken_source_to_unverifiable():
    intent = _intent()

    class _Boom(Exception):
        pass

    verdict = verify_intent(_StaticSource(_Boom("endpoint down")), intent)
    assert verdict.decision == RECEIPT_UNVERIFIABLE
    assert "endpoint down" in verdict.detail


def test_receipt_endpoint_lives_under_site_base_path():
    endpoint = receipt_endpoint("http://127.0.0.1:5021/lease-demo/", "__receipt__")
    assert endpoint == "http://127.0.0.1:5021/lease-demo/__receipt__/"
    # Page and receipt paths never share a route segment.
    page = "http://127.0.0.1:5021/lease-demo/c.html"
    assert not endpoint.startswith(page)
