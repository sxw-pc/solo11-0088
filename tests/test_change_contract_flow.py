"""End-to-end tests: target contracts, ledger propagation, and the CLI surface."""

import json

import pytest

from crawlbudget.change_monitor import (
    CONTRACT_CURRENT_BODY,
    CONTRACT_TRANSIENT_REVIEW,
    build_change_monitor,
)
from crawlbudget.cli import main
from crawlbudget.pipeline import run_pipeline
from crawlbudget.storage import Storage
from crawlbudget.util import sha256_text
from tests.change_panel import (
    panel,
    panel_report,
    pages,
    pipeline_config,
    seed_panel,
    session_for,
    workload,
)


def test_two_contracts_disagree_on_same_target_interval():
    with Storage(":memory:") as storage:
        seed_panel(storage, panel())
        report = panel_report(storage, workload())
    result_pages = pages(report)
    target = result_pages["target.html"]

    # current-body: only the two captured successes are compared, so no alert.
    current = target["current_body"]
    assert current["contract_id"] == CONTRACT_CURRENT_BODY
    assert current["different_bodies"] is False
    assert current["notify"] is False
    assert current["status"] == "unchanged"
    assert "does not assert that the body was unchanged" in current["statement"]
    assert current["evaluated_interval"]["intervening_gaps"] == [
        "panel-2", "panel-3", "panel-4"
    ]

    # transient-review: the model-conditional hidden-change probability crosses 0.5.
    transient = target["transient_review"]
    assert transient["contract_id"] == CONTRACT_TRANSIENT_REVIEW
    assert transient["status"] == "notify_manual_review"
    assert transient["notify"] is True
    assert len(transient["review_intervals"]) == 1
    row = transient["review_intervals"][0]
    assert row["elapsed_days"] == 4
    assert row["conditional_probability"] == pytest.approx(9 / 17, abs=1e-12)
    assert "not evidence that a change actually occurred" in transient["statement"]
    assert [
        item["contract_id"] for item in report["notifications"]["transient_review"]
    ] == [CONTRACT_TRANSIENT_REVIEW]
    # The separate p4 hash change is the only current-body alert; target A/A is not.
    assert all(
        not item["url"].endswith("target.html")
        for item in report["notifications"]["current_body"]
    )
    assert [
        item["url"].rsplit("/", 1)[-1]
        for item in report["notifications"]["current_body"]
    ] == ["p4.html"]


def test_changed_success_hash_propagates_through_model_and_notification():
    states = panel()
    # Flatten p4 to A on the final day: one calibration transition disappears.
    states["days"][4]["states"]["p4"] = "A"
    with Storage(":memory:") as storage:
        seed_panel(storage, states)
        report = panel_report(storage, workload())
    calibration = report["transient_model"]["calibration"]
    assert calibration["interval_count"] == 16
    assert calibration["different_endpoint_count"] == 3
    assert calibration["pooled_endpoint_difference_probability"] == pytest.approx(3 / 16)
    target = pages(report)["target.html"]
    expected = conditional(3 / 16, 4)
    assert expected < 0.5
    assert target["transient_review"]["review_intervals"] == []
    assert target["transient_review"]["status"] == "no_notification"
    assert target["transient_review"]["intervals"][0]["conditional_probability"] == (
        pytest.approx(expected, abs=1e-12)
    )
    assert target["current_body"]["notify"] is False


def test_changed_failure_terminal_removes_observations_and_gaps():
    states = panel()
    states["days"][2]["states"]["p1"] = None  # p1 fails on day 3
    with Storage(":memory:") as storage:
        seed_panel(storage, states)
        report = panel_report(storage, workload())
    result_pages = pages(report)
    assert result_pages["p1.html"]["observations"][2]["body_sha256"] is None
    calibration = report["transient_model"]["calibration"]
    # p1 keeps only the day1->day2 and day4->day5 one-day success intervals.
    assert calibration["interval_count"] == 14
    excluded = [
        item for item in calibration["excluded_intervals"]
        if item["url"].endswith("p1.html")
    ]
    assert excluded and excluded[0]["reasons"] == [
        "intervening_failure_gap", "multi_day_interval"
    ]
    assert calibration["excluded_failure_observation_count"] == 4
    target = result_pages["target.html"]
    assert target["current_body"]["notify"] is False
    assert target["transient_review"]["notify"] is True


def test_same_run_retries_collapse_to_one_terminal_observation():
    def add_retry(run_id, page, state, events):
        if run_id == "panel-1" and page == "p1":
            return [
                {"page": page, "state": None, "attempt": 1,
                 "outcome": "retryable_error"},
                {"page": page, "state": state, "attempt": 2, "outcome": "success"},
            ]
        return events

    with Storage(":memory:") as storage:
        seed_panel(storage, panel(), events_mutator=add_retry)
        report = panel_report(storage, workload())
    calibration = report["transient_model"]["calibration"]
    # The retry is not an independent state observation.
    assert calibration["interval_count"] == 16
    assert calibration["different_endpoint_count"] == 4
    assert calibration["excluded_failure_observation_count"] == 3


