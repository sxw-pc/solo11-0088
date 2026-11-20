"""Offline-verifiable collection receipts for crash-safe resume admission.

Every HTTP attempt is announced by a persisted dispatch intent that binds
(run, site, job, url hash, attempt, current lease owner) into one stable
identity. The mock site records a signed receipt when such an intent arrives,
so a resumed run can ask the site whether that exact identity was received
instead of guessing from an empty local event ledger. All cryptography is
local and deterministic: receipts are HMAC-SHA256 tokens over canonical JSON,
derived from the site id, so verification needs no external service.
"""

from __future__ import annotations

import hmac
from dataclasses import dataclass
from hashlib import sha256
from typing import Any

from .models import SiteConfig
from .util import canonical_json, sha256_text

INTENT_HEADER = "X-Crawlbudget-Intent"
RUN_HEADER = "X-Crawlbudget-Run"
LEASE_HEADER = "X-Crawlbudget-Lease"

RECEIPT_API_PREFIX = "/__crawlbudget__/receipts"

VERDICT_RECEIVED = "received"
VERDICT_NOT_RECEIVED = "not_received"
VERDICT_UNVERIFIABLE = "unverifiable"
VERDICT_CONFLICT = "conflict"

CONTRACT_AUDIT = "audit"
CONTRACT_RECOVER = "recover"
RECEIPT_CONTRACTS = frozenset({CONTRACT_AUDIT, CONTRACT_RECOVER})

INTENT_STATES = frozenset({"dispatched", "settled", "voided", "quarantined"})

RECEIPT_FIELDS = (
    "intent_id", "site_id", "run_id", "lease_id", "path", "status",
    "body_sha256", "seq",
)
ABSENCE_FIELDS = ("verdict", "site_id", "intent_id")
BINDING_FIELDS = ("intent_id", "run_id", "lease_id")


def make_intent_id(
    run_id: str, site_id: str, job_id: str, url_hash: str, attempt: int,
    lease_id: str,
) -> str:
    """Stable identity of one dispatch intent."""
    parts = [run_id, site_id, job_id, url_hash, str(attempt), lease_id]
    return f"intent-{sha256_text(chr(31).join(parts))[:24]}"


def receipt_signing_key(site_id: str) -> bytes:
    return sha256(f"crawlbudget-receipt\x1f{site_id}".encode("utf-8")).digest()


def _token(site_id: str, fields: dict[str, Any], names: tuple[str, ...]) -> str:
    payload = {name: fields.get(name) for name in names}
    message = canonical_json(payload).encode("utf-8")
    return hmac.new(receipt_signing_key(site_id), message, sha256).hexdigest()


def sign_receipt(site_id: str, receipt: dict[str, Any]) -> dict[str, Any]:
    """Return a receipt dict with a fresh verifiable token."""
    signed = {name: receipt[name] for name in RECEIPT_FIELDS}
    signed["token"] = _token(site_id, signed, RECEIPT_FIELDS)
    return signed


def verify_receipt(site_id: str, receipt: Any) -> bool:
    if not isinstance(receipt, dict):
        return False
    if any(name not in receipt for name in (*RECEIPT_FIELDS, "token")):
        return False
    expected = _token(site_id, receipt, RECEIPT_FIELDS)
    return hmac.compare_digest(expected, str(receipt["token"]))


def sign_absence(site_id: str, intent_id: str) -> dict[str, Any]:
    """Signed statement that the site never received this intent."""
    statement = {
        "verdict": VERDICT_NOT_RECEIVED, "site_id": site_id, "intent_id": intent_id,
    }
    statement["token"] = _token(site_id, statement, ABSENCE_FIELDS)
    return statement


def verify_absence(site_id: str, statement: Any) -> bool:
    if not isinstance(statement, dict):
        return False
    if statement.get("verdict") != VERDICT_NOT_RECEIVED:
        return False
    if any(name not in statement for name in (*ABSENCE_FIELDS, "token")):
        return False
    expected = _token(site_id, statement, ABSENCE_FIELDS)
    return hmac.compare_digest(expected, str(statement["token"]))


