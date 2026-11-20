"""The two pre-authorized change-notification contracts and their page results.

``current-body/v1`` compares two captured successful bodies. ``transient-review/v1``
turns a same-body sparse interval into a model-conditional probability of a
hidden change.  The contracts are defined here as stable, auditable text; the
per-page conclusions here only format decisions made from stored endpoints and
the estimated model.
"""

from __future__ import annotations

from typing import Any

from .change_calibration import elapsed_days
from .change_model import conditional_hidden_change_probability

CONTRACT_CURRENT_BODY = "current-body/v1"
CONTRACT_TRANSIENT_REVIEW = "transient-review/v1"

CURRENT_BODY_CONTRACT = {
    "contract_id": CONTRACT_CURRENT_BODY,
    "authorization": (
        "Pre-authorized: compare the current successful body hash with the "
        "previous successful body hash at the same scoped URL."
    ),
    "question": (
        "Is the current successful body different from the previous successful body?"
    ),
    "notification_rule": "notify when the two captured successful bodies differ",
    "must_not_claim": (
        "Equality of two captures does not assert that the body was unchanged "
        "during failed, skipped, or otherwise unobserved days."
    ),
}

TRANSIENT_REVIEW_CONTRACT = {
    "contract_id": CONTRACT_TRANSIENT_REVIEW,
    "authorization": (
        "Requires an explicit caller declaration that the pages form a stable, "
        "mutually independent workload sharing one symmetric two-state "
        "continuous-time Markov change rate."
    ),
    "question": (
        "Given two successful same-body endpoints separated by T days, what is "
        "the model-conditional probability that at least one change occurred "
        "somewhere inside the interval?"
    ),
    "notification_rule": (
        "raise a manual-review notification when the model-conditional "
        "probability is strictly greater than the configured threshold"
    ),
    "must_not_claim": (
        "A model-conditional probability above threshold is a screening signal, "
        "not evidence that a change actually occurred or that the endpoint "
        "states were observed continuously."
    ),
}


def endpoint(observation: dict[str, Any]) -> dict[str, Any]:
    return {
        "run_id": observation["run_id"],
        "virtual_date": observation["virtual_date"],
        "url": observation["url"],
        "body_sha256": observation["body_sha256"],
    }


# ---------------------------------------------------------------------------
# current-body/v1


def current_body_contract(page: dict[str, Any]) -> dict[str, Any]:
    successes = page["_successes"]
    if len(successes) < 2:
        return {
            "contract_id": CONTRACT_CURRENT_BODY,
            "status": "insufficient_observations",
            "notify": False,
            "reason": "fewer than two successful captures for this URL",
            "previous_success": None,
            "current_success": endpoint(successes[-1]) if successes else None,
            "different_bodies": None,
            "latest_run_failed": bool(page["observations"])
            and page["observations"][-1]["body_sha256"] is None,
            "latest_observation_run": (
                page["observations"][-1]["run_id"] if page["observations"] else None
            ),
            "evaluated_interval": None,
            "comparisons": [],
            "statement": CURRENT_BODY_CONTRACT["must_not_claim"],
        }
    previous, current = successes[-2], successes[-1]
    different = previous["body_sha256"] != current["body_sha256"]
    interval = next(
        (
            item for item in page["intervals"]
            if item["from_run"] == previous["run_id"]
            and item["to_run"] == current["run_id"]
        ),
        None,
    )
    comparisons = [
        {
            "from_run": item["from_run"], "to_run": item["to_run"],
            "from_date": item["from_date"], "to_date": item["to_date"],
            "different_bodies": item["different_bodies"],
            "intervening_gaps": item["intervening_gaps"],
            "is_latest": item is interval,
        }
        for item in page["intervals"]
    ]
    return {
        "contract_id": CONTRACT_CURRENT_BODY,
        "status": "changed" if different else "unchanged",
        "notify": different,
        "reason": (
            "current successful body differs from the previous successful body"
            if different else
            "current successful body equals the previous successful body; "
            "this says nothing about unobserved days in between"
        ),
        "previous_success": endpoint(previous),
        "current_success": endpoint(current),
        "different_bodies": different,
        # The latest run for this URL may have failed; "current" is then the
        # most recent success, not necessarily a capture from today.
        "latest_run_failed": bool(page["observations"])
        and page["observations"][-1]["body_sha256"] is None,
        "latest_observation_run": (
            page["observations"][-1]["run_id"] if page["observations"] else None
        ),
        "evaluated_interval": (
            {**interval, "elapsed_days": elapsed_days(
                interval["from_date"], interval["to_date"])}
            if interval else None
        ),
        "comparisons": comparisons,
        "statement": CURRENT_BODY_CONTRACT["must_not_claim"],
    }


