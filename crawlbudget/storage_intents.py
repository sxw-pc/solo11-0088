"""Dispatch-intent ledger mixed into the SQLite storage layer."""

from __future__ import annotations

import json
from typing import Any

from .models import DispatchIntent
from .util import canonical_json


class IntentStore:
    """Pre-dispatch intent records; expects ``self.conn`` from the host class."""

    conn: Any

    def record_intent(self, intent: DispatchIntent) -> tuple[dict[str, Any], bool]:
        """Persist a pre-dispatch intent; returns (row, identity_conflict).

        The identity is stable: the same (run, site, url hash, attempt, lease,
        job) always yields the same intent id, so re-dispatch after a
        definitive not-received verdict reuses the row. A row for the same
        attempt key under a different identity signals an ownership conflict.
        """
        existing = self.conn.execute(
            """SELECT * FROM dispatch_intents
               WHERE run_id=? AND site_id=? AND url_hash=? AND attempt=?""",
            (intent.run_id, intent.site_id, intent.url_hash, intent.attempt),
        ).fetchone()
        if existing is not None:
            row = self._intent_row(existing)
            if row["intent_id"] != intent.intent_id:
                return row, True
            if row["state"] == "voided":
                self.conn.execute(
                    """UPDATE dispatch_intents
                       SET state='dispatched', resolution='', resolved_at=''
                       WHERE intent_id=?""",
                    (intent.intent_id,),
                )
                self.conn.commit()
                row.update({"state": "dispatched", "resolution": "", "resolved_at": ""})
            return row, False
        self.conn.execute(
            """INSERT INTO dispatch_intents
               (intent_id, run_id, site_id, job_id, url_hash, attempt, lease_id,
                url, state, resolution, receipt_json, created_at, resolved_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (intent.intent_id, intent.run_id, intent.site_id, intent.job_id,
             intent.url_hash, intent.attempt, intent.lease_id, intent.url,
             intent.state, intent.resolution, canonical_json(intent.receipt),
             intent.created_at, intent.resolved_at),
        )
        self.conn.commit()
        row = self.get_intent(
            intent.run_id, intent.site_id, intent.url_hash, intent.attempt
        )
        assert row is not None
        return row, False

    def get_intent(
        self, run_id: str, site_id: str, url_hash: str, attempt: int
    ) -> dict[str, Any] | None:
        row = self.conn.execute(
            """SELECT * FROM dispatch_intents
               WHERE run_id=? AND site_id=? AND url_hash=? AND attempt=?""",
            (run_id, site_id, url_hash, attempt),
        ).fetchone()
        return self._intent_row(row) if row else None

    def get_intents(
        self, run_id: str, *, state: str | None = None
    ) -> list[dict[str, Any]]:
        sql = "SELECT * FROM dispatch_intents WHERE run_id=?"
        params: tuple[Any, ...] = (run_id,)
        if state:
            sql += " AND state=?"
            params += (state,)
        return [self._intent_row(row)
                for row in self.conn.execute(sql + " ORDER BY rowid", params)]

    def resolve_intent(
        self, intent_id: str, *, state: str, resolution: str,
        receipt: dict[str, Any] | None = None, resolved_at: str = "",
    ) -> None:
        self.conn.execute(
            """UPDATE dispatch_intents
               SET state=?, resolution=?, receipt_json=?, resolved_at=?
               WHERE intent_id=?""",
            (state, resolution, canonical_json(receipt or {}), resolved_at,
             intent_id),
        )
        self.conn.commit()

    @staticmethod
    def _intent_row(row: Any) -> dict[str, Any]:
        data = dict(row)
        try:
            data["receipt"] = json.loads(data.get("receipt_json") or "{}")
        except json.JSONDecodeError:
            data["receipt"] = {}
        return data
