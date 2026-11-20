"""Model estimation behind the ``transient-review/v1`` change contract.

This module owns the workload declaration, the calibration gates, the
symmetric two-state continuous-time Markov rate estimate, and its uncertainty.
It never renders a page conclusion and never reads storage.

The chain has two body states with equal one-way transition rate ``lambda``.
Over ``t`` days the probability of ending in the other state is
``(1 - exp(-2*lambda*t)) / 2``; inverting at one day gives the per-day rate.
"""

from __future__ import annotations

import math
from typing import Any

from .change_calibration import build_calibration_sample

TRANSIENT_MODEL = "symmetric_two_state_continuous_time_markov"
DEFAULT_THRESHOLD = 0.5
DEFAULT_MIN_CALIBRATION_INTERVALS = 8
WILSON_Z = 1.959963984540054  # two-sided 95%


def workload_policy(workload: dict[str, Any] | None) -> dict[str, Any]:
    declaration = {
        "declared": False, "model": None, "stationary": False,
        "independent": False, "common_rate": False, "symmetric": False,
        "two_state": False,
    }
    if isinstance(workload, dict):
        declaration.update({
            "declared": bool(workload.get("declared", False)),
            "model": workload.get("model"),
            "stationary": bool(workload.get("stationary", False)),
            "independent": bool(workload.get("independent", False)),
            "common_rate": bool(workload.get("common_rate", False)),
            "symmetric": bool(workload.get("symmetric", False)),
            "two_state": bool(workload.get("two_state", False)),
        })
    threshold = float((workload or {}).get("threshold", DEFAULT_THRESHOLD))
    if not 0.0 < threshold < 1.0:
        raise ValueError("threshold must be strictly between 0 and 1")
    minimum = int((workload or {}).get(
        "min_calibration_intervals", DEFAULT_MIN_CALIBRATION_INTERVALS
    ))
    if minimum < 1:
        raise ValueError("min_calibration_intervals must be at least 1")
    authorized = (
        declaration["declared"]
        and declaration["model"] == TRANSIENT_MODEL
        and all(declaration[key] for key in (
            "stationary", "independent", "common_rate", "symmetric", "two_state",
        ))
    )
    return {
        "declaration": declaration, "authorized": authorized,
        "threshold": threshold, "min_calibration_intervals": minimum,
    }


def estimate_transient_model(
    runs: list[dict[str, Any]], pages: list[dict[str, Any]], policy: dict[str, Any]
) -> dict[str, Any]:
    sample = build_calibration_sample(pages)
    model: dict[str, Any] = {
        "model": TRANSIENT_MODEL, "status": "refused",
        "reason": None, "reason_detail": None,
        "threshold": policy["threshold"],
        "min_calibration_intervals": policy["min_calibration_intervals"],
        "calibration": sample, "parameters": None, "uncertainty": None,
    }

    def refuse(reason: str, detail: str) -> dict[str, Any]:
        model["reason"] = reason
        model["reason_detail"] = detail
        return model

    if not policy["authorized"]:
        return refuse(
            "workload_not_declared",
            "transient-review requires an explicit declaration of a stable, "
            "mutually independent, common-rate symmetric two-state "
            "continuous-time Markov workload",
        )
    dates = [run["virtual_date"] for run in runs]
    if any(later <= earlier for earlier, later in zip(dates, dates[1:])):
        return refuse(
            "dates_not_strictly_increasing",
            "run virtual dates must be strictly increasing for time intervals "
            "to identify the continuous-time rate",
        )
    missing = [
        item["url_hash"] for page in pages for item in page["_successes"]
        if not item["body_sha256"]
    ]
    if missing:
        return refuse(
            "missing_body_hash",
            f"{len(missing)} successful capture(s) lack a usable body hash",
        )
    third_state = []
    for page in pages:
        states = {item["body_sha256"] for item in page["_successes"]
                  if item["body_sha256"]}
        if len(states) > 2:
            third_state.append((page["url_hash"], len(states)))
    if third_state:
        return refuse(
            "third_state_observed",
            "page(s) show more than two body states: " + ", ".join(
                f"{url_hash} ({count} states)" for url_hash, count in third_state
            ),
        )
    if sample["interval_count"] < policy["min_calibration_intervals"]:
        return refuse(
            "insufficient_calibration_samples",
            f"{sample['interval_count']} eligible one-day interval(s), fewer "
            f"than the declared minimum of {policy['min_calibration_intervals']}",
        )
    transitions = sample["different_endpoint_count"]
    total = sample["interval_count"]
    probability = transitions / total
    if transitions == 0 or 2 * transitions >= total:
        return refuse(
            "non_identifiable_boundary",
            "pooled endpoint-difference probability "
            f"{probability:.6f} sits on the symmetric two-state boundary "
            "(lambda=0 or lambda unbounded); no finite rate is identifiable",
        )
    lam = lambda_per_day(probability)
    model.update({
        "status": "estimated", "reason": None, "reason_detail": None,
        "parameters": {
            "one_day_endpoint_difference_probability": probability,
            "lambda_per_day": lam,
            "lambda_units": "state changes per day per direction",
            "identifiable": True,
        },
        "uncertainty": _uncertainty(transitions, total, lam),
    })
    return model


# ---------------------------------------------------------------------------
# Closed-form probability and uncertainty


def lambda_per_day(probability: float) -> float:
    """Symmetric two-state CTMC: P(different after t) = (1-e^(-2*lambda*t))/2."""
    return -math.log(1.0 - 2.0 * probability) / 2.0


def conditional_hidden_change_probability(
    probability: float, days: float
) -> float:
    """P(at least one change | both endpoints observed in the same state).

    P(same state after T) = (1 + exp(-2*lambda*T)) / 2 and
    P(no change over T) = exp(-lambda*T).  Conditioning gives
    ``1 - 2*q**(T/2) / (1 + q**T)`` with ``q = 1 - 2*p_one_day``.
    """
    q = 1.0 - 2.0 * probability
    if q <= 0.0:
        return 1.0
    return 1.0 - 2.0 * q ** (days / 2.0) / (1.0 + q ** days)


def _wilson_interval(
    successes: int, total: int, z: float = WILSON_Z
) -> tuple[float, float]:
    p = successes / total
    denominator = 1.0 + z * z / total
    centre = (p + z * z / (2.0 * total)) / denominator
    margin = z * math.sqrt(
        p * (1.0 - p) / total + z * z / (4.0 * total * total)
    ) / denominator
    return max(0.0, centre - margin), min(1.0, centre + margin)


def _uncertainty(transitions: int, total: int, lam: float) -> dict[str, Any]:
    low, high = _wilson_interval(transitions, total)
    return {
        "method": "Wilson score interval for a binomial proportion",
        "confidence": 0.95,
        "assumption_note": (
            "The interval treats the pooled one-day endpoint comparisons as "
            "independent Bernoulli draws under the caller's declaration. It "
            "does not model dependence among pages or serially correlated "
            "daily states; use it as a screening width, not a guarantee."
        ),
        "endpoint_difference_probability": {
            "estimate": transitions / total, "low": low, "high": high,
        },
        "lambda_per_day": {
            "estimate": lam,
            "low": lambda_per_day(low),
            "high": lambda_per_day(high) if high < 0.5 else None,
            "high_unbounded": high >= 0.5,
        },
    }
