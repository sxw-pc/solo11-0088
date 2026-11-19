"""Stable dispatch identities and offline-verifiable remote receipts.

A dispatch intent is the durable claim the crawler makes *before* sending a
GET.  Its identity binds the run, site, job, URL hash, attempt number, and the
lease that owns the quota at dispatch time.  The loopback mock site answers a
matched GET with a receipt: a signed statement that the site accepted exactly
one collection for that identity.

Three facts are deliberately kept distinct:

* remote receipt ........... the site accepted the collection;
* page delivery ........... the local ledger holds the response body;
* local quota consumed .... the owning lease was billed.

A receipt alone proves only the first fact.  It can never turn a page the
local process never stored into a reported delivery, and HMAC verification is
fully offline: the signing key is derived deterministically from the site id,
so no key distribution endpoint is trusted at run time.
"""

from __future__ import annotations

import hmac
import json
from dataclasses import dataclass
from typing import Any, Protocol
from urllib.parse import urljoin

from .util import canonical_json, sha256_text

# Receipt verification decisions used by the executor's resume admission gate.
RECEIPT_MATCHED = "matched"
RECEIPT_DEFINITELY_ABSENT = "definitely_absent"
RECEIPT_UNVERIFIABLE = "unverifiable"
RECEIPT_IDENTITY_CONFLICT = "identity_conflict"
RECEIPT_LEASE_CONFLICT = "lease_conflict"

# Lifecycle states persisted on dispatch_intents.
INTENT_DISPATCHED = "dispatched"
INTENT_SUPERSEDED = "superseded"
INTENT_SETTLED = "settled"
INTENT_QUARANTINED = "quarantined"

# Lifecycle states persisted on dispatch_receipts.
RECEIPT_STATE_RECORDED = "recorded"
RECEIPT_STATE_AUDIT_SETTLED = "audit_settled"
RECEIPT_STATE_REDELIVERED = "redelivered"
RECEIPT_STATE_QUARANTINED = "quarantined"

# Delivery conclusions.  A remote receipt never claims the original delivery.
DELIVERY_NOT_DELIVERED = "not_delivered"
DELIVERY_RECOVERED = "recovered_after_receipt"

# Authorization contracts selected before resume admission.
RECEIPT_AUDIT = "audit"
RECEIPT_REDELIVER = "redeliver"

RECEIPT_QUERY_MARK = "__receipt__"
RECEIPT_RECOVER_MARK = "__recover__"
DISPATCH_HEADER = "X-CB-Dispatch-ID"


def make_dispatch_id(
    run_id: str, site_id: str, job_id: str, url_hash: str, attempt: int, lease_id: str
) -> str:
    """Return the stable identity of one pre-dispatch collection intent."""
    payload = canonical_json({
        "run_id": run_id,
        "site_id": site_id,
        "job_id": job_id,
        "url_hash": url_hash,
        "attempt": int(attempt),
        "lease_id": lease_id,
    })
    return sha256_text(payload)


def site_receipt_key(site_id: str) -> bytes:
    """Deterministic offline HMAC key shared by the mock site and verifier."""
    return sha256_text(f"crawlbudget-receipt-key\x1f{site_id}").encode("utf-8")


def sign_receipt(site_id: str, dispatch_id: str) -> str:
    return hmac.new(
        site_receipt_key(site_id), dispatch_id.encode("utf-8"), "sha256"
    ).hexdigest()


def receipt_endpoint(site_base_url: str, mark: str) -> str:
    """Build the site-side receipt/recovery endpoint under the site base path.

    The target page is carried separately by the client as a query parameter,
    so receipt handling never collides with a page's catch-all path segment.
    """
    base = site_base_url if site_base_url.endswith("/") else site_base_url + "/"
    return urljoin(base, mark + "/")


@dataclass(frozen=True)
class DispatchIntent:
    """The durable identity persisted immediately before a GET is sent."""

    dispatch_id: str
    run_id: str
    site_id: str
    job_id: str
    url_hash: str
    url: str
    attempt: int
    lease_id: str
    state: str = INTENT_DISPATCHED


@dataclass(frozen=True)
class Receipt:
    """A site-issued statement that one collection was accepted."""

    dispatch_id: str
    site_id: str
    run_id: str
    job_id: str
    url_hash: str
    url: str
    attempt: int
    lease_id: str
    status_code: int
    body_sha256: str
    response_bytes: int
    received_at: str
    signature: str

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "Receipt":
        return cls(
            dispatch_id=str(payload["dispatch_id"]),
            site_id=str(payload["site_id"]),
            run_id=str(payload["run_id"]),
            job_id=str(payload["job_id"]),
            url_hash=str(payload["url_hash"]),
            url=str(payload["url"]),
            attempt=int(payload["attempt"]),
            lease_id=str(payload["lease_id"]),
            status_code=int(payload["status_code"]),
            body_sha256=str(payload["body_sha256"]),
            response_bytes=int(payload["response_bytes"]),
            received_at=str(payload["received_at"]),
            signature=str(payload["signature"]),
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "dispatch_id": self.dispatch_id,
            "site_id": self.site_id,
            "run_id": self.run_id,
            "job_id": self.job_id,
            "url_hash": self.url_hash,
            "url": self.url,
            "attempt": self.attempt,
            "lease_id": self.lease_id,
            "status_code": self.status_code,
            "body_sha256": self.body_sha256,
            "response_bytes": self.response_bytes,
            "received_at": self.received_at,
            "signature": self.signature,
        }


@dataclass(frozen=True)
class ReceiptVerdict:
    """Outcome of verifying a receipt claim against the local dispatch intent."""

    decision: str
    receipt: Receipt | None = None
    detail: str = ""

    @property
    def matched(self) -> bool:
        return self.decision == RECEIPT_MATCHED


