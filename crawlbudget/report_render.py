"""Stable Markdown and self-contained HTML renderers for run summaries."""

from __future__ import annotations

import html
import json
from typing import Any


def dump_json(summary: dict[str, Any]) -> str:
    return json.dumps(summary, ensure_ascii=False, indent=2) + "\n"


def _cell(value: Any) -> str:
    return ("" if value is None else str(value)).replace("|", "\\|").replace("\n", " ")


def _md_table(headers: list[str], rows: list[list[Any]]) -> str:
    lines = [
        "| " + " | ".join(headers) + " |",
        "|" + "|".join("---" for _ in headers) + "|",
    ]
    lines.extend("| " + " | ".join(_cell(value) for value in row) + " |" for row in rows)
    return "\n".join(lines)


def _sections(summary: dict[str, Any]) -> list[tuple[str, list[str], list[list[Any]], str]]:
    totals = summary["totals"]
    fp = summary["fingerprints"]
    sites = summary["sites"]
    leases = [
        [lease["lease_id"], site["site_id"], lease["job_id"], lease["granted_requests"],
         lease["used_requests"], lease["remaining_requests"], lease["expires_at"],
         lease["reason"]]
        for site in sites for lease in site["leases"]
    ]
    return [
        ("Totals", ["Metric", "Value"], [
            ["Sites", totals["sites"]],
            ["Planned requests", totals["planned_requests"]],
            ["Granted requests", totals["granted_requests"]],
            ["Executed requests", totals["executed_requests"]],
            ["HTTP attempts", totals["http_attempts"]],
            ["Blocked events", totals["blocked_events"]],
            ["Response bytes", totals["response_bytes"]],
        ], "_No totals._"),
        ("Fingerprints", ["Artifact", "SHA-256"], [
            ["Configuration", fp["config"]], ["Seed manifests", fp["seeds"]],
            ["Robots fixtures", fp["robots"]], ["Report", fp["report"]],
        ], "_No fingerprints._"),
        ("Sites", ["Site", "Planned", "Granted", "Executed", "Blocked",
                   "HTTP attempts", "Bytes", "Quota used before"], [
            [site["site_id"], site["planned_requests"], site["granted_requests"],
             site["executed_requests"], site["blocked_events"], site["http_attempts"],
             site["response_bytes"], site["quota_used_before"]]
            for site in sites
        ], "_No sites._"),
        ("Leases", ["Lease", "Site", "Job", "Granted", "Used", "Remaining", "Expires", "Reason"],
         leases, "_No leases._"),
        ("Request status distribution", ["Status", "Attempts"],
         [[key, value] for key, value in summary["request_status_counts"].items()],
         "_No request attempts._"),
        ("Blocked reasons", ["Reason", "Count"],
         [[key, value] for key, value in summary["blocked_reason_counts"].items()],
         "_No blocked events._"),
        ("Robots rule matches", ["Site", "Path", "Decision", "Rule", "Pattern", "Group"], [
            [item["site_id"], item["path"], item["decision"], item["rule_id"],
             item["pattern"], item["group_user_agent"]]
            for item in summary["robots_matches"]
        ], "_No robots rules matched._"),
        ("Warnings", ["Code", "Message", "Context"], [
            [item["code"], item["message"], json.dumps(item["context"], sort_keys=True)]
            for item in summary["warnings"]
        ], "_No warnings._"),
    ]


def render_markdown(summary: dict[str, Any]) -> str:
    run = summary["run"]
    lines = [
        f"# crawlbudget run {run['run_id']}", "",
        f"- Status: `{run['status']}`",
        f"- Dry run: `{str(run['dry_run']).lower()}`",
        f"- Virtual start: `{run['started_at']}`",
        f"- Virtual finish: `{run['finished_at']}`",
        f"- Virtual date: `{run['virtual_date']}`",
    ]
    for title, headers, rows, empty in _sections(summary):
        lines.extend(["", f"## {title}", "", _md_table(headers, rows) if rows else empty])
    return "\n".join(lines) + "\n"


def _html_table(headers: list[str], rows: list[list[Any]], empty: str) -> str:
    head = "".join(f"<th>{html.escape(item)}</th>" for item in headers)
    body = "".join(
        "<tr>" + "".join(f"<td>{html.escape(str(value))}</td>" for value in row) + "</tr>"
        for row in rows
    ) or f'<tr><td colspan="{len(headers)}">{html.escape(empty.strip("_"))}</td></tr>'
    return f"<table><tr>{head}</tr>{body}</table>"


def render_html(summary: dict[str, Any]) -> str:
    run = summary["run"]
    totals = summary["totals"]
    cards = [
        ("Status", run["status"]), ("Dry run", str(run["dry_run"]).lower()),
        ("Virtual start", run["started_at"]), ("Virtual finish", run["finished_at"]),
        ("Requests", f"{totals['executed_requests']} / {totals['granted_requests']} granted"),
        ("HTTP attempts", totals["http_attempts"]),
        ("Response bytes", totals["response_bytes"]),
        ("Blocked events", totals["blocked_events"]),
    ]
    card_html = "".join(
        f'<div class="card"><strong>{html.escape(str(name))}</strong><br>'
        f"{html.escape(str(value))}</div>"
        for name, value in cards
    )
    sections = "".join(
        f"<h2>{html.escape(title)}</h2>{_html_table(headers, rows, empty)}"
        for title, headers, rows, empty in _sections(summary)
    )
    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>crawlbudget run {html.escape(run['run_id'])}</title>
<style>
:root {{ color-scheme: light dark; --line:#8a8f98; --soft:#f3f4f6; }}
body {{ font: 14px/1.5 system-ui, sans-serif; margin: 2rem; max-width: 1200px; }}
h1 {{ font-size: 1.5rem; }} h2 {{ font-size: 1.1rem; margin-top: 2rem; }}
table {{ border-collapse: collapse; width: 100%; margin: .5rem 0 1rem; }}
th, td {{ border: 1px solid var(--line); padding: .35rem .5rem; text-align: left; vertical-align: top; }}
th {{ background: var(--soft); }} code {{ word-break: break-all; }}
.grid {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(180px, 1fr)); gap: .6rem; }}
.card {{ border: 1px solid var(--line); border-radius: 6px; padding: .6rem; }}
.muted {{ opacity: .75; }}
</style>
</head>
<body>
<h1>crawlbudget run {html.escape(run['run_id'])}</h1>
<p class="muted">Read-only summary. No external resources are referenced.</p>
<div class="grid">{card_html}</div>
{sections}</body>
</html>
"""
