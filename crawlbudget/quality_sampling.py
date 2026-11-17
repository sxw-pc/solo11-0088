"""Deterministic URL sample intake and observed response audit."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from .models import SiteConfig
from .scope import scope_url
from .storage import Storage


def load_frame(path: str | Path, site: SiteConfig) -> dict[str, Any]:
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(raw, dict) or set(raw) != {"site_id", "seed", "sample_size", "urls"}:
        raise ValueError("sample frame has invalid fields")
    urls = raw["urls"]
    if raw["site_id"] != site.site_id or not isinstance(raw["seed"], str) or not raw["seed"]:
        raise ValueError("sample frame site or seed is invalid")
    if not isinstance(urls, list) or not urls or any(not isinstance(url, str) for url in urls):
        raise ValueError("sample frame URLs are invalid")
    if len(set(urls)) != len(urls):
        raise ValueError("sample frame has duplicate URLs")
    if any(not scope_url(url, site).allowed for url in urls):
        raise ValueError("sample frame contains an out-of-scope URL")
    n = raw["sample_size"]
    if type(n) is not int or n <= 0 or n > len(urls):
        raise ValueError("sample size is invalid")
    return raw


def draw_frame(frame: dict[str, Any]) -> list[str]:
    """Draw without replacement by seeded hash rank; tie-break by URL."""
    seed = frame["seed"]
    return sorted(frame["urls"], key=lambda url: (
        hashlib.sha256(f"{seed}|{url}".encode("utf-8")).hexdigest(), url,
    ))[:frame["sample_size"]]


def audit_sample(storage: Storage, run_id: str, frame: dict[str, Any]) -> dict[str, Any]:
    """Show observed attempt paths, without certifying population quality."""
    if storage.get_run(run_id) is None:
        raise ValueError(f"run not found: {run_id}")
    selected = draw_frame(frame)
    planned = {row["url"] for row in storage.get_planned(run_id)
               if row["site_id"] == frame["site_id"]}
    if any(url not in planned for url in selected):
        raise ValueError("selected URL is missing from the run plan")
    events = storage.get_request_events(run_id, site_id=frame["site_id"])
    observations = []
    for url in selected:
        attempts = sorted((event for event in events if event["url"] == url),
                          key=lambda event: (event["attempt"], event["event_id"]))
        observations.append({
            "url": url,
            "attempts": len(attempts),
            "first_outcome": attempts[0]["outcome"] if attempts else None,
            "terminal_outcome": attempts[-1]["outcome"] if attempts else None,
            "first_success": bool(attempts and attempts[0]["outcome"] == "success"),
            "eventual_success": any(event["outcome"] == "success" for event in attempts),
        })
    return {"run_id": run_id, "site_id": frame["site_id"],
            "frame_size": len(frame["urls"]), "sample_size": len(selected),
            "observations": observations}
