"""Cross-run observations of page-body changes at the same scoped URL."""

from __future__ import annotations

from collections import defaultdict
from typing import Any, Iterable

from .storage import Storage


def build_change_monitor(storage: Storage, run_ids: Iterable[str]) -> dict[str, Any]:
    """Compare successful body hashes; missing captures remain evidence gaps."""
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
    by_url: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for run in runs:
        events = storage.get_request_events(run["run_id"])
        attempts: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
        for event in events:
            attempts[(event["site_id"], event["url_hash"])].append(event)
        for key, rows in attempts.items():
            last = max(rows, key=lambda row: (row["attempt"], row["event_id"]))
            by_url[key].append({
                "run_id": run["run_id"], "virtual_date": run["virtual_date"],
                "url": last["url"], "outcome": last["outcome"],
                "body_sha256": last["body_sha256"] if last["outcome"] == "success" else None,
            })
    pages = []
    for (site_id, url_hash), observations in sorted(by_url.items()):
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
                    "intervening_gaps": gaps,
                })
            previous = item
            gaps = []
        pages.append({
            "site_id": site_id, "url_hash": url_hash,
            "url": observations[-1]["url"],
            "observations": observations, "intervals": intervals,
            "latest_success_date": previous["virtual_date"] if previous else None,
            "latest_body_sha256": previous["body_sha256"] if previous else None,
        })
    return {"run_ids": [run["run_id"] for run in runs], "pages": pages}
