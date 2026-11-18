"""Attestation gates that turn executor receipts into billable weights.

A receipt is only a *claim*.  It becomes billable only after re-checking the
recognised success event, the recorded bytes and SHA-256, the local fixture
bytes, the site's scope/robots decision, and the granted lease.  Nothing here
issues a GET or changes lease usage.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Iterable, Mapping
from urllib.parse import urlsplit

from .billing_inputs import ParsedInputs
from .billing_receipts import Receipt, expected_urls
from .models import ConfigBundle, SiteConfig
from .robots import load_robots
from .scope import scope_url
from .util import fixture_bytes, sha256_bytes


def verify_receipts(
    parsed: ParsedInputs,
    config: ConfigBundle,
    receipts: Iterable[Receipt],
    *,
    project_root: Path,
    manifest: Mapping[str, Any],
    urls: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    urls = dict(urls or expected_urls(parsed, config, manifest))
    by_url: dict[str, list[Receipt]] = {}
    for receipt in receipts:
        site = config.sites.get(receipt.site_id)
        if site is None:
            continue
        scoped = scope_url(receipt.url, site)
        key = scoped.normalized_url if scoped.allowed else receipt.url.strip()
        by_url.setdefault(key, []).append(receipt)
    accepted: dict[str, dict[str, Any]] = {}
    rejected: list[dict[str, Any]] = []
    matched: set[int] = set()
    for subscriber in parsed.subscribers:
        expected = urls.get(subscriber.subscriber_id, "")
        site = config.sites.get(subscriber.site_id)
        if site is None:
            rejected.append(_reject(subscriber.subscriber_id, "billing_unknown_site",
                                    "subscriber site is not configured",
                                    {"site_id": subscriber.site_id}))
            continue
        expected_scoped = scope_url(expected, site)
        if not expected_scoped.allowed:
            rejected.append(_reject(subscriber.subscriber_id, "billing_page_unmapped",
                                    "subscriber page URL is not in scope",
                                    expected_scoped.evidence()))
            continue
        candidates = [item for item in by_url.get(expected_scoped.normalized_url, [])
                      if item.site_id == subscriber.site_id]
        if len(candidates) != 1:
            code = ("billing_receipts_missing" if not candidates
                    else "billing_duplicate_receipt")
            rejected.append(_reject(
                subscriber.subscriber_id, code,
                "exactly one executor receipt is required for this subscriber",
                {"expected_url": expected_scoped.normalized_url,
                 "candidate_count": len(candidates)},
            ))
            continue
        receipt = candidates[0]
        matched.add(id(receipt))
        issues = _receipt_issues(subscriber, receipt, site, expected_scoped, project_root)
        if issues:
            rejected.append(_reject(
                subscriber.subscriber_id, "billing_receipt_rejected",
                "executor receipt failed attestation",
                {"receipt": receipt.identity(), "issues": issues},
            ))
            continue
        accepted[subscriber.subscriber_id] = {
            "identity": receipt.identity(),
            "response_bytes": int(receipt.response_bytes or 0),
            "body_sha256": receipt.body_sha256,
            "verified": True,
        }
    for receipt in receipts:
        if id(receipt) not in matched:
            rejected.append(_reject(
                receipt.subscriber_id or None, "billing_unmatched_receipt",
                "receipt does not match any verified subscriber/page",
                {"receipt": receipt.identity(), "known_subscribers": list(urls)},
            ))
    return {
        "all_verified": not rejected and set(accepted) == set(parsed.ids),
        "accepted": accepted,
        "rejected": rejected,
    }


def _reject(subscriber_id: str | None, code: str, message: str,
            evidence: Mapping[str, Any]) -> dict[str, Any]:
    return {"subscriber_id": subscriber_id, "code": code,
            "message": message, "evidence": dict(evidence)}


def _receipt_issues(
    subscriber, receipt: Receipt, site: SiteConfig, expected_scoped,
    project_root: Path,
) -> list[dict[str, Any]]:
    issues: list[dict[str, Any]] = []

    def fail(code: str, detail: str, **evidence: Any) -> None:
        issues.append({"code": code, "detail": detail, **evidence})

    if receipt.site_id != subscriber.site_id:
        fail("site_mismatch", "receipt site differs from subscriber site",
             receipt_site=receipt.site_id, subscriber_site=subscriber.site_id)
    if receipt.url != expected_scoped.normalized_url:
        fail("url_mismatch", "receipt URL is not the mapped subscriber page",
             receipt_url=receipt.url, expected_url=expected_scoped.normalized_url)
    if receipt.url_hash and receipt.url_hash != expected_scoped.url_hash:
        fail("url_hash_mismatch", "receipt url_hash does not match scoped URL",
             receipt_url_hash=receipt.url_hash, expected_url_hash=expected_scoped.url_hash)
    if receipt.outcome != "success":
        fail("not_success", "only a successful GET may be billed", outcome=receipt.outcome)
    if receipt.status_code is None or not 200 <= int(receipt.status_code) <= 299:
        fail("not_success_status", "success receipt must have a 2xx status",
             status_code=receipt.status_code)
    if not receipt.planned_allowed or receipt.block_reason:
        fail("not_authorized", "planned request was not granted",
             planned_allowed=receipt.planned_allowed, block_reason=receipt.block_reason)
    if receipt.robots_decision != "allow":
        fail("robots_not_allowed", "robots decision was not allow",
             robots_decision=receipt.robots_decision)
    if not receipt.lease_id or receipt.lease_granted < 1 or receipt.lease_used < 1:
        fail("lease_not_granted", "receipt lacks a granted and consumed lease",
             lease_id=receipt.lease_id, lease_granted=receipt.lease_granted,
             lease_used=receipt.lease_used)
    match = load_robots(site).evaluate(urlsplit(expected_scoped.normalized_url).path)
    if not match.allowed:
        fail("robots_not_allowed", "current robots policy denies the path",
             robots=match.evidence())
    try:
        local = fixture_bytes(project_root / subscriber.page_path)
    except OSError as exc:
        fail("local_page_unreadable", "local fixture page cannot be read",
             error=str(exc), page_path=subscriber.page_path)
        return issues
    local_sha = sha256_bytes(local)
    if len(local) != subscriber.body_bytes:
        fail("fixture_bytes_mismatch", "local fixture size differs from intake",
             local_bytes=len(local), declared_bytes=subscriber.body_bytes)
    if local_sha != subscriber.body_sha256:
        fail("fixture_sha_mismatch", "local fixture SHA-256 differs from intake",
             local_sha256=local_sha, declared_sha256=subscriber.body_sha256)
    if not receipt.body_sha256:
        fail("receipt_sha_missing", "receipt has no body_sha256")
    elif receipt.body_sha256 != local_sha:
        fail("receipt_sha_mismatch", "receipt body_sha256 differs from local bytes",
             receipt_sha256=receipt.body_sha256, local_sha256=local_sha)
    if receipt.response_bytes != len(local):
        fail("receipt_bytes_mismatch", "receipt response_bytes differs from local bytes",
             response_bytes=receipt.response_bytes, local_bytes=len(local))
    return issues


__all__ = ["verify_receipts"]
