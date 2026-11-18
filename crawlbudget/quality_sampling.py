"""Committed URL sampling frames and exact hypergeometric evidence bounds.

A frame is bound to a run before any sampled GET.  The binding records every
frame URL, the seed, the drawn sample, the metadata contracts, and the code
version digest.  Later inspection must use that commitment; it may not redraw,
replace a failed URL, or swap contracts after seeing document bodies.
"""

from __future__ import annotations

import hashlib
import json
from fractions import Fraction
from pathlib import Path
from typing import Any, Mapping

from .models import SiteConfig
from .quality_stats import DEFAULT_ALPHA, ONE_SIDED_CONFIDENCE
from .scope import scope_url
from .storage import Storage
from .util import canonical_json, fingerprint

MAX_FRAME_URLS = 30
MAX_SAMPLE_SIZE = 12
QUALITY_SCHEMA_VERSION = "1.0"
QUALITY_VERSION = "metadata-population-certificate/1.1"


def load_frame(path: str | Path, site: SiteConfig) -> dict[str, Any]:
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(raw, dict) or set(raw) != {"site_id", "seed", "sample_size", "urls"}:
        raise ValueError("sample frame has invalid fields")
    if raw["site_id"] != site.site_id:
        raise ValueError("sample frame site or seed is invalid")
    _validate_frame(raw)
    if any(not scope_url(url, site).allowed for url in raw["urls"]):
        raise ValueError("sample frame contains an out-of-scope URL")
    return raw


def _validate_frame(frame: Mapping[str, Any]) -> None:
    if not isinstance(frame, Mapping):
        raise ValueError("sample frame must be an object")
    site_id = frame.get("site_id")
    if not isinstance(site_id, str) or not site_id:
        raise ValueError("sample frame site or seed is invalid")
    seed = frame.get("seed")
    if not isinstance(seed, str) or not seed:
        raise ValueError("sample frame site or seed is invalid")
    urls = frame.get("urls")
    if not isinstance(urls, list) or not urls or any(not isinstance(url, str) for url in urls):
        raise ValueError("sample frame URLs are invalid")
    if len(urls) > MAX_FRAME_URLS:
        raise ValueError(f"sample frame exceeds {MAX_FRAME_URLS} URLs")
    if len(set(urls)) != len(urls):
        raise ValueError("sample frame has duplicate URLs")
    n = frame.get("sample_size")
    if type(n) is not int or n <= 0 or n > len(urls):
        raise ValueError("sample size is invalid")
    if n > MAX_SAMPLE_SIZE:
        raise ValueError(f"sample size exceeds {MAX_SAMPLE_SIZE}")


def draw_frame(frame: Mapping[str, Any]) -> list[str]:
    """Draw without replacement by seeded hash rank; tie-break by URL."""
    seed = frame["seed"]
    return sorted(frame["urls"], key=lambda url: (
        hashlib.sha256(f"{seed}|{url}".encode("utf-8")).hexdigest(), url,
    ))[:frame["sample_size"]]


def canonical_frame(frame: Mapping[str, Any]) -> dict[str, Any]:
    """Order-insensitive frame identity: URL order is not a sample decision."""
    _validate_frame(frame)
    return {
        "site_id": frame.get("site_id"),
        "seed": frame["seed"],
        "sample_size": frame["sample_size"],
        "urls": sorted(frame["urls"]),
    }


def frame_digest(frame: Mapping[str, Any]) -> str:
    return fingerprint(canonical_frame(frame))


def selected_digest(selected: list[str]) -> str:
    return fingerprint(sorted(selected))


def parse_fraction(text: Any) -> Fraction:
    if isinstance(text, Mapping):
        text = text.get("text")
    if not isinstance(text, str) or text.count("/") != 1:
        raise ValueError("minimum_population_fraction must be a fraction such as 3/4")
    left, right = text.split("/", 1)
    try:
        numerator, denominator = int(left), int(right)
    except ValueError as exc:
        raise ValueError("minimum_population_fraction is not an integer fraction") from exc
    if numerator <= 0 or denominator <= 0 or numerator > denominator:
        raise ValueError("minimum_population_fraction must be in (0, 1]")
    return Fraction(numerator, denominator)


