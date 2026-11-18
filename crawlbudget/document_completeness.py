"""Certify captured document metadata from a pre-committed quality sample.

Inspection never promotes an observed sample fraction to a population claim.
Each sampled URL is judged only against the exact successful HTML receipt for
that same URL, every field is checked against contracts that were committed
before execution, and an exact hypergeometric tail inversion produces the
one-sided population lower bound used for the sign/refuse decision.
"""

from __future__ import annotations

import json
from fractions import Fraction
from pathlib import Path
from typing import Any, Mapping

from .metadata_fields import failed_fields, field_states
from .quality_sampling import (
    contracts_digest,
    get_commitment,
    normalize_contracts,
    parse_fraction,
    verify_commitment,
)
from .quality_stats import (
    DEFAULT_ALPHA,
    ONE_SIDED_CONFIDENCE,
    fraction_dict,
    population_bounds,
    population_lower_bound,
)
from .util import fingerprint, sha256_bytes


def load_intake(path: str | Path) -> dict[str, Any]:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError("document intake must be an object")
    body_paths = data.get("body_paths")
    if (not isinstance(body_paths, dict) or not body_paths
            or any(not isinstance(url, str) or not isinstance(body, str)
                   for url, body in body_paths.items())):
        raise ValueError("document intake body_paths are invalid")
    normalized = normalize_contracts(data.get("contracts"))
    frame_path = data.get("frame_path")
    if frame_path is not None and not isinstance(frame_path, str):
        raise ValueError("document intake frame_path is invalid")
    return {
        "raw": data,
        "body_paths": body_paths,
        "contracts": normalized,
        "frame_path": frame_path,
        "digest": fingerprint(data),
    }


def _resolve_body(path_root: Path, reference: str) -> Path:
    candidate = Path(reference)
    return candidate if candidate.is_absolute() else path_root / candidate


def _intake_root(intake_path: Path, body_paths: Mapping[str, str]) -> Path:
    candidates = []
    if len(intake_path.parents) > 2:
        candidates.append(intake_path.parents[2])
    candidates.extend([intake_path.parent, Path.cwd()])
    for candidate in candidates:
        if all(_resolve_body(candidate, body).is_file() for body in body_paths.values()):
            return candidate
    return candidates[0]


def _receipt(events: list[dict[str, Any]], body: bytes) -> dict[str, Any]:
    """Match the one real success event against this URL's captured bytes."""
    content_sha = sha256_bytes(body)
    successes = [event for event in events if event["outcome"] == "success"]
    if not successes:
        return {"status": "missing", "reason": "no_success_receipt", "event": None}
    if len(successes) > 1:
        return {"status": "invalid", "reason": "ambiguous_success_receipts", "event": None}
    event = successes[0]
    if (event["body_sha256"] != content_sha
            or int(event["response_bytes"]) != len(body)
            or not str(event["content_type"]).lower().startswith("text/html")):
        return {"status": "mismatch",
                "reason": "success_receipt_does_not_match_capture", "event": event}
    return {"status": "valid", "reason": "matching_successful_html_receipt",
            "event": event}


def _certificate(
    contract: Mapping[str, Any], *, population: int, sample_size: int,
    qualified: int, alpha: Fraction,
) -> dict[str, Any]:
    minimum = parse_fraction(contract["minimum_population_fraction"])
    lower = population_lower_bound(population, sample_size, qualified, alpha=alpha)
    lower_fraction = Fraction(lower, population) if population else Fraction(0, 1)
    meets = lower_fraction >= minimum
    if qualified == 0 and not meets:
        reason = "no_qualified_sample"
    elif meets:
        reason = "lower_bound_meets_minimum"
    else:
        reason = "insufficient_evidence"
    certificate = {
        "contract": contract["name"],
        "required_fields": list(contract["required_fields"]),
        "minimum_population_fraction": dict(contract["minimum_population_fraction"]),
        "population_size": population,
        "N": population,
        "sample_size": sample_size,
        "n": sample_size,
        "qualified_count": qualified,
        "x": qualified,
        "signed": meets,
        "observed_sample_fraction": {
            "numerator": qualified, "denominator": sample_size,
            "text": f"{qualified}/{sample_size}", "is_population_estimate": False,
        },
        "alpha": fraction_dict(alpha),
        "one_sided_confidence": fraction_dict(ONE_SIDED_CONFIDENCE),
        "lower_bound": lower,
        "lower_bound_fraction": {
            "numerator": lower, "denominator": population,
            "text": f"{lower}/{population}",
            "reduced": f"{lower_fraction.numerator}/{lower_fraction.denominator}",
        },
        "lower_bound_meets_minimum": meets,
        "decision": "signed" if meets else "refused",
        "reason": reason,
        "candidate_bounds": population_bounds(
            population, sample_size, qualified, alpha=alpha
        ),
    }
    certificate["tail_probabilities"] = {
        str(row["population_successes"]): row["tail_probability"]["text"]
        for row in certificate["candidate_bounds"]
    }
    certificate["issued"] = meets
    return certificate


