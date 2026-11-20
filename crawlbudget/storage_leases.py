"""Lease and lease-accounting records mixed into the SQLite storage layer."""

from __future__ import annotations

from typing import Any

from .models import Lease


class LeaseStore:
    """Site leases, pre-dispatch lease actions, and accounting projection.

    Expects ``self.conn`` and ``self._all`` from the host class.
    """

    conn: Any

    def upsert_lease(self, lease: Lease) -> None:
        self.conn.execute(
            """INSERT INTO site_leases VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(run_id, lease_id) DO UPDATE SET
                 granted_requests=excluded.granted_requests,
                 remaining_requests=max(0, excluded.granted_requests - site_leases.used_requests),
                 expires_at=excluded.expires_at, reason=excluded.reason""",
            (lease.run_id, lease.lease_id, lease.site_id, lease.job_id,
             lease.granted_requests, lease.used_requests, lease.remaining_requests,
             lease.expires_at, lease.reason),
        )
        self.conn.commit()

    def get_leases(self, run_id: str) -> list[dict[str, Any]]:
        return self._all(
            "SELECT * FROM site_leases WHERE run_id=? ORDER BY site_id, job_id", (run_id,)
        )

    def get_lease_actions(self, run_id: str) -> list[dict[str, Any]]:
        return self._all(
            "SELECT * FROM lease_actions WHERE run_id=? ORDER BY rowid", (run_id,)
        )

    def insert_lease_action(self, action: dict[str, str]) -> None:
        self.conn.execute(
            """INSERT INTO lease_actions
               (action_id, run_id, kind, source_hash, target_hash, as_of, reason)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            tuple(action[key] for key in (
                "action_id", "run_id", "kind", "source_hash", "target_hash",
                "as_of", "reason",
            )),
        )
        self.conn.commit()

    def get_lease_accounting(self, run_id: str) -> list[dict[str, Any]]:
        return self._all(
            "SELECT * FROM lease_accounting WHERE run_id=? ORDER BY site_id, job_id",
            (run_id,),
        )

    def upsert_lease_accounting(self, row: dict[str, Any]) -> None:
        keys = ("run_id", "lease_id", "site_id", "job_id", "state", "granted",
                "consumed", "available", "cancelled", "transferred_in",
                "transferred_out", "expired", "quarantined", "settled_at")
        self.conn.execute(
            """INSERT INTO lease_accounting VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(run_id, lease_id) DO UPDATE SET
                 state=excluded.state, granted=excluded.granted,
                 consumed=excluded.consumed, available=excluded.available,
                 cancelled=excluded.cancelled, transferred_in=excluded.transferred_in,
                 transferred_out=excluded.transferred_out, expired=excluded.expired,
                 quarantined=excluded.quarantined, settled_at=excluded.settled_at""",
            tuple(row[key] for key in keys),
        )
        self.conn.commit()