def normalize_contract(name: str, raw: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(name, str) or not name:
        raise ValueError("contract name is invalid")
    if not isinstance(raw, Mapping):
        raise ValueError(f"contract {name!r} must be an object")
    if raw.get("name") not in (None, name):
        raise ValueError(f"contract {name!r} has a conflicting name")
    fields = raw.get("required_fields")
    if (not isinstance(fields, list) or not fields
            or any(not isinstance(field, str) or not field.strip() for field in fields)):
        raise ValueError(f"contract {name!r} has invalid required_fields")
    if len(set(fields)) != len(fields):
        raise ValueError(f"contract {name!r} repeats a required field")
    minimum = parse_fraction(raw.get("minimum_population_fraction"))
    return {
        "name": name,
        "required_fields": sorted(field.strip().lower() for field in fields),
        "minimum_population_fraction": {
            "text": f"{minimum.numerator}/{minimum.denominator}",
            "numerator": minimum.numerator,
            "denominator": minimum.denominator,
        },
    }


def normalize_contracts(contracts: Mapping[str, Mapping[str, Any]]) -> dict[str, dict[str, Any]]:
    if not isinstance(contracts, Mapping) or not contracts:
        raise ValueError("at least one metadata contract is required")
    return {name: normalize_contract(name, contracts[name]) for name in sorted(contracts)}


def contracts_digest(contracts: Mapping[str, Mapping[str, Any]]) -> str:
    normalized = normalize_contracts(contracts)
    return fingerprint([normalized[name] for name in sorted(normalized)])


def _version_digest() -> str:
    return fingerprint({
        "schema_version": QUALITY_SCHEMA_VERSION,
        "quality_version": QUALITY_VERSION,
        "sampling": "sha256-seeded-rank-without-replacement",
        "interval": "hypergeometric-exact-tail-inversion",
        "confidence": f"{ONE_SIDED_CONFIDENCE.numerator}/{ONE_SIDED_CONFIDENCE.denominator}",
        "alpha": f"{DEFAULT_ALPHA.numerator}/{DEFAULT_ALPHA.denominator}",
    })


def build_commitment(
    run_id: str, frame: Mapping[str, Any], contracts: Mapping[str, Mapping[str, Any]]
) -> dict[str, Any]:
    if not isinstance(run_id, str) or not run_id:
        raise ValueError("run id is invalid")
    canonical = canonical_frame(frame)
    normalized = normalize_contracts(contracts)
    selected = draw_frame(canonical)
    payload = {
        "run_id": run_id, "site_id": canonical["site_id"],
        "schema_version": QUALITY_SCHEMA_VERSION, "quality_version": QUALITY_VERSION,
        "frame": canonical, "selected": selected, "contracts": normalized,
        "frame_digest": frame_digest(canonical),
        "selected_digest": selected_digest(selected),
        "contracts_digest": contracts_digest(normalized),
        "version_digest": _version_digest(),
    }
    payload["commitment_digest"] = fingerprint(payload)
    return payload


def commit_quality_sample(
    storage: Storage,
    run_id: str,
    frame: Mapping[str, Any],
    contracts: Mapping[str, Mapping[str, Any]],
    *,
    created_at: str = "",
) -> dict[str, Any]:
    """Persist the one allowed quality commitment for a run.

    Repeating an identical commitment is idempotent.  A changed seed, URL set,
    selected sample, contract, or version is rejected, never overwritten.
    """
    payload = build_commitment(run_id, frame, contracts)
    existing = storage.get_quality_commitment(run_id)
    if existing is not None:
        stored = parse_commitment(existing)
        if stored["commitment_digest"] != payload["commitment_digest"]:
            raise ValueError(
                "run is already bound to a different quality sample commitment"
            )
        return stored
    if storage.attempt_count(run_id) > 0:
        raise ValueError(
            "quality commitment must be recorded before any sampled GET"
        )
    storage.save_quality_commitment({
        "run_id": run_id,
        "commitment_digest": payload["commitment_digest"],
        "schema_version": payload["schema_version"],
        "quality_version": payload["quality_version"],
        "version_digest": payload["version_digest"],
        "frame_digest": payload["frame_digest"],
        "selected_digest": payload["selected_digest"],
        "contracts_digest": payload["contracts_digest"],
        "frame_json": canonical_json(payload["frame"]),
        "selected_json": canonical_json(payload["selected"]),
        "contracts_json": canonical_json(payload["contracts"]),
        "created_at": created_at,
    })
    stored = storage.get_quality_commitment(run_id)
    assert stored is not None
    return parse_commitment(stored)


def parse_commitment(row: Mapping[str, Any]) -> dict[str, Any]:
    payload = {
        "run_id": row["run_id"],
        "schema_version": row["schema_version"],
        "quality_version": row["quality_version"],
        "frame": json.loads(row["frame_json"]),
        "selected": json.loads(row["selected_json"]),
        "contracts": json.loads(row["contracts_json"]),
        "frame_digest": row["frame_digest"],
        "selected_digest": row["selected_digest"],
        "contracts_digest": row["contracts_digest"],
        "version_digest": row["version_digest"],
        "commitment_digest": row["commitment_digest"],
        "committed_at": row["created_at"],
    }
    payload["site_id"] = payload["frame"].get("site_id")
    return payload


def get_commitment(storage: Storage, run_id: str) -> dict[str, Any] | None:
    row = storage.get_quality_commitment(run_id)
    return parse_commitment(row) if row else None


COMMITMENT_FIELDS = (
    "run_id", "site_id", "schema_version", "quality_version", "frame", "selected",
    "contracts", "frame_digest", "selected_digest", "contracts_digest", "version_digest",
)


def verify_commitment(
    commitment: Mapping[str, Any],
    *,
    frame: Mapping[str, Any] | None = None,
    contracts: Mapping[str, Mapping[str, Any]] | None = None,
) -> None:
    if commitment["version_digest"] != _version_digest():
        raise ValueError("quality commitment version digest is stale")
    if frame is not None and frame_digest(frame) != commitment["frame_digest"]:
        raise ValueError("frame or seed does not match the pre-execution commitment")
    if contracts is not None and contracts_digest(contracts) != commitment["contracts_digest"]:
        raise ValueError("metadata contracts do not match the pre-execution commitment")
    payload = {field: commitment[field] for field in COMMITMENT_FIELDS}
    if fingerprint(payload) != commitment["commitment_digest"]:
        raise ValueError("quality commitment digest does not match its contents")


def audit_sample(storage: Storage, run_id: str, frame: dict[str, Any]) -> dict[str, Any]:
    """Show observed attempt paths, without certifying population quality."""
    if storage.get_run(run_id) is None:
        raise ValueError(f"run not found: {run_id}")
    commitment = get_commitment(storage, run_id)
    if commitment is not None:
        verify_commitment(commitment, frame=frame)
        selected = list(commitment["selected"])
    else:
        selected = draw_frame(frame)
    planned = {row["url"] for row in storage.get_planned(run_id)
               if row["site_id"] == frame["site_id"]}
    if any(url not in planned for url in selected):
        raise ValueError("selected URL is missing from the run plan")
    events = storage.get_request_events(run_id, site_id=frame["site_id"])
    observations = []
    for url in selected:
        attempts = sorted((event for event in events if event["url"] == url),
                          key=lambda event: (event["attempt"], event["event_id"]))
        observations.append({
            "url": url,
            "attempts": len(attempts),
            "first_outcome": attempts[0]["outcome"] if attempts else None,
            "terminal_outcome": attempts[-1]["outcome"] if attempts else None,
            "first_success": bool(attempts and attempts[0]["outcome"] == "success"),
            "eventual_success": any(event["outcome"] == "success" for event in attempts),
        })
    return {"run_id": run_id, "site_id": frame["site_id"],
            "frame_size": len(frame["urls"]), "sample_size": len(selected),
            "commitment_digest": commitment["commitment_digest"] if commitment else None,
            "observations": observations}