class ReceiptSource(Protocol):
    def lookup_receipt(self, site_id: str, dispatch_id: str) -> dict[str, Any] | None:
        """Return the signed receipt payload, or None for a definite absence."""

    def fetch_recovery(self, site_id: str, dispatch_id: str) -> dict[str, Any]:
        """Re-fetch the body stored under an identity without new collection cost."""


def verify_receipt_claim(
    payload: dict[str, Any] | None,
    *,
    site_id: str,
    expected_dispatch_id: str,
    expected: DispatchIntent,
) -> ReceiptVerdict:
    """Verify a raw receipt claim against the local pre-dispatch identity.

    ``payload is None`` means the site gave a definite, answerable "no
    collection arrived for this identity" conclusion.  Any transport that
    cannot answer at all is handled by the caller as an unverifiable claim.
    """
    if payload is None:
        return ReceiptVerdict(RECEIPT_DEFINITELY_ABSENT, detail="site reports no receipt")
    try:
        receipt = Receipt.from_dict(payload)
    except (KeyError, TypeError, ValueError) as exc:
        return ReceiptVerdict(RECEIPT_UNVERIFIABLE, detail=f"malformed receipt: {exc}")

    expected_signature = sign_receipt(site_id, receipt.dispatch_id)
    if not hmac.compare_digest(expected_signature, receipt.signature):
        return ReceiptVerdict(
            RECEIPT_UNVERIFIABLE, receipt=receipt, detail="receipt signature mismatch"
        )
    if receipt.dispatch_id != expected_dispatch_id:
        return ReceiptVerdict(
            RECEIPT_IDENTITY_CONFLICT, receipt=receipt,
            detail="receipt dispatch id does not match the local intent",
        )
    identity_fields = ("run_id", "site_id", "job_id", "url_hash", "attempt")
    for field_name in identity_fields:
        local = getattr(expected, field_name)
        remote = getattr(receipt, field_name)
        if str(local) != str(remote):
            return ReceiptVerdict(
                RECEIPT_IDENTITY_CONFLICT, receipt=receipt,
                detail=f"identity field conflict: {field_name}",
            )
    if receipt.lease_id != expected.lease_id:
        return ReceiptVerdict(
            RECEIPT_LEASE_CONFLICT, receipt=receipt,
            detail="receipt lease owner differs from the current lease binding",
        )
    return ReceiptVerdict(RECEIPT_MATCHED, receipt=receipt, detail="verified")


@dataclass
class UrlReceiptClient:
    """Concrete client: query and recovery endpoints under the site base path."""

    session: Any
    base_url: str
    page_url: str
    site_id: str
    timeout_ms: int = 2000

    def _get(self, endpoint: str) -> Any:
        return self.session.get(
            endpoint,
            timeout=self.timeout_ms / 1000.0,
            allow_redirects=False,
            headers={"User-Agent": "crawlbudget/1.0 (+local-only)"},
        )

    def lookup_receipt(self, site_id: str, dispatch_id: str) -> dict[str, Any] | None:
        from urllib.parse import urlencode

        query = urlencode({"dispatch_id": dispatch_id, "url": self.page_url})
        endpoint = receipt_endpoint(self.base_url, RECEIPT_QUERY_MARK) + "?" + query
        response = self._get(endpoint)
        status = int(response.status_code)
        if status == 404:
            response.close()
            return None
        if status != 200:
            response.close()
            raise ReceiptTransportError(f"receipt endpoint answered status {status}")
        try:
            payload = json.loads(response.content.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ReceiptTransportError(f"receipt endpoint returned invalid JSON: {exc}") from exc
        finally:
            response.close()
        if not isinstance(payload, dict):
            raise ReceiptTransportError("receipt endpoint returned a non-object payload")
        return payload

    def fetch_recovery(self, site_id: str, dispatch_id: str) -> dict[str, Any]:
        from urllib.parse import urlencode

        query = urlencode({"dispatch_id": dispatch_id, "url": self.page_url})
        endpoint = receipt_endpoint(self.base_url, RECEIPT_RECOVER_MARK) + "?" + query
        response = self._get(endpoint)
        status = int(response.status_code)
        if status != 200:
            response.close()
            raise ReceiptTransportError(f"recovery endpoint answered status {status}")
        chunks: list[bytes] = []
        for chunk in response.iter_content(chunk_size=4096):
            if chunk:
                chunks.append(chunk)
        content_type = str(response.headers.get("Content-Type", ""))
        response.close()
        body = b"".join(chunks)
        from .util import sha256_bytes

        return {
            "status_code": status,
            "body": body,
            "content_type": content_type,
            "body_sha256": sha256_bytes(body),
            "response_bytes": len(body),
        }


class ReceiptTransportError(Exception):
    """Raised when a receipt endpoint cannot give an answerable conclusion."""


def verify_intent(
    source: ReceiptSource,
    intent: DispatchIntent,
) -> ReceiptVerdict:
    """Query a receipt source and verify its claim for one stored intent."""
    try:
        payload = source.lookup_receipt(intent.site_id, intent.dispatch_id)
    except ReceiptTransportError as exc:
        return ReceiptVerdict(RECEIPT_UNVERIFIABLE, detail=str(exc))
    except Exception as exc:  # a broken remote must never look like "not spent"
        return ReceiptVerdict(
            RECEIPT_UNVERIFIABLE, detail=f"{type(exc).__name__}: {exc}"
        )
    return verify_receipt_claim(
        payload,
        site_id=intent.site_id,
        expected_dispatch_id=intent.dispatch_id,
        expected=intent,
    )
