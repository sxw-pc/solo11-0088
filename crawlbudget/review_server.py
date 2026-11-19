"""Read-only local review console for ledger runs and reports."""

from __future__ import annotations

import html
import json
from pathlib import Path
from typing import Any

from flask import Flask, Response, abort, g, jsonify

from .storage import Storage
from .util import is_loopback_host

CSS = """
:root { color-scheme: light dark; --line:#888; --soft:#f2f3f5; }
body { font:14px/1.5 system-ui,sans-serif; margin:2rem; max-width:1400px; }
h1,h2 { font-weight:600; } h2 { margin-top:2rem; }
a { color:#2563a8; } table { border-collapse:collapse; width:100%; margin:.5rem 0 1.2rem; }
th,td { border:1px solid var(--line); padding:.3rem .5rem; text-align:left; vertical-align:top; }
th { background:var(--soft); } code { word-break:break-all; } .muted { opacity:.7; }
.notice { border:1px solid var(--line); padding:.7rem; border-radius:5px; }
"""


def create_review_app(db_path: str | Path) -> Flask:
    app = Flask("crawlbudget_review")
    app.config["CRAWLBUDGET_DB"] = str(db_path)

    def store() -> Storage:
        if "store" not in g:
            g.store = Storage(str(db_path))
        return g.store

    @app.teardown_appcontext
    def close_store(_exc: BaseException | None) -> None:
        existing = g.pop("store", None)
        if existing is not None:
            existing.close()

    @app.after_request
    def security_headers(response: Response) -> Response:
        response.headers["Content-Security-Policy"] = (
            "default-src 'none'; style-src 'unsafe-inline'; img-src data:; "
            "base-uri 'none'; form-action 'none'"
        )
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Cache-Control"] = "no-store"
        return response

    @app.get("/")
    def index() -> str:
        runs = store().list_runs()
        rows = "".join(
            f'<tr><td><a href="/runs/{html.escape(run["run_id"])}">'
            f'{html.escape(run["run_id"])}</a></td>'
            f'<td>{html.escape(run["started_at"])}</td><td>{html.escape(run["status"])}</td>'
            f'<td>{"yes" if run["dry_run"] else "no"}</td>'
            f'<td><code>{html.escape(run["report_fingerprint"][:16])}</code></td></tr>'
            for run in runs
        )
        body = (
            f"<table><tr><th>Run</th><th>Started (virtual)</th><th>Status</th>"
            f"<th>Dry run</th><th>Report fingerprint</th></tr>{rows}</table>"
            if runs else '<p class="notice">No runs yet. Run '
            "<code>python -m crawlbudget demo --reset</code> locally.</p>"
        )
        return _page("crawlbudget review", f"<h1>crawlbudget review</h1>{body}")

    @app.get("/runs/<run_id>")
    def run_detail(run_id: str) -> str:
        db = store()
        run = db.get_run(run_id)
        if run is None:
            abort(404)
        leases = db.get_leases(run_id)
        planned = db.get_planned(run_id)
        events = db.get_request_events(run_id)
        blocked = db.get_blocked(run_id)
        warnings = db.get_warnings(run_id)
        receipts = db.get_dispatch_receipts(run_id)
        report = db.get_report(run_id)
        summary_link = (
            f'<a href="/runs/{html.escape(run_id)}/report">summary.html</a> · '
            f'<a href="/runs/{html.escape(run_id)}/summary.json">summary.json</a>'
            if report else '<span class="muted">report not generated</span>'
        )
        run_info = _kv_table([
            ("Run ID", run["run_id"]), ("Status", run["status"]),
            ("Dry run", "yes" if run["dry_run"] else "no"),
            ("Virtual start", run["started_at"]), ("Virtual finish", run["finished_at"]),
            ("Virtual date", run["virtual_date"]),
            ("Config fingerprint", run["config_fingerprint"]),
            ("Seeds fingerprint", run["seeds_fingerprint"]),
            ("Robots fingerprint", run["robots_fingerprint"]),
            ("Report fingerprint", run["report_fingerprint"]),
        ])
        lease_table = _rows_table(
            ["Lease", "Site", "Job", "Granted", "Used", "Remaining", "Expires", "Reason"],
            [[row["lease_id"], row["site_id"], row["job_id"], row["granted_requests"],
              row["used_requests"], row["remaining_requests"], row["expires_at"],
              row["reason"]] for row in leases],
        )
        plan_table = _rows_table(
            ["#", "Site", "Job", "URL", "Granted", "Reason", "Offset ms", "Completed", "Outcome"],
            [[row["request_order"], row["site_id"], row["job_id"], row["url"],
              "yes" if row["allowed"] else "no", row["block_reason"] or "",
              row["virtual_offset_ms"] if row["virtual_offset_ms"] is not None else "",
              "yes" if row["completed"] else "no", row["final_outcome"] or ""]
             for row in planned],
        )
        event_table = _rows_table(
            ["Site", "URL", "Attempt", "Status", "Outcome", "Bytes", "Elapsed ms", "SHA-256", "At"],
            [[row["site_id"], row["url"], row["attempt"], row["status_code"] or "",
              row["outcome"], row["response_bytes"], row["elapsed_ms"],
              row["body_sha256"][:16], row["recorded_at"]] for row in events],
        )
        blocked_table = _rows_table(
            ["Site", "URL", "Stage", "Reason", "Evidence"],
            [[row["site_id"], row["url"], row["stage"], row["reason"],
              _compact_json(row["evidence_json"])] for row in blocked],
        )
        warning_table = _rows_table(
            ["Code", "Message", "Context"],
            [[row["code"], row["message"], _compact_json(row["context_json"])]
             for row in warnings],
        )
        receipt_table = _rows_table(
            ["Dispatch ID", "Site", "Job", "Attempt", "State", "Delivery",
             "Contract", "Conflict"],
            [[row["dispatch_id"][:16], row["site_id"], row["job_id"], row["attempt"],
              row["receipt_state"], row["delivery_conclusion"],
              row["recovery_contract"] or "-", row["conflict_detail"] or "-"]
             for row in receipts],
        )
        content = (
            f"<h1>Run {html.escape(run_id)}</h1>"
            f'<p><a href="/">← all runs</a> · {summary_link}</p>'
            f"<h2>Run</h2>{run_info}<h2>Budget leases</h2>{lease_table}"
            f"<h2>Planned requests</h2>{plan_table}<h2>Request events</h2>{event_table}"
            f"<h2>Receipt attestations</h2>{receipt_table}"
            f"<h2>Blocked events</h2>{blocked_table}<h2>Warnings</h2>{warning_table}"
            '<p class="muted">This console is read-only and exposes no mutation controls.</p>'
        )
        return _page(f"run {run_id}", content)

    @app.get("/runs/<run_id>/summary.json")
    def run_json(run_id: str) -> Response:
        return _report_response(store().get_report(run_id), "summary_json", "application/json")

    @app.get("/runs/<run_id>/report")
    def run_report(run_id: str) -> Response:
        return _report_response(store().get_report(run_id), "summary_html", "text/html")

    @app.get("/healthz")
    def healthz() -> Response:
        return jsonify({"status": "ok", "mode": "read-only"})

    return app