def test_executor_hash_and_failure_changes_reach_the_monitor(tmp_path):
    """Real runs: executor body hashes and 503 terminals drive every conclusion."""
    config = pipeline_config(tmp_path)
    states = panel()
    run_ids = [f"real-run-{index}" for index in range(1, 6)]
    with Storage(":memory:") as storage:
        for run_id, day in zip(run_ids, states["days"]):
            run_pipeline(
                config, storage, run_id,
                virtual_start=day["date"] + "T00:00:00Z",
                reports_dir=tmp_path / "reports", session=session_for(day),
            )
        report = build_change_monitor(storage, run_ids, workload=workload())
        # Mutate only the ledger's terminal events: a real 503 becomes a success,
        # which must remove a failure gap downstream.
        storage.conn.execute(
            "UPDATE request_events SET outcome='success', status_code=200, "
            "body_sha256=? WHERE run_id='real-run-3' AND url LIKE '%/target.html'",
            (sha256_text("target-A"),),
        )
        storage.conn.commit()
        mutated = build_change_monitor(storage, run_ids, workload=workload())

    assert report["transient_model"]["parameters"]["lambda_per_day"] == pytest.approx(
        0.34657359027997264, rel=1e-9
    )
    target = pages(report)["target.html"]
    assert target["transient_review"]["notify"] is True
    assert target["transient_review"]["review_intervals"][0]["intervening_gaps"] == [
        "real-run-2", "real-run-3", "real-run-4"
    ]
    mutated_target = pages(mutated)["target.html"]
    # One failed day becomes a success: the single 4-day gap becomes two 2-day
    # same-body intervals. The probability drops below threshold, so the
    # notification disappears -- driven entirely by the ledger event change.
    assert mutated_target["current_body"]["evaluated_interval"]["intervening_gaps"] == [
        "real-run-4"
    ]
    rows = mutated_target["transient_review"]["intervals"]
    assert [row["elapsed_days"] for row in rows] == [2, 2]
    assert [row["intervening_gaps"] for row in rows] == [
        ["real-run-2"], ["real-run-4"]
    ]
    assert rows[0]["conditional_probability"] == pytest.approx(0.2, abs=1e-12)
    assert mutated_target["transient_review"]["notify"] is False


def conditional(probability, days):
    from crawlbudget.change_model import conditional_hidden_change_probability
    return conditional_hidden_change_probability(probability, days)


def test_cli_exposes_contracts_calibration_and_notifications(tmp_path, capsys):
    db = tmp_path / "ledger.sqlite3"
    with Storage(db) as storage:
        seed_panel(storage, panel())
    args = [
        "change-monitor", "--db", str(db), "--project-root", str(tmp_path),
        "--declare-transient-workload", "--min-calibration-intervals", "8",
    ]
    for index in range(1, 6):
        args += ["--run-id", f"panel-{index}"]
    assert main(args) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["contracts"]["current_body"]["contract_id"] == "current-body/v1"
    assert payload["contracts"]["transient_review"]["contract_id"] == "transient-review/v1"
    assert payload["workload_declaration"]["declared"] is True
    assert payload["transient_model"]["calibration"]["interval_count"] == 16
    assert payload["transient_model"]["parameters"]["lambda_per_day"] == pytest.approx(
        0.34657359027997264, rel=1e-9
    )
    target = pages(payload)["target.html"]
    assert target["current_body"]["notify"] is False
    assert target["transient_review"]["notify"] is True
    assert target["transient_review"]["review_intervals"][0][
        "conditional_probability"
    ] == pytest.approx(9 / 17, abs=1e-12)
    assert len(payload["notifications"]["transient_review"]) == 1
    assert [
        item["url"].rsplit("/", 1)[-1]
        for item in payload["notifications"]["current_body"]
    ] == ["p4.html"]


def test_cli_without_declaration_reports_refusal_and_current_body(tmp_path, capsys):
    db = tmp_path / "ledger.sqlite3"
    with Storage(db) as storage:
        seed_panel(storage, panel())
    args = ["change-monitor", "--db", str(db), "--project-root", str(tmp_path)]
    for index in range(1, 6):
        args += ["--run-id", f"panel-{index}"]
    assert main(args) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["transient_model"]["reason"] == "workload_not_declared"
    target = pages(payload)["target.html"]
    assert target["current_body"]["status"] == "unchanged"
    assert target["transient_review"]["probability"] is None
    assert payload["notifications"]["transient_review"] == []