def binding_mismatches(intent: dict[str, Any], receipt: dict[str, Any]) -> dict[str, Any]:
    """Identity fields where a verified receipt contradicts the intent."""
    return {
        field: [receipt.get(field), intent.get(field)]
        for field in BINDING_FIELDS
        if receipt.get(field) != intent.get(field)
    }


def receipt_evidence(receipt: dict[str, Any]) -> dict[str, Any]:
    """Receipt metadata kept as evidence; never implies local delivery."""
    return {
        key: receipt.get(key)
        for key in ("intent_id", "seq", "status", "body_sha256", "token")
    }


@dataclass
class ReceiptVerdict:
    kind: str
    receipt: dict[str, Any] | None = None
    detail: str = ""


class SiteReceiptVerifier:
    """Asks a mock site whether one persisted dispatch intent was received."""

    def __init__(self, session: Any, sites: dict[str, SiteConfig]) -> None:
        self.session = session
        self.sites = sites

    def verify(self, intent: dict[str, Any]) -> ReceiptVerdict:
        site = self.sites[intent["site_id"]]
        try:
            response = self.session.get(
                self._endpoint(site, intent["intent_id"]),
                timeout=site.timeout_ms / 1000.0,
            )
        except Exception as exc:  # any transport doubt is unverifiable
            return ReceiptVerdict(VERDICT_UNVERIFIABLE,
                                  detail=f"transport:{type(exc).__name__}")
        try:
            payload = response.json()
        except ValueError:
            return ReceiptVerdict(VERDICT_UNVERIFIABLE, detail="non-JSON receipt reply")
        return classify_receipt_reply(intent, response.status_code, payload)

    def fetch_body(self, intent: dict[str, Any], receipt: dict[str, Any]) -> tuple[bytes, str] | None:
        """Retrieve the exact body the site served for this intent, if kept."""
        site = self.sites[intent["site_id"]]
        try:
            response = self.session.get(
                self._endpoint(site, intent["intent_id"], suffix="/body"),
                timeout=site.timeout_ms / 1000.0,
            )
        except Exception:
            return None
        if int(response.status_code) != 200:
            return None
        return response.content, str(response.headers.get("Content-Type", ""))

    def _endpoint(self, site: SiteConfig, intent_id: str, suffix: str = "") -> str:
        base = site.parsed_base
        host = base.hostname or ""
        if ":" in host and not host.startswith("["):
            host = f"[{host}]"
        port = base.port or 80
        return (f"{base.scheme}://{host}:{port}"
                f"{RECEIPT_API_PREFIX}/{intent_id}{suffix}")


def classify_receipt_reply(
    intent: dict[str, Any], status_code: int, payload: Any
) -> ReceiptVerdict:
    """Turn a receipt-endpoint reply into a verdict; pure and offline."""
    site_id = intent["site_id"]
    if status_code == 404:
        if verify_absence(site_id, payload) and payload.get("intent_id") == intent["intent_id"]:
            return ReceiptVerdict(VERDICT_NOT_RECEIVED)
        return ReceiptVerdict(VERDICT_UNVERIFIABLE,
                              detail="absence statement failed verification")
    if status_code != 200 or not isinstance(payload, dict):
        return ReceiptVerdict(VERDICT_UNVERIFIABLE,
                              detail=f"unexpected receipt status {status_code}")
    if not verify_receipt(site_id, payload):
        return ReceiptVerdict(VERDICT_UNVERIFIABLE, detail="receipt token mismatch")
    mismatches = binding_mismatches(intent, payload)
    if mismatches:
        return ReceiptVerdict(VERDICT_CONFLICT, receipt=payload,
                              detail=canonical_json(mismatches))
    return ReceiptVerdict(VERDICT_RECEIVED, receipt=payload)
