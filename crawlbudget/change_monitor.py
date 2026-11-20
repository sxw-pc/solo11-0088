"""Cross-run observations of page-body changes at the same scoped URL.

The module exposes two independently authorized notification contracts over the
same per-URL endpoint sequence:

``current-body/v1``
    Compares the current successful body hash with the previous successful body
    hash.  Equality is a statement about those two captures only; it never
    claims that the page stayed unchanged during failed or unobserved days.

``transient-review/v1``
    Only when the caller explicitly declares a stationary, mutually
    independent, common-rate, symmetric two-state continuous-time Markov
    workload does it estimate a shared per-day transition rate from one-day
    endpoint intervals and convert same-body sparse intervals into a
    model-conditional probability of at least one hidden change.  Above the
    threshold it raises a manual-review notification: a screening signal, not
    evidence that a change actually occurred.

Every interval, gap, calibration sample, and probability is derived from run
``virtual_date`` values and the terminal per-URL event sequence in
``Storage.get_request_events``.  A changed successful ``body_sha256`` or a
changed terminal failure therefore changes the endpoints, gaps, calibration,
model estimate, and notification result; no presentation-only threshold can
manufacture that conclusion.
"""

from __future__ import annotations

from collections import defaultdict
from typing import Any, Iterable

from .change_contracts import (
    CONTRACT_CURRENT_BODY,
    CONTRACT_TRANSIENT_REVIEW,
    CURRENT_BODY_CONTRACT,
    TRANSIENT_REVIEW_CONTRACT,
    current_body_contract,
    transient_review_contract,
)
from .change_model import (
    TRANSIENT_MODEL,
    estimate_transient_model,
    workload_policy,
)
from .storage import Storage

__all__ = [
    "CONTRACT_CURRENT_BODY", "CONTRACT_TRANSIENT_REVIEW", "TRANSIENT_MODEL",
    "build_change_monitor",
]


def build_change_monitor(
    storage: Storage,
    run_ids: Iterable[str],
    *,
    workload: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build endpoint comparisons plus the two independent change contracts.

    ``workload`` declares the assumptions required by ``transient-review/v1``::

        {
            "declared": True,
            "model": "symmetric_two_state_continuous_time_markov",
            "stationary": True,
            "independent": True,
            "common_rate": True,
            "symmetric": True,
            "two_state": True,
            "threshold": 0.5,
            "min_calibration_intervals": 8,
        }

    Without a complete declaration the transient contract refuses to emit a
    probability while the current-body contract keeps producing its ordinary
    result and reason.
    """
    ids = list(dict.fromkeys(run_ids))
    if not ids:
        raise ValueError("at least one run is required")
    runs = []
    for run_id in ids:
        run = storage.get_run(run_id)
        if run is None:
            raise ValueError(f"unknown run: {run_id}")
        runs.append(run)
    runs.sort(key=lambda run: (run["virtual_date"], run["run_id"]))

    pages = _collect_pages(storage, runs)
    policy = workload_policy(workload)
    model = estimate_transient_model(runs, pages, policy)

    notifications: dict[str, list[dict[str, Any]]] = {
        "current_body": [], "transient_review": [],
    }
    for page in pages:
        page["current_body"] = current_body_contract(page)
        if page["current_body"]["notify"]:
            notifications["current_body"].append(_notification(page, page["current_body"]))
        page["transient_review"] = transient_review_contract(page, model, policy)
        for interval in page["transient_review"]["review_intervals"]:
            notifications["transient_review"].append({
                **_notification(page, page["transient_review"]),
                "from_run": interval["from_run"], "to_run": interval["to_run"],
                "from_date": interval["from_date"], "to_date": interval["to_date"],
                "elapsed_days": interval["elapsed_days"],
                "conditional_probability": interval["conditional_probability"],
                "threshold": policy["threshold"],
            })
        page.pop("_successes", None)

    return {
        "run_ids": [run["run_id"] for run in runs],
        "contracts": {
            "current_body": CURRENT_BODY_CONTRACT,
            "transient_review": TRANSIENT_REVIEW_CONTRACT,
        },
        "workload_declaration": policy["declaration"],
        "transient_model": model,
        "notifications": notifications,
        "pages": pages,
    }


def _collect_pages(
    storage: Storage, runs: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """Collapse each run's retries to one terminal event per ``(site, url)``.

    Only the terminal attempt of a run contributes an endpoint.  A terminal
    success has a body hash; a terminal failure is an evidence gap.  Retries
    within one run therefore never inflate the independent state observations.
    """
    by_url: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for run in runs:
        attempts: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
        for event in storage.get_request_events(run["run_id"]):
            attempts[(event["site_id"], event["url_hash"])].append(event)
        for key, rows in attempts.items():
            last = max(rows, key=lambda row: (row["attempt"], row["event_id"]))
            by_url[key].append({
                "run_id": run["run_id"], "virtual_date": run["virtual_date"],
                "url": last["url"], "outcome": last["outcome"],
                "body_sha256": (
                    last["body_sha256"] if last["outcome"] == "success" else None
                ),
            })
    return [
        _page_record(site_id, url_hash, observations)
        for (site_id, url_hash), observations in sorted(by_url.items())
    ]


def _page_record(
    site_id: str, url_hash: str, observations: list[dict[str, Any]]
) -> dict[str, Any]:
    """Keep the original interval shape byte-for-byte compatible."""
    intervals = []
    previous = None
    gaps: list[str] = []
    for item in observations:
        if item["body_sha256"] is None:
            gaps.append(item["run_id"])
            continue
        if previous is not None:
            intervals.append({
                "from_run": previous["run_id"], "to_run": item["run_id"],
                "from_date": previous["virtual_date"], "to_date": item["virtual_date"],
                "different_bodies": previous["body_sha256"] != item["body_sha256"],
                "intervening_gaps": list(gaps),
            })
        previous = item
        gaps = []
    successes = [item for item in observations if item["body_sha256"] is not None]
    return {
        "site_id": site_id, "url_hash": url_hash,
        "url": observations[-1]["url"],
        "observations": observations, "intervals": intervals,
        "latest_success_date": previous["virtual_date"] if previous else None,
        "latest_body_sha256": previous["body_sha256"] if previous else None,
        "_successes": successes,
    }


def _notification(page: dict[str, Any], contract: dict[str, Any]) -> dict[str, Any]:
    return {
        "contract_id": contract["contract_id"],
        "site_id": page["site_id"],
        "url_hash": page["url_hash"],
        "url": page["url"],
        "status": contract["status"],
        "reason": contract["reason"],
    }
