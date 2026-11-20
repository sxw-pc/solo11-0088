"""Offline representation attestation for conditional captures.

The engine turns three kinds of local evidence into deterministic
representation verdicts:

* conditional capture receipts (``capture_receipts``): 200 bodies or 304
  validator facts, never both in one receipt;
* persisted executor ``request_events`` (successful 2xx only);
* the body files actually retained on local disk.

It is not an HTTP cache. It never opens a socket, never guesses a body for a
304, and never presents an old digest as the current server digest. Two
downstream contracts are evaluated independently:

* ``semantic-readable`` may reuse an old 200 behind a weak-validator 304 while
  keeping the old SHA and an explicit "current bytes not proven" level;
* ``byte-exact`` publishes an exact snapshot only for a fresh 200 or a
  strong-validator chain that proves byte identity.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from .util import canonical_json, fingerprint

if TYPE_CHECKING:
    from .storage import Storage

SCHEMA_VERSION = "1.0"
CONTRACTS = ("semantic-readable", "byte-exact")
EVIDENCE_FRESH_200 = "fresh_200"
EVIDENCE_STRONG_304 = "strong_validator_304"
EVIDENCE_WEAK_304 = "weak_validator_304_semantic_only"
EVIDENCE_UNPUBLISHED = "unpublished"

# Stable, machine-readable non-publication reasons.
VARY_WILDCARD = "vary_wildcard"
VARY_HEADER_CONFLICT = "vary_header_conflict"
BODY_FILE_MISSING = "body_file_missing"
BODY_FILE_TAMPERED = "body_file_tampered"
BODY_FILE_UNREADABLE = "body_file_unreadable"
NO_CANDIDATE_200 = "no_verified_200_candidate"
ONLY_FAILED_GETS = "only_failed_get_events"
CONFLICTING_CANDIDATES = "conflicting_200_candidates"
VALIDATOR_MISMATCH = "validator_mismatch"
VALIDATOR_MISSING = "validator_missing"
WEAK_NOT_BYTE_EXACT = "weak_validator_not_byte_exact"

BODY_FAILURE_REASONS = frozenset({
    BODY_FILE_MISSING, BODY_FILE_TAMPERED, BODY_FILE_UNREADABLE,
})


@dataclass(frozen=True)
class BodyLocation:
    """One digest-addressed body file known to the attestation engine."""

    sha256: str
    path: str


@dataclass(frozen=True)
class RepresentationFact:
    """Normalized evidence item from a receipt or a persisted request event."""

    origin: str                      # "capture", "executor_success", "executor_failed"
    origin_id: str
    site_id: str
    url: str
    recorded_at: str
    status: int
    request_headers: tuple[tuple[str, str], ...]
    etag: str
    vary: str
    body_sha256: str
    body_path: str | None
    event_outcome: str = ""


@dataclass
class RepresentationVerdict:
    site_id: str
    url: str
    variant_key: str
    trigger_status: int
    trigger_recorded_at: str
    trigger_origin: str
    etag: str
    vary: str
    evidence_level: str
    body_sha256: str | None
    body_path: str | None
    current_bytes_identical: bool
    contracts: dict[str, dict[str, Any]]
    not_publishable_reason: str | None
    source_chain: list[dict[str, Any]] = field(default_factory=list)
    candidates: list[dict[str, Any]] = field(default_factory=list)


@dataclass
class RepresentationReport:
    schema_version: str
    inputs: dict[str, Any]
    body_locations: list[dict[str, str]]
    subjects: list[RepresentationVerdict]

    def fingerprint(self) -> str:
        return fingerprint(to_payload(self))


def canonical_headers(headers: Any) -> tuple[tuple[str, str], ...]:
    """Lower-case header names, trim values, and sort the pairs."""
    return tuple(sorted(
        (str(name).strip().lower(), str(value).strip())
        for name, value in headers
    ))


def parse_vary(vary: str) -> tuple[str, ...]:
    """Return the canonical Vary header-name list; a wildcard is preserved."""
    text = (vary or "").strip()
    if "*" in text:
        return ("*",)
    names = [part.strip().lower() for part in text.split(",") if part.strip()]
    return tuple(sorted(dict.fromkeys(names)))


def variant_signature(
    headers: tuple[tuple[str, str], ...], vary_names: tuple[str, ...]
) -> str:
    """Project request headers onto the Vary-selected request dimensions."""
    selected = {name: value for name, value in headers if name in set(vary_names)}
    pairs = [(name, selected.get(name, "")) for name in vary_names]
    return canonical_json(pairs)


def is_weak_etag(etag: str) -> bool:
    return etag.strip().upper().startswith("W/")


def etag_weak_match(left: str, right: str) -> bool:
    """RFC 7232 weak comparison: ignore the W/ prefix and compare raw tags."""
    return left.strip().lstrip("Ww/").strip() == right.strip().lstrip("Ww/").strip()


def etag_strong_match(left: str, right: str) -> bool:
    if not left.strip() or not right.strip() or is_weak_etag(left) or is_weak_etag(right):
        return False
    return left.strip() == right.strip()


def build_representation_report(
    storage: Storage,
    run_id: str,
    *,
    body_dirs: list[str | Path] | None = None,
) -> RepresentationReport:
    """Recompute every representation verdict from persisted local evidence."""
    from .representation_engine import (
        failed_get_keys,
        load_facts,
        resolve_body_locations,
    )
    from .representation_verdicts import build_verdicts

    facts = load_facts(storage, run_id)
    locations = resolve_body_locations(facts, body_dirs or [])
    failed = frozenset(failed_get_keys(storage.get_request_events(run_id)))
    verdicts = build_verdicts(facts, locations, failed)
    body_rows = [
        {"sha256": item.sha256, "path": item.path}
        for item in sorted(locations.values(), key=lambda item: item.sha256)
    ]
    inputs = {
        "run_id": run_id,
        "fact_count": len(facts),
        "body_location_count": len(body_rows),
        "body_dirs": [str(directory) for directory in (body_dirs or [])],
        "contracts": list(CONTRACTS),
    }
    return RepresentationReport(SCHEMA_VERSION, inputs, body_rows, verdicts)


def to_payload(report: RepresentationReport) -> dict[str, Any]:
    """Deterministic, JSON-serializable form of the whole report."""
    subjects = [
        {
            "site_id": verdict.site_id,
            "url": verdict.url,
            "variant_key": verdict.variant_key,
            "trigger_status": verdict.trigger_status,
            "trigger_recorded_at": verdict.trigger_recorded_at,
            "trigger_origin": verdict.trigger_origin,
            "etag": verdict.etag,
            "vary": verdict.vary,
            "evidence_level": verdict.evidence_level,
            "body_sha256": verdict.body_sha256,
            "body_path": verdict.body_path,
            "current_bytes_identical": verdict.current_bytes_identical,
            "not_publishable_reason": verdict.not_publishable_reason,
            "contracts": {
                name: dict(verdict.contracts[name]) for name in CONTRACTS
            },
            "source_chain": verdict.source_chain,
            "candidates": verdict.candidates,
        }
        for verdict in report.subjects
    ]
    return {
        "schema_version": report.schema_version,
        "inputs": report.inputs,
        "body_locations": report.body_locations,
        "subjects": subjects,
        "fingerprint": "",
    }


def dump_representation_json(report: RepresentationReport) -> str:
    payload = to_payload(report)
    payload["fingerprint"] = fingerprint(payload)
    return json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
