"""Executor-receipt records and ledger extraction for subscriber billing.

A receipt is only a *claim*.  ``billing_verify`` decides whether it is billable;
this module only reads and represents it.  Nothing here issues a GET or changes
lease usage.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from .billing_inputs import ParsedInputs, billing_error
from .models import ConfigBundle


@dataclass
class Receipt:
    subscriber_id: str
    site_id: str
    url: str
    outcome: str = ""
    status_code: int | None = None
    response_bytes: int | None = None
    body_sha256: str = ""
    run_id: str = ""
    url_hash: str = ""
    lease_id: str = ""
    lease_granted: int = 0
    lease_used: int = 0
    planned_allowed: bool = False
    block_reason: str | None = None
    robots_decision: str = ""

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "Receipt":
        robots = raw.get("robots")
        decision = raw.get("robots_decision", "")
        if not decision and isinstance(robots, Mapping):
            decision = str(robots.get("decision", ""))
        return cls(
            subscriber_id=str(raw.get("subscriber_id", "")),
            site_id=str(raw.get("site_id", "")),
            url=str(raw.get("url", "")),
            outcome=str(raw.get("outcome", "")),
            status_code=raw.get("status_code"),
            response_bytes=raw.get("response_bytes"),
            body_sha256=str(raw.get("body_sha256", "")),
            run_id=str(raw.get("run_id", "")),
            url_hash=str(raw.get("url_hash", "")),
            lease_id=str(raw.get("lease_id", "")),
            lease_granted=_as_int(raw.get("lease_granted", 0)),
            lease_used=_as_int(raw.get("lease_used", 0)),
            planned_allowed=bool(raw.get("planned_allowed", raw.get("allowed", False))),
            block_reason=raw.get("block_reason"),
            robots_decision=str(decision),
        )

    def identity(self) -> dict[str, Any]:
        return {
            "subscriber_id": self.subscriber_id,
            "run_id": self.run_id,
            "site_id": self.site_id,
            "url": self.url,
            "url_hash": self.url_hash,
            "lease_id": self.lease_id,
            "outcome": self.outcome,
            "status_code": self.status_code,
            "response_bytes": self.response_bytes,
            "body_sha256": self.body_sha256,
        }


def _as_int(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        return 0
    return value


def load_receipts(path: Path) -> list[Receipt]:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise billing_error("billing_receipts_unreadable",
                            "cannot read receipt attestation file",
                            path=str(path), error=str(exc)) from exc
    items = raw.get("receipts") if isinstance(raw, Mapping) else raw
    if not isinstance(items, list):
        raise billing_error("billing_receipts_invalid",
                            "receipt file must contain a list",
                            path=str(path))
    receipts = []
    for index, item in enumerate(items):
        if not isinstance(item, Mapping):
            raise billing_error("billing_receipts_invalid",
                                "receipt entries must be objects", index=index)
        receipts.append(Receipt.from_dict(item))
    return receipts


def receipts_from_storage(
    storage: Any, run_id: str, url_by_subscriber: Mapping[str, str]
) -> list[Receipt]:
    """Build receipts from an executor ledger without altering it."""
    planned = {(row["site_id"], row["url_hash"]): row
               for row in storage.get_planned(run_id)}
    events: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for event in storage.get_request_events(run_id):
        events.setdefault((event["site_id"], event["url_hash"]), []).append(event)
    leases = {lease["lease_id"]: lease for lease in storage.get_leases(run_id)}
    receipts: list[Receipt] = []
    for subscriber_id, url in url_by_subscriber.items():
        receipt = _receipt_from_ledger(subscriber_id, url, planned, events, leases)
        if receipt is not None:
            receipts.append(receipt)
    return receipts


def _receipt_from_ledger(
    subscriber_id: str,
    url: str,
    planned: Mapping[tuple[str, str], dict[str, Any]],
    events: Mapping[tuple[str, str], list[dict[str, Any]]],
    leases: Mapping[str, dict[str, Any]],
) -> Receipt | None:
    matches = [(key, row) for key, row in planned.items() if row["url"] == url]
    if not matches:
        return None
    (site_id, url_hash), row = matches[0]
    lease = leases.get(row["lease_id"], {})
    final = next((event for event in reversed(events.get((site_id, url_hash), []))
                  if event["outcome"] in {"success", "http_error", "redirect_blocked",
                                          "body_too_large", "transport_error_final",
                                          "preflight_blocked", "budget_exhausted"}), None)
    final = final or {}
    robots = json.loads(row.get("robots_evidence_json") or "{}")
    return Receipt(
        subscriber_id=subscriber_id,
        site_id=site_id,
        url=row["url"],
        outcome=str(final.get("outcome", "")),
        status_code=final.get("status_code"),
        response_bytes=final.get("response_bytes"),
        body_sha256=str(final.get("body_sha256") or ""),
        run_id=row["run_id"],
        url_hash=url_hash,
        lease_id=row["lease_id"],
        lease_granted=_as_int(lease.get("granted_requests", 0)),
        lease_used=_as_int(lease.get("used_requests", 0)),
        planned_allowed=bool(row.get("allowed")),
        block_reason=row.get("block_reason"),
        robots_decision=str(robots.get("decision", "")),
    )


def expected_urls(
    parsed: ParsedInputs, config: ConfigBundle, manifest: Mapping[str, Any]
) -> dict[str, str]:
    urls: dict[str, str] = {}
    for subscriber in parsed.subscribers:
        pages = manifest.get(subscriber.site_id)
        if not isinstance(pages, Mapping):
            raise billing_error("billing_unknown_site",
                                f"site {subscriber.site_id} is not configured",
                                subscriber_id=subscriber.subscriber_id)
        wanted = Path(subscriber.page_path).name
        matches = [subpath for subpath, entry in pages.items()
                   if isinstance(entry, Mapping) and entry.get("file") == wanted]
        if len(matches) != 1:
            raise billing_error(
                "billing_page_unmapped",
                f"page {subscriber.page_path} is not uniquely mapped for site",
                subscriber_id=subscriber.subscriber_id,
                site_id=subscriber.site_id, matches=matches,
            )
        site = config.sites.get(subscriber.site_id)
        if site is None:
            raise billing_error("billing_unknown_site",
                                f"site {subscriber.site_id} is not configured",
                                subscriber_id=subscriber.subscriber_id)
        urls[subscriber.subscriber_id] = site.base_url + matches[0].lstrip("/")
    return urls


__all__ = [
    "Receipt",
    "expected_urls",
    "load_receipts",
    "receipts_from_storage",
]