def serve_review(db_path: str | Path, host: str = "127.0.0.1", port: int = 8765) -> None:
    if not is_loopback_host(host):
        raise ValueError("review console must bind to a loopback address (127.0.0.1 or ::1)")
    if port <= 0 or port > 65535:
        raise ValueError("port must be between 1 and 65535")
    app = create_review_app(db_path)
    app.run(host=host, port=port, debug=False, use_reloader=False, threaded=True)


def _page(title: str, body: str) -> str:
    return (
        "<!doctype html><html lang=\"en\"><head><meta charset=\"utf-8\">"
        "<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">"
        f"<title>{html.escape(title)}</title><style>{CSS}</style></head>"
        f"<body>{body}</body></html>"
    )


def _kv_table(rows: list[tuple[str, Any]]) -> str:
    return "<table>" + "".join(
        f"<tr><th>{html.escape(str(key))}</th><td><code>{html.escape(str(value))}</code></td></tr>"
        for key, value in rows
    ) + "</table>"


def _rows_table(headers: list[str], rows: list[list[Any]]) -> str:
    head = "".join(f"<th>{html.escape(item)}</th>" for item in headers)
    body = "".join(
        "<tr>" + "".join(f"<td>{html.escape(str(value))}</td>" for value in row) + "</tr>"
        for row in rows
    )
    return f"<table><tr>{head}</tr>{body}</table>"


def _report_response(report: dict[str, Any] | None, field: str, mimetype: str) -> Response:
    if report is None:
        abort(404)
    return Response(report[field], mimetype=mimetype)


def _compact_json(text: str) -> str:
    try:
        value = json.loads(text)
    except (json.JSONDecodeError, TypeError):
        return str(text)
    return json.dumps(value, ensure_ascii=False, sort_keys=True)
