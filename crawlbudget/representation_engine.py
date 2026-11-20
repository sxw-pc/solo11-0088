"""Fact loading, body-file integrity, and deterministic candidate validation."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from .representation import (
    BODY_FILE_MISSING,
    BODY_FILE_TAMPERED,
    BODY_FILE_UNREADABLE,
    BodyLocation,
    RepresentationFact,
    canonical_headers,
)
from .storage import Storage

_EXECUTOR_USER_AGENT = ("user-agent", "crawlbudget/1.0 (+local-only)")
_ORIGIN_RANK = {"capture": 0, "executor_success": 1}


def verify_body_file(path: str | Path, expected_sha256: str) -> str | None:
    """Return a stable failure reason, or None when bytes match the digest."""
    target = Path(path)
    if not target.is_file():
        return BODY_FILE_MISSING
    digest = hashlib.sha256()
    try:
        with target.open("rb") as handle:
            for chunk in iter(lambda: handle.read(65536), b""):
                digest.update(chunk)
    except OSError:
        return BODY_FILE_UNREADABLE
    return None if digest.hexdigest() == expected_sha256 else BODY_FILE_TAMPERED


def get_capture_receipts(
    storage: Storage, run_id: str, *, site_id: str | None = None,
    url: str | None = None,
) -> list[dict[str, Any]]:
    """Return persisted receipts in deterministic capture order."""
    clauses = ["run_id=?"]
    params: list[Any] = [run_id]
    if site_id:
        clauses.append("site_id=?")
        params.append(site_id)
    if url:
        clauses.append("url=?")
        params.append(url)
    rows = storage._all(
        "SELECT * FROM capture_receipts WHERE " + " AND ".join(clauses)
        + " ORDER BY receipt_id", tuple(params),
    )
    for row in rows:
        row["request_headers"] = [
            tuple(pair) for pair in json.loads(row.pop("request_headers_json"))
        ]
    return rows


def facts_from_receipts(receipts: list[dict[str, Any]]) -> list[RepresentationFact]:
    return [
        RepresentationFact(
            origin="capture", origin_id=f"capture:{row['receipt_id']}",
            site_id=row["site_id"], url=row["url"], recorded_at=row["recorded_at"],
            status=int(row["status"]),
            request_headers=canonical_headers(row["request_headers"]),
            etag=row["etag"], vary=row["vary"],
            body_sha256=row["body_sha256"] or "", body_path=row["body_path"],
        )
        for row in receipts
    ]


def facts_from_events(events: list[dict[str, Any]]) -> list[RepresentationFact]:
    facts: list[RepresentationFact] = []
    for row in events:
        if not (200 <= int(row["status_code"] or 0) <= 299):
            continue
        if row["outcome"] != "success" or not row["body_sha256"]:
            continue
        facts.append(RepresentationFact(
            origin="executor_success", origin_id=f"event:{row['event_id']}",
            site_id=row["site_id"], url=row["url"], recorded_at=row["recorded_at"],
            status=int(row["status_code"]),
            request_headers=(_EXECUTOR_USER_AGENT,),
            etag="", vary="", body_sha256=row["body_sha256"], body_path=None,
            event_outcome=row["outcome"],
        ))
    return facts


def failed_get_keys(events: list[dict[str, Any]]) -> set[tuple[str, str]]:
    """Return (site_id, url) pairs that have a persisted non-2xx GET event."""
    return {
        (row["site_id"], row["url"]) for row in events
        if not (200 <= int(row["status_code"] or 0) <= 299)
    }


def load_facts(storage: Storage, run_id: str) -> list[RepresentationFact]:
    receipts = facts_from_receipts(get_capture_receipts(storage, run_id))
    events = facts_from_events(storage.get_request_events(run_id))
    return sorted(receipts + events, key=lambda f: (
        f.recorded_at, _ORIGIN_RANK[f.origin], f.origin_id))


def resolve_body_locations(
    facts: list[RepresentationFact], body_dirs: list[str | Path]
) -> dict[str, BodyLocation]:
    """Map every recorded digest to a verified on-disk body file."""
    locations: dict[str, BodyLocation] = {}
    dirs = [Path(directory) for directory in body_dirs]
    for fact in facts:
        if not fact.body_sha256 or fact.body_sha256 in locations:
            continue
        candidates = [Path(fact.body_path)] if fact.body_path else []
        candidates += [directory / f"{fact.body_sha256}.body" for directory in dirs]
        for candidate in candidates:
            if verify_body_file(candidate, fact.body_sha256) is None:
                locations[fact.body_sha256] = BodyLocation(
                    fact.body_sha256, str(candidate))
                break
    return locations


def verified_candidate(
    fact: RepresentationFact, locations: dict[str, BodyLocation]
) -> dict[str, Any] | None:
    """Validate one 200 fact: recorded digest must match the file on disk."""
    if not fact.body_sha256:
        return None
    location = locations.get(fact.body_sha256)
    if location is None:
        reason = _locate_failure(fact)
        return _candidate_row(fact, False, reason, None)
    return _candidate_row(fact, True, None, location.path)


def _locate_failure(fact: RepresentationFact) -> str:
    if fact.body_path:
        return verify_body_file(fact.body_path, fact.body_sha256) or BODY_FILE_TAMPERED
    return BODY_FILE_MISSING


def _candidate_row(
    fact: RepresentationFact, verified: bool, reason: str | None,
    body_path: str | None,
) -> dict[str, Any]:
    return {
        "origin": fact.origin, "origin_id": fact.origin_id,
        "recorded_at": fact.recorded_at, "status": fact.status,
        "etag": fact.etag, "vary": fact.vary,
        "body_sha256": fact.body_sha256, "body_path": body_path,
        "verified": verified, "not_publishable_reason": reason,
    }


def fact_source(fact: RepresentationFact,
                locations: dict[str, BodyLocation]) -> dict[str, Any]:
    location = locations.get(fact.body_sha256) if fact.body_sha256 else None
    return {
        "origin": fact.origin, "origin_id": fact.origin_id,
        "recorded_at": fact.recorded_at, "status": fact.status,
        "etag": fact.etag, "vary": fact.vary,
        "request_headers": list(fact.request_headers),
        "body_sha256": fact.body_sha256 or None,
        "body_path": location.path if location else fact.body_path,
    }
