"""Calibration sample construction for the ``transient-review/v1`` contract.

Only one-day success-to-success intervals with no intervening failure are
calibration draws.  Failure days stay evidence gaps and multi-day intervals
(including same-fingerprint long gaps) are excluded, never pooled as
zero-change observations.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import date
from typing import Any


def elapsed_days(from_date: str, to_date: str) -> int:
    return (date.fromisoformat(to_date) - date.fromisoformat(from_date)).days


def public_interval(interval: dict[str, Any]) -> dict[str, Any]:
    return {
        **interval,
        "elapsed_days": elapsed_days(interval["from_date"], interval["to_date"]),
    }


def calibration_candidate(
    page: dict[str, Any], interval: dict[str, Any], failure_count: int
) -> dict[str, Any]:
    """Classify one success-to-success interval as a calibration draw or not."""
    reasons = []
    if interval["intervening_gaps"]:
        reasons.append("intervening_failure_gap")
    if elapsed_days(interval["from_date"], interval["to_date"]) != 1:
        reasons.append("multi_day_interval")
    return {
        "site_id": page["site_id"], "url_hash": page["url_hash"],
        "url": page["url"],
        **public_interval(interval),
        "intervening_gaps": list(interval["intervening_gaps"]),
        "failure_observations_excluded": failure_count,
        "reasons": reasons,
    }


def build_calibration_sample(pages: list[dict[str, Any]]) -> dict[str, Any]:
    eligible: list[dict[str, Any]] = []
    excluded: list[dict[str, Any]] = []
    failure_counts: dict[tuple[str, str], int] = defaultdict(int)
    calibration_pages: dict[tuple[str, str], dict[str, Any]] = {}
    for page in pages:
        key = (page["site_id"], page["url_hash"])
        failure_counts[key] = sum(
            1 for item in page["observations"] if item["body_sha256"] is None
        )
        distinct = sorted({
            item["body_sha256"] for item in page["observations"]
            if item["body_sha256"] is not None
        })
        page_intervals = [
            calibration_candidate(page, interval, failure_counts[key])
            for interval in page["intervals"]
        ]
        included = [item for item in page_intervals if not item["reasons"]]
        if included:
            calibration_pages[key] = {
                "site_id": page["site_id"], "url_hash": page["url_hash"],
                "url": page["url"],
                "success_dates": [item["virtual_date"] for item in page["_successes"]],
                "distinct_states": distinct,
                "included_interval_count": len(included),
                "failure_observations_excluded": failure_counts[key],
            }
        for item in page_intervals:
            (excluded if item["reasons"] else eligible).append(item)
    return {
        "interval_unit": (
            "one virtual day between consecutive successful endpoint captures "
            "with no intervening failure observation for the same URL"
        ),
        "pages": [calibration_pages[key] for key in sorted(calibration_pages)],
        "interval_count": len(eligible),
        "different_endpoint_count": sum(
            1 for item in eligible if item["different_bodies"]
        ),
        "pooled_endpoint_difference_probability": (
            sum(1 for item in eligible if item["different_bodies"]) / len(eligible)
            if eligible else None
        ),
        "included_intervals": eligible,
        "excluded_intervals": excluded,
        "excluded_failure_observation_count": sum(failure_counts.values()),
        "scope_statement": (
            "Calibration uses only one-day success-to-success intervals. "
            "Failure days are never counted as zero-change observations and "
            "multi-day intervals (including same-fingerprint long gaps) are "
            "never pooled into the one-day calibration sample."
        ),
    }