# ---------------------------------------------------------------------------
# transient-review/v1


def transient_review_contract(
    page: dict[str, Any], model: dict[str, Any], policy: dict[str, Any]
) -> dict[str, Any]:
    base = {
        "contract_id": CONTRACT_TRANSIENT_REVIEW,
        "authorized": policy["authorized"],
        "threshold": policy["threshold"],
        "statement": TRANSIENT_REVIEW_CONTRACT["must_not_claim"],
    }
    if model["status"] != "estimated":
        # Refusal leaves the current-body result and reason untouched.
        return {
            **base,
            "status": "refused",
            "notify": False,
            "reason": model["reason"],
            "reason_detail": model["reason_detail"],
            "probability": None,
            "intervals": [
                _refused_interval(item, model["reason"]) for item in page["intervals"]
            ],
            "review_intervals": [],
            "current_body_preserved": True,
        }
    probability = model["parameters"]["one_day_endpoint_difference_probability"]
    intervals = [
        _transient_interval(item, probability, policy["threshold"], model["uncertainty"])
        for item in page["intervals"]
    ]
    review = [item for item in intervals if item["notify"]]
    return {
        **base,
        "status": "notify_manual_review" if review else "no_notification",
        "notify": bool(review),
        "reason": (
            "at least one same-body interval exceeds the review threshold"
            if review else "no same-body interval exceeds the review threshold"
        ),
        "probability": probability,
        "intervals": intervals,
        "review_intervals": review,
        "current_body_preserved": True,
    }


def _refused_interval(interval: dict[str, Any], reason: str) -> dict[str, Any]:
    return {
        **_public_interval(interval),
        "same_body_endpoints": not interval["different_bodies"],
        "conditional_probability": None,
        "notify": False,
        "reason": "model refused: " + str(reason),
        "uncertainty": None,
    }


def _transient_interval(
    interval: dict[str, Any],
    probability: float,
    threshold: float,
    uncertainty: dict[str, Any] | None,
) -> dict[str, Any]:
    days = elapsed_days(interval["from_date"], interval["to_date"])
    same_body = not interval["different_bodies"]
    if same_body:
        conditional = conditional_hidden_change_probability(probability, days)
        reason = (
            "model-conditional probability of at least one unobserved change "
            "between same-body endpoints"
        )
    else:
        conditional = 1.0
        reason = (
            "endpoints differ, so at least one change is already observed; "
            "current-body reports the change"
        )
    return {
        **_public_interval(interval),
        "same_body_endpoints": same_body,
        "conditional_probability": conditional,
        "notify": bool(same_body and conditional > threshold),
        "reason": reason,
        "uncertainty": _interval_uncertainty(uncertainty, same_body, days),
    }


def _public_interval(interval: dict[str, Any]) -> dict[str, Any]:
    return {
        **interval,
        "intervening_gaps": list(interval["intervening_gaps"]),
        "elapsed_days": elapsed_days(interval["from_date"], interval["to_date"]),
    }


def _interval_uncertainty(
    model_uncertainty: dict[str, Any] | None, same_body: bool, days: int
) -> dict[str, Any] | None:
    if not model_uncertainty or not same_body:
        return None
    rate = model_uncertainty["endpoint_difference_probability"]
    return {
        "method": "monotone transform of the Wilson interval for the pooled rate",
        "confidence": model_uncertainty["confidence"],
        "low": conditional_hidden_change_probability(rate["low"], days),
        "high": conditional_hidden_change_probability(rate["high"], days),
        "high_is_bound": rate["high"] >= 0.5,
    }
