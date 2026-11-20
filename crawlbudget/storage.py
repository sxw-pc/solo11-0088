"""SQLite ledger with idempotent, resumable run records."""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

from .models import (
    BlockedEvent,
    PlannedRequest,
    RequestEvent,
    RunRecord,
    WarningRecord,
)
from .storage_intents import IntentStore
from .storage_leases import LeaseStore
from .storage_schema import SCHEMA
from .util import canonical_json




class Storage(IntentStore, LeaseStore):
    def __init__(self, path: str | Path) -> None:
        self.path = str(path)
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(self.path, timeout=15)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")
        self.conn.execute("PRAGMA journal_mode = WAL" if self.path != ":memory:" else "PRAGMA journal_mode = MEMORY")
        self.initialize()

    def _all(self, sql: str, params: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
        return [dict(row) for row in self.conn.execute(sql, params).fetchall()]

    def initialize(self) -> None:
        self.conn.executescript(SCHEMA)
        self._migrate()
        self.conn.commit()

    def _migrate(self) -> None:
        columns = {
            row["name"]
            for row in self.conn.execute("PRAGMA table_info(lease_accounting)")
        }
        if columns and "quarantined" not in columns:
            self.conn.execute(
                "ALTER TABLE lease_accounting "
                "ADD COLUMN quarantined INTEGER NOT NULL DEFAULT 0"
            )

    def reset(self) -> None:
        tables = ("run_reports", "warnings", "blocked_events", "request_events",
                  "planned_requests", "dispatch_intents", "lease_accounting",
                  "lease_actions", "site_leases", "runs")
        for table in tables:
            self.conn.execute(f"DROP TABLE IF EXISTS {table}")
        self.conn.commit()
        self.initialize()

    def close(self) -> None:
        self.conn.close()

    def __enter__(self) -> "Storage":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def create_run(self, run: RunRecord) -> None:
        self.conn.execute(
            """INSERT INTO runs VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(run_id) DO UPDATE SET
                 finished_at=excluded.finished_at, status=excluded.status,
                 config_fingerprint=excluded.config_fingerprint,
                 seeds_fingerprint=excluded.seeds_fingerprint,
                 robots_fingerprint=excluded.robots_fingerprint""",
            (run.run_id, run.created_at, run.started_at, run.finished_at, run.virtual_date,
             int(run.dry_run), run.status, run.config_fingerprint, run.seeds_fingerprint,
             run.robots_fingerprint, run.report_fingerprint),
        )
        self.conn.commit()

    def update_run(self, run_id: str, **fields: Any) -> None:
        allowed = {"finished_at", "status", "report_fingerprint"}
        invalid = set(fields) - allowed
        if invalid:
            raise ValueError(f"cannot update run fields: {sorted(invalid)}")
        assignments = ", ".join(f"{key}=?" for key in fields)
        self.conn.execute(
            f"UPDATE runs SET {assignments} WHERE run_id=?",
            (*fields.values(), run_id),
        )
        self.conn.commit()

    def get_run(self, run_id: str) -> dict[str, Any] | None:
        row = self.conn.execute("SELECT * FROM runs WHERE run_id=?", (run_id,)).fetchone()
        return dict(row) if row else None

    def list_runs(self) -> list[dict[str, Any]]:
        return self._all("SELECT * FROM runs ORDER BY created_at DESC, run_id ASC")

    def clear_plan_blocks(self, run_id: str) -> None:
        self.conn.execute(
            "DELETE FROM blocked_events WHERE run_id=? AND stage='plan'", (run_id,)
        )
        self.conn.commit()

    def insert_planned(self, request: PlannedRequest) -> None:
        self.conn.execute(
            """INSERT INTO planned_requests
               (run_id, site_id, job_id, request_order, url, url_hash, lease_id,
                allowed, block_reason, block_evidence_json, virtual_offset_ms,
                robots_rule_id, robots_evidence_json, seeded_url, completed,
                final_outcome, attempt)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(run_id, site_id, url_hash, attempt) DO UPDATE SET
                 job_id=excluded.job_id, request_order=excluded.request_order,
                 url=excluded.url, lease_id=excluded.lease_id,
                 allowed=excluded.allowed, block_reason=excluded.block_reason,
                 block_evidence_json=excluded.block_evidence_json,
                 virtual_offset_ms=excluded.virtual_offset_ms,
                 robots_rule_id=excluded.robots_rule_id,
                 robots_evidence_json=excluded.robots_evidence_json""",
            (request.run_id, request.site_id, request.job_id, request.request_order,
             request.url, request.url_hash, request.lease_id, int(request.allowed),
             request.block_reason, canonical_json(request.block_evidence),
             request.virtual_offset_ms, request.robots_rule_id,
             canonical_json(request.robots_evidence), request.seeded_url,
             int(request.completed), request.final_outcome, request.attempt),
        )
        self.conn.commit()

    def get_planned(self, run_id: str) -> list[dict[str, Any]]:
        return self._all(
            "SELECT * FROM planned_requests WHERE run_id=? ORDER BY request_order", (run_id,)
        )

    def complete_planned(
        self, run_id: str, site_id: str, url_hash: str, final_outcome: str
    ) -> bool:
        row = self.conn.execute(
            """SELECT lease_id FROM planned_requests
               WHERE run_id=? AND site_id=? AND url_hash=? AND attempt=1""",
            (run_id, site_id, url_hash),
        ).fetchone()
        if row is None:
            return False
        cur = self.conn.execute(
            """UPDATE planned_requests SET completed=1, final_outcome=?
               WHERE run_id=? AND site_id=? AND url_hash=? AND attempt=1 AND completed=0""",
            (final_outcome, run_id, site_id, url_hash),
        )
        if cur.rowcount:
            self.conn.execute(
                """UPDATE site_leases
                   SET used_requests=used_requests+1,
                       remaining_requests=max(0, granted_requests-(used_requests+1))
                   WHERE run_id=? AND lease_id=?""",
                (run_id, row["lease_id"]),
            )
        self.conn.commit()
        return bool(cur.rowcount)

    def insert_request_event(self, event: RequestEvent) -> bool:
        cur = self.conn.execute(
            """INSERT OR IGNORE INTO request_events
               (run_id, site_id, job_id, url_hash, attempt, url, status_code,
                outcome, elapsed_ms, response_bytes, content_type, body_sha256,
                error_code, evidence_json, recorded_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (event.run_id, event.site_id, event.job_id, event.url_hash, event.attempt,
             event.url, event.status_code, event.outcome, event.elapsed_ms,
             event.response_bytes, event.content_type, event.body_sha256,
             event.error_code, canonical_json(event.evidence), event.recorded_at),
        )
        self.conn.commit()
        return bool(cur.rowcount)

    def get_request_events(
        self, run_id: str, *, site_id: str | None = None, url_hash: str | None = None
    ) -> list[dict[str, Any]]:
        clauses = ["run_id=?"]
        params: list[Any] = [run_id]
        if site_id:
            clauses.append("site_id=?")
            params.append(site_id)
        if url_hash:
            clauses.append("url_hash=?")
            params.append(url_hash)
        return self._all(
            "SELECT * FROM request_events WHERE " + " AND ".join(clauses)
            + " ORDER BY event_id", tuple(params),
        )

    def attempt_count(self, run_id: str, site_id: str | None = None) -> int:
        sql = "SELECT COUNT(*) AS n FROM request_events WHERE run_id=?"
        params: tuple[Any, ...] = (run_id,)
        if site_id:
            sql += " AND site_id=?"
            params += (site_id,)
        return int(self.conn.execute(sql, params).fetchone()["n"])

    def daily_quota_used(
        self, site_id: str, day: str, *, exclude_run_id: str | None = None
    ) -> int:
        sql = """SELECT COUNT(*) AS n FROM request_events e
                 JOIN runs r ON r.run_id=e.run_id
                 WHERE e.site_id=? AND r.virtual_date=?"""
        params: list[Any] = [site_id, day]
        if exclude_run_id:
            sql += " AND e.run_id<>?"
            params.append(exclude_run_id)
        row = self.conn.execute(sql, params).fetchone()
        return int(row["n"])

    def insert_blocked(self, event: BlockedEvent) -> None:
        self.conn.execute(
            """INSERT OR IGNORE INTO blocked_events
               (run_id, site_id, job_id, url_hash, url, stage, reason,
                evidence_json, recorded_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (event.run_id, event.site_id, event.job_id, event.url_hash, event.url,
             event.stage, event.reason, canonical_json(event.evidence), event.recorded_at),
        )
        self.conn.commit()

    def get_blocked(self, run_id: str) -> list[dict[str, Any]]:
        return self._all(
            "SELECT * FROM blocked_events WHERE run_id=? ORDER BY event_id", (run_id,)
        )

    def add_warning(self, warning: WarningRecord) -> None:
        self.conn.execute(
            """INSERT OR IGNORE INTO warnings
               (run_id, code, message, context_json, recorded_at)
               VALUES (?, ?, ?, ?, ?)""",
            (warning.run_id, warning.code, warning.message, canonical_json(warning.context),
             warning.recorded_at),
        )
        self.conn.commit()

    def get_warnings(self, run_id: str) -> list[dict[str, Any]]:
        return self._all("SELECT * FROM warnings WHERE run_id=? ORDER BY warning_id", (run_id,))

    def save_report(
        self, run_id: str, summary_json: str, summary_md: str, summary_html: str,
        report_fingerprint: str, created_at: str,
    ) -> None:
        self.conn.execute(
            """INSERT INTO run_reports VALUES (?, ?, ?, ?, ?, ?)
               ON CONFLICT(run_id) DO UPDATE SET
                 summary_json=excluded.summary_json, summary_md=excluded.summary_md,
                 summary_html=excluded.summary_html,
                 report_fingerprint=excluded.report_fingerprint,
                 created_at=excluded.created_at""",
            (run_id, summary_json, summary_md, summary_html, report_fingerprint, created_at),
        )
        self.conn.execute(
            "UPDATE runs SET report_fingerprint=? WHERE run_id=?",
            (report_fingerprint, run_id),
        )
        self.conn.commit()

    def get_report(self, run_id: str) -> dict[str, Any] | None:
        row = self.conn.execute(
            "SELECT * FROM run_reports WHERE run_id=?", (run_id,)
        ).fetchone()
        return dict(row) if row else None

    def count_rows(self, table: str, run_id: str) -> int:
        allowed = {
            "planned_requests", "request_events", "blocked_events", "warnings",
            "site_leases", "dispatch_intents"
        }
        if table not in allowed:
            raise ValueError("unsupported table")
        row = self.conn.execute(
            f"SELECT COUNT(*) AS n FROM {table} WHERE run_id=?", (run_id,)
        ).fetchone()
        return int(row["n"])