def _row(url: str, reference: str | None, events: list[dict[str, Any]],
         root: Path, contracts: Mapping[str, Mapping[str, Any]]) -> dict[str, Any]:
    row: dict[str, Any] = {
        "url": url, "captured_body": reference, "attempts": len(events),
        "attempt_outcomes": [event["outcome"] for event in events],
        "body_sha256": None, "receipt": None, "receipt_status": "invalid",
        "unqualified_reason": None, "fields": {},
        "qualified": {name: False for name in contracts},
    }
    if reference is None:
        row["unqualified_reason"] = "captured_body_missing"
        return row
    try:
        body = _resolve_body(root, reference).read_bytes()
    except OSError:
        row["unqualified_reason"] = "captured_body_unreadable"
        return row
    row["body_sha256"] = sha256_bytes(body)
    receipt = _receipt(events, body)
    event = receipt["event"]
    row["receipt_status"] = receipt["status"]
    row["receipt"] = {
        key: (event[key] if event else None) for key in (
            "event_id", "attempt", "status_code", "outcome", "response_bytes",
            "content_type", "body_sha256", "recorded_at",
        )
    }
    if receipt["status"] != "valid":
        row["unqualified_reason"] = receipt["reason"]
        return row
    for name, contract in contracts.items():
        states, ok = field_states(body, contract["required_fields"])
        row["fields"][name] = {
            "required_fields": list(contract["required_fields"]),
            "states": states,
            "failed": failed_fields(states),
        }
        row["qualified"][name] = ok
    if not all(row["qualified"].values()):
        row["unqualified_reason"] = "required_fields_not_unique_and_non_empty"
    return row


def inspect_documents(
    storage, run_id: str, frame: dict, intake_path: str | Path, *,
    contracts: Mapping[str, Mapping[str, Any]] | None = None,
) -> dict[str, Any]:
    commitment = get_commitment(storage, run_id)
    if commitment is None:
        raise ValueError("pre-execution quality commitment is missing")
    verify_commitment(commitment, frame=frame, contracts=contracts)
    run = storage.get_run(run_id)
    if run is None:
        raise ValueError(f"run not found: {run_id}")

    intake = load_intake(intake_path)
    committed = commitment["contracts"]
    if contracts_digest(intake["contracts"]) != commitment["contracts_digest"]:
        raise ValueError("metadata contracts do not match the pre-execution commitment")
    if contracts is not None and normalize_contracts(contracts) != committed:
        raise ValueError("metadata contracts do not match the pre-execution commitment")
    root = _intake_root(Path(intake_path).resolve(), intake["body_paths"])
    selected = list(commitment["selected"])
    if len(selected) != commitment["frame"]["sample_size"]:
        raise ValueError("committed sample size is inconsistent")
    events = storage.get_request_events(run_id, site_id=commitment["site_id"])

    rows = []
    for url in selected:
        url_events = sorted(
            (event for event in events if event["url"] == url),
            key=lambda event: (event["attempt"], event["event_id"]),
        )
        rows.append(_row(url, intake["body_paths"].get(url), url_events, root, committed))

    population, sample_size = len(commitment["frame"]["urls"]), len(selected)
    certificates = {
        name: _certificate(
            contract, population=population, sample_size=sample_size,
            qualified=sum(1 for row in rows if row["qualified"][name]),
            alpha=DEFAULT_ALPHA,
        )
        for name, contract in committed.items()
    }
    decisions = {name: cert["decision"] for name, cert in certificates.items()}
    reasons = {name: cert["reason"] for name, cert in certificates.items()}
    run_digest = fingerprint({
        key: run[key] for key in (
            "run_id", "started_at", "virtual_date", "config_fingerprint",
            "seeds_fingerprint", "robots_fingerprint",
        )
    })
    return {
        "run_id": run_id,
        "site_id": commitment["site_id"],
        "run_fingerprint": run_digest,
        "run": {
            "started_at": run["started_at"], "virtual_date": run["virtual_date"],
            "config_fingerprint": run["config_fingerprint"],
            "seeds_fingerprint": run["seeds_fingerprint"],
            "robots_fingerprint": run["robots_fingerprint"],
        },
        "commitment": {
            "commitment_digest": commitment["commitment_digest"],
            "frame_digest": commitment["frame_digest"],
            "selected_digest": commitment["selected_digest"],
            "contracts_digest": commitment["contracts_digest"],
            "version_digest": commitment["version_digest"],
            "schema_version": commitment["schema_version"],
            "quality_version": commitment["quality_version"],
            "committed_at": commitment["committed_at"],
            "intake_digest": intake["digest"],
            "frame_urls": list(commitment["frame"]["urls"]),
            "seed": commitment["frame"]["seed"],
            "sample_urls": selected,
            "contracts": committed,
        },
        "frame_size": population,
        "sample_size": sample_size,
        "N": population,
        "n": sample_size,
        "x": {name: cert["qualified_count"] for name, cert in certificates.items()},
        "rows": rows,
        "contracts": certificates,
        "certificates": certificates,
        "decisions": decisions,
        "decision_reasons": reasons,
        "population_certificate": {
            name: cert if cert["decision"] == "signed" else None
            for name, cert in certificates.items()
        },
    }
