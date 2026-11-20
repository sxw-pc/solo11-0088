"""Contract-level tests: calibration, closed form, refusals, and legacy shape."""

import math

import pytest

from crawlbudget.change_model import (
    conditional_hidden_change_probability,
    lambda_per_day,
)
from crawlbudget.change_monitor import build_change_monitor
from crawlbudget.storage import Storage
from tests.change_panel import panel, panel_report, pages, seed_panel, workload


def test_calibration_uses_only_p1_to_p4_one_day_success_intervals():
    with Storage(":memory:") as storage:
        seed_panel(storage, panel())
        report = panel_report(storage, workload())
    calibration = report["transient_model"]["calibration"]
    assert calibration["interval_count"] == 16
    assert calibration["different_endpoint_count"] == 4
    assert calibration["pooled_endpoint_difference_probability"] == pytest.approx(0.25)
    assert {entry["url"].rsplit("/", 1)[-1] for entry in calibration["pages"]} == {
        "p1.html", "p2.html", "p3.html", "p4.html"
    }
    # The target's four-day same-fingerprint interval is never a calibration draw,
    # and its three failed days are never counted as zero-change observations.
    assert not any(
        item["url"].endswith("target.html")
        for item in calibration["included_intervals"]
    )
    target_excluded = [
        item for item in calibration["excluded_intervals"]
        if item["url"].endswith("target.html")
    ]
    assert [item["reasons"] for item in target_excluded] == [
        ["intervening_failure_gap", "multi_day_interval"]
    ]
    assert calibration["excluded_failure_observation_count"] == 3

    parameters = report["transient_model"]["parameters"]
    assert parameters["one_day_endpoint_difference_probability"] == pytest.approx(0.25)
    assert parameters["lambda_per_day"] == pytest.approx(0.34657359027997264, rel=1e-9)
    lam = report["transient_model"]["uncertainty"]["lambda_per_day"]
    assert lam["low"] < parameters["lambda_per_day"] < lam["high"]


def test_closed_form_matches_the_model_definition():
    probability = 0.25
    lam = lambda_per_day(probability)
    q = 1 - 2 * probability
    assert math.isclose(q, math.exp(-2 * lam))
    for days in (1, 2, 4, 9):
        expected = 1 - 2 * q ** (days / 2) / (1 + q ** days)
        assert conditional_hidden_change_probability(probability, days) == pytest.approx(
            expected, abs=1e-12
        )


def test_missing_workload_declaration_refuses_probability_but_keeps_current_body():
    with Storage(":memory:") as storage:
        seed_panel(storage, panel())
        report = panel_report(storage)  # no declaration supplied
    assert report["workload_declaration"]["declared"] is False
    model = report["transient_model"]
    assert model["status"] == "refused"
    assert model["reason"] == "workload_not_declared"
    assert model["parameters"] is None
    target = pages(report)["target.html"]
    assert target["transient_review"]["status"] == "refused"
    assert target["transient_review"]["probability"] is None
    assert target["transient_review"]["notify"] is False
    # The current-body result and its reason survive the refusal.
    assert target["current_body"]["status"] == "unchanged"
    assert target["current_body"]["notify"] is False
    assert "says nothing about unobserved days" in target["current_body"]["reason"]


def test_incomplete_declaration_also_refuses():
    with Storage(":memory:") as storage:
        seed_panel(storage, panel())
        report = panel_report(storage, workload(independent=False))
    assert report["transient_model"]["reason"] == "workload_not_declared"
    assert report["transient_model"]["parameters"] is None


def test_third_state_in_calibration_refuses_and_preserves_current_body():
    states = panel()
    states["days"][2]["states"]["p2"] = "C"
    with Storage(":memory:") as storage:
        seed_panel(storage, states)
        report = panel_report(storage, workload())
    assert report["transient_model"]["status"] == "refused"
    assert report["transient_model"]["reason"] == "third_state_observed"
    target = pages(report)["target.html"]
    assert target["current_body"]["status"] == "unchanged"
    assert target["transient_review"]["status"] == "refused"
    assert target["transient_review"]["probability"] is None


def test_insufficient_calibration_samples_refuses():
    with Storage(":memory:") as storage:
        seed_panel(storage, panel())
        report = panel_report(storage, workload(min_calibration_intervals=17))
    assert report["transient_model"]["reason"] == "insufficient_calibration_samples"
    assert report["transient_model"]["parameters"] is None


@pytest.mark.parametrize("constant", [True, False])
def test_non_identifiable_boundary_refuses(constant):
    """All-same (lambda=0) and all-different (lambda=inf) samples are refused."""
    states = panel()
    for index, day in enumerate(states["days"]):
        if constant:
            day["states"] = {page: "A" for page in states["pages"]}
        else:  # every one-day interval differs
            day["states"] = {
                page: ("A" if index % 2 == 0 else "B") for page in states["pages"]
            }
    with Storage(":memory:") as storage:
        seed_panel(storage, states)
        report = panel_report(storage, workload())
    assert report["transient_model"]["reason"] == "non_identifiable_boundary"
    assert report["transient_model"]["parameters"] is None


def test_duplicate_run_date_refuses_time_identification():
    states = panel()
    states["days"][4]["date"] = states["days"][3]["date"]  # two runs share a day
    with Storage(":memory:") as storage:
        seed_panel(storage, states)
        report = panel_report(storage, workload())
    assert report["transient_model"]["status"] == "refused"
    assert report["transient_model"]["reason"] == "dates_not_strictly_increasing"


def test_legacy_monitor_shape_is_preserved_and_private_keys_are_not_leaked():
    with Storage(":memory:") as storage:
        seed_panel(storage, panel())
        report = build_change_monitor(storage, [f"panel-{index}" for index in range(1, 6)])
    target = pages(report)["target.html"]
    assert target["intervals"] == [{
        "from_run": "panel-1", "to_run": "panel-5",
        "from_date": "2026-02-01", "to_date": "2026-02-05",
        "different_bodies": False,
        "intervening_gaps": ["panel-2", "panel-3", "panel-4"],
    }]
    for page in report["pages"]:
        assert "_successes" not in page
    assert report["run_ids"] == [f"panel-{index}" for index in range(1, 6)]
