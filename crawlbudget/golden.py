"""Golden report comparison with precise field-level differences."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .errors import GOLDEN_INPUT_MISSING, GoldenError
from .storage import Storage
from .util import read_fixture_text, sha256_text


def compare_golden(
    actual_summary: dict[str, Any],
    actual_markdown: str,
    expected_dir: str | Path,
) -> list[dict[str, Any]]:
    expected = Path(expected_dir)
    json_path = expected / "demo_summary.json"
    md_path = expected / "demo_summary.md"
    if not json_path.exists() or not md_path.exists():
        raise GoldenError("golden fixture set is incomplete", details={
            "expected_dir": expected.name, "json": json_path.name, "md": md_path.name,
        })
    try:
        expected_summary = load_summary(json_path)
    except (json.JSONDecodeError, OSError) as exc:
        raise GoldenError("golden JSON cannot be read", details={
            "error": str(exc), "code": GOLDEN_INPUT_MISSING,
        }) from exc
    differences = compare_values(actual_summary, expected_summary)
    expected_md = read_fixture_text(md_path).replace("\r\n", "\n")
    actual_md = actual_markdown.replace("\r\n", "\n")
    if expected_md != actual_md:
        differences.append(_markdown_difference(expected_md, actual_md))
    return differences


def compare_values(
    actual: Any, expected: Any, path: str = "$"
) -> list[dict[str, Any]]:
    if type(actual) is not type(expected):
        return [{
            "path": path, "expected": expected, "actual": actual,
            "message": "type mismatch", "expected_type": type(expected).__name__,
            "actual_type": type(actual).__name__,
        }]
    if isinstance(actual, dict):
        differences: list[dict[str, Any]] = []
        for key in sorted(set(actual) | set(expected)):
            child = f"{path}.{key}"
            if key not in expected:
                differences.append(
                    {"path": child, "expected": "<missing>", "actual": actual[key],
                     "message": "unexpected field"}
                )
            elif key not in actual:
                differences.append(
                    {"path": child, "expected": expected[key], "actual": "<missing>",
                     "message": "missing field"}
                )
            else:
                differences.extend(compare_values(actual[key], expected[key], child))
        return differences
    if isinstance(actual, list):
        differences = []
        if len(actual) != len(expected):
            differences.append(
                {"path": path, "expected": len(expected), "actual": len(actual),
                 "message": "list length mismatch"}
            )
        for index, (actual_item, expected_item) in enumerate(zip(actual, expected)):
            differences.extend(compare_values(actual_item, expected_item, f"{path}[{index}]"))
        return differences
    if actual != expected:
        return [{"path": path, "expected": expected, "actual": actual,
                 "message": "value mismatch"}]
    return []


def check_golden_from_storage(
    storage: Storage, run_id: str, expected_dir: str | Path
) -> list[dict[str, Any]]:
    report = storage.get_report(run_id)
    if report is None:
        raise GoldenError("run has no persisted report", details={
            "run_id": run_id, "error": "REPORT_MISSING",
        })
    return compare_golden(
        json.loads(report["summary_json"]),
        str(report["summary_md"]),
        expected_dir,
    )


def load_summary(path: str | Path) -> dict[str, Any]:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def _markdown_difference(expected: str, actual: str) -> dict[str, Any]:
    expected_lines, actual_lines = expected.splitlines(), actual.splitlines()
    for index, (old, new) in enumerate(zip(expected_lines, actual_lines), start=1):
        if old != new:
            return {"path": f"summary.md:line {index}", "expected": old, "actual": new,
                    "message": "markdown line mismatch"}
    if len(expected_lines) != len(actual_lines):
        return {"path": "summary.md", "expected": f"{len(expected_lines)} lines",
                "actual": f"{len(actual_lines)} lines",
                "message": "markdown line count mismatch"}
    return {"path": "summary.md", "expected": sha256_text(expected),
            "actual": sha256_text(actual), "message": "markdown content mismatch"}
