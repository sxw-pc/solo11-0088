import copy
import json

import pytest

from crawlbudget.cli import main
from crawlbudget.errors import GoldenError
from crawlbudget.golden import (
    check_golden_from_storage, compare_golden, compare_values, load_summary,
)
from crawlbudget.pipeline import run_pipeline
from crawlbudget.storage import Storage
from tests.conftest import FIXTURES
from tests.fakes import demo_replay_session


def _run_golden_pipeline(tmp_path, demo_config):
    database = tmp_path / "ledger.sqlite3"
    storage = Storage(database)
    result = run_pipeline(demo_config, storage, "demo-run",
                          reports_dir=tmp_path / "reports",
                          session=demo_replay_session(), sleeper=lambda delay: None)
    return storage, database, result


def test_pipeline_summary_and_reports_match_golden(tmp_path, demo_config):
    storage, _database, result = _run_golden_pipeline(tmp_path, demo_config)
    try:
        expected = load_summary(FIXTURES / "expected" / "demo_summary.json")
        assert result.summary == expected
        assert check_golden_from_storage(storage, "demo-run", FIXTURES / "expected") == []
        text = json.dumps(result.summary)
    finally:
        storage.close()
    assert str(tmp_path) not in text
    assert "2026-01-15T00:00:00Z" in text and "created_at" not in text


def test_golden_diff_reports_fields_and_markdown_line(tmp_path, demo_config):
    storage, _database, result = _run_golden_pipeline(tmp_path, demo_config)
    try:
        changed = copy.deepcopy(result.summary)
        changed["sites"][0]["granted_requests"] = 999
        changed["sites"][1]["executed_requests"] = 0
        changed["fingerprints"]["report"] = "0" * 64
        markdown = result.report_paths["markdown"].read_text(encoding="utf-8").replace(
            "| mock-alpha | 10 | 6 |", "| mock-alpha | 10 | 999 |"
        )
        differences = compare_golden(changed, markdown, FIXTURES / "expected")
    finally:
        storage.close()
    fields = {item["path"]: item for item in differences}
    assert fields["$.sites[0].granted_requests"]["expected"] == 6
    assert fields["$.sites[0].granted_requests"]["actual"] == 999
    assert fields["$.sites[1].executed_requests"]["expected"] == 6
    assert fields["$.sites[1].executed_requests"]["actual"] == 0
    assert fields["$.fingerprints.report"]["expected"] == result.summary["fingerprints"]["report"]
    assert any(item["path"].startswith("summary.md:line") for item in differences)


def test_compare_values_paths_types_and_missing_fixtures(tmp_path):
    actual = {"a": 1, "b": [{"c": 2}], "extra": True}
    expected = {"a": 9, "b": [{"c": 3}]}
    by_path = {item["path"]: item for item in compare_values(actual, expected)}
    assert set(by_path) == {"$.a", "$.b[0].c", "$.extra"}
    assert by_path["$.a"] == {"path": "$.a", "expected": 9, "actual": 1,
                              "message": "value mismatch"}
    assert compare_values(1, "1")[0]["message"] == "type mismatch"
    with pytest.raises(GoldenError) as error:
        compare_golden({}, "", tmp_path / "missing")
    assert error.value.exit_code == 3


def test_golden_cli_success(tmp_path, demo_config, capsys):
    storage, database, _result = _run_golden_pipeline(tmp_path, demo_config)
    storage.close()
    code = main(["check-golden", "--db", str(database), "--run-id", "demo-run",
                 "--expected-dir", str(FIXTURES / "expected"),
                 "--project-root", str(tmp_path)])
    assert code == 0 and "GOLDEN_OK demo-run" in capsys.readouterr().out
