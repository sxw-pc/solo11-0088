"""Run summaries, report persistence, and golden comparison."""

from __future__ import annotations

import json
from collections import Counter
from pathlib import Path
from typing import Any

from .config import ConfigBundle
from .models import Plan
from .report_render import dump_json, render_html, render_markdown
from .storage import Storage
from .util import SCHEMA_VERSION, fingerprint


def build_summary(
    config: ConfigBundle,
    plan: Plan,
    storage: Storage,
    run: dict[str, Any],
) -> dict[str, Any]:
    run_id = run["run_id"]
    planned = storage.get_planned(run_id)
    leases = storage.get_leases(run_id)
    events = storage.get_request_events(run_id)
    blocked = storage.get_blocked(run_id)
    warnings = storage.get_warnings(run_id)
    raw_status = Counter(
        str(event["status_code"]) if event["status_code"] is not None else "no_status"
        for event in events
    )
    status_counts = {
        key: raw_status[key]
        for key in sorted(
            raw_status, key=lambda key: (0, int(key)) if key.isdigit() else (1, key)
        )
    }
    blocked_counts = dict(sorted(Counter(str(row["reason"]) for row in blocked).items()))
    top_blocked = [
        {"reason": reason, "count": count}
        for reason, count in sorted(blocked_counts.items(), key=lambda item: (-item[1], item[0]))[:3]
    ]
    site_payloads = []
    for site_id in sorted(config.sites):
        site = config.sites[site_id]
        site_planned = [row for row in planned if row["site_id"] == site_id]
        site_events = [row for row in events if row["site_id"] == site_id]
        site_leases = [
            {key: lease[key] for key in (
                "lease_id", "job_id", "granted_requests", "used_requests",
                "remaining_requests", "expires_at", "reason",
            )}
            for lease in leases if lease["site_id"] == site_id
        ]
        site_payloads.append({
            "site_id": site_id, "base_url": site.base_url, "enabled": site.enabled,
            "daily_quota": site.daily_quota,
            "quota_used_before": plan.quota_used_before.get(site_id, 0),
            "max_requests_per_run": site.max_requests_per_run,
            "planned_requests": len(site_planned),
            "granted_requests": sum(1 for row in site_planned if row["allowed"]),
            "executed_requests": sum(1 for row in site_planned if row["completed"]),
            "blocked_events": sum(1 for row in blocked if row["site_id"] == site_id),
            "http_attempts": len(site_events),
            "response_bytes": sum(int(row["response_bytes"]) for row in site_events),
            "leases": site_leases,
        })
    summary: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "run": {
            "run_id": run_id,
            "status": run["status"],
            "dry_run": bool(run["dry_run"]),
            "started_at": run["started_at"],
            "finished_at": run["finished_at"],
            "virtual_date": run["virtual_date"],
        },
        "fingerprints": {
            "config": run["config_fingerprint"],
            "seeds": run["seeds_fingerprint"],
            "robots": run["robots_fingerprint"],
            "report": "",
        },
        "totals": {
            "sites": len(config.sites),
            "jobs": len(config.jobs),
            "planned_requests": len(planned),
            "granted_requests": sum(1 for row in planned if row["allowed"]),
            "executed_requests": sum(1 for row in planned if row["completed"]),
            "http_attempts": len(events),
            "blocked_events": len(blocked),
            "response_bytes": sum(int(row["response_bytes"]) for row in events),
        },
        "sites": site_payloads,
        "request_status_counts": status_counts,
        "blocked_reason_counts": blocked_counts,
        "top_blocked_reasons": top_blocked,
        "robots_matches": _robots_matches(planned),
        "warnings": [
            {"code": row["code"], "message": row["message"],
             "context": _json_or_empty(row["context_json"])}
            for row in warnings
        ],
    }
    summary["fingerprints"]["report"] = fingerprint(summary)
    return summary


def write_report_files(
    reports_dir: str | Path, run_id: str, summary: dict[str, Any]
) -> dict[str, Path]:
    target = Path(reports_dir) / run_id
    target.mkdir(parents=True, exist_ok=True)
    paths = {
        "json": target / "summary.json",
        "markdown": target / "summary.md",
        "html": target / "summary.html",
    }
    _write_text(paths["json"], dump_json(summary))
    _write_text(paths["markdown"], render_markdown(summary))
    _write_text(paths["html"], render_html(summary))
    return paths


def persist_report(
    storage: Storage,
    reports_dir: str | Path,
    run_id: str,
    summary: dict[str, Any],
    created_at: str,
) -> dict[str, Path]:
    paths = write_report_files(reports_dir, run_id, summary)
    storage.save_report(
        run_id,
        dump_json(summary),
        render_markdown(summary),
        render_html(summary),
        summary["fingerprints"]["report"],
        created_at,
    )
    return paths


def _robots_matches(planned: list[dict[str, Any]]) -> list[dict[str, Any]]:
    found: dict[tuple[Any, ...], dict[str, Any]] = {}
    for row in planned:
        evidence = _json_or_empty(row["robots_evidence_json"])
        if not evidence:
            continue
        rule_id = evidence.get("rule_id")
        if not rule_id and evidence.get("reason") != "robots_missing":
            continue
        item = {
            "site_id": row["site_id"], "path": evidence.get("path", ""),
            "decision": evidence.get("decision", ""), "rule_id": rule_id or "<none>",
            "pattern": evidence.get("pattern") or "<none>",
            "group_user_agent": evidence.get("group_user_agent", ""),
            "reason": evidence.get("reason", ""),
        }
        key = tuple(item[name] for name in ("site_id", "path", "decision", "rule_id", "pattern"))
        found[key] = item
    return [found[key] for key in sorted(found)]


def _json_or_empty(text: str | None) -> dict[str, Any]:
    try:
        value = json.loads(text) if text else {}
    except json.JSONDecodeError:
        return {}
    return value if isinstance(value, dict) else {}


def _write_text(path: Path, text: str) -> None:
    with path.open("w", encoding="utf-8", newline="\n") as handle:
        handle.write(text)
