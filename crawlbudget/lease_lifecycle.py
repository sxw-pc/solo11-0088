"""Pre-dispatch lease changes and their deterministic accounting projection."""

from __future__ import annotations

from collections import Counter
from typing import Any

from .budget import quota_available
from .errors import BUDGET_QUOTA_EXCEEDED, BUDGET_RUN_LIMIT_EXCEEDED
from .models import Plan, PlannedRequest
from .planner import build_plan
from .storage import Storage
from .util import parse_utc, virtual_date


QUOTA_BLOCKS = {BUDGET_QUOTA_EXCEEDED, BUDGET_RUN_LIMIT_EXCEEDED}


def _request(plan: Plan, url_hash: str) -> PlannedRequest:
    matches = [item for item in plan.requests if item.url_hash == url_hash]
    if len(matches) != 1:
        raise ValueError(f"request hash must identify one planned URL: {url_hash}")
    return matches[0]


def _block(item: PlannedRequest, reason: str, action_id: str) -> None:
    item.allowed = False
    item.block_reason = reason
    item.block_evidence = {"lease_action_id": action_id}
    item.virtual_offset_ms = None


def replay_actions(
    plan: Plan, actions: list[dict[str, str]],
    completed_hashes: set[str] | None = None,
) -> None:
    """Project recorded decisions onto a fresh deterministic plan."""
    leases = {lease.lease_id: lease for lease in plan.leases}
    completed_hashes = completed_hashes or set()
    for action in actions:
        kind = action["kind"]
        at = parse_utc(action["as_of"])
        if at < parse_utc(plan.virtual_start):
            raise ValueError("lease action precedes run start")
        if kind in {"cancel", "transfer"}:
            source = _request(plan, action["source_hash"])
            lease = leases[source.lease_id]
            if not source.allowed or at >= parse_utc(lease.expires_at):
                raise ValueError("source has no live grant at action time")
            offset = source.virtual_offset_ms
            if kind == "transfer":
                target = _request(plan, action["target_hash"])
                if (target.site_id != source.site_id or target.job_id == source.job_id
                        or target.allowed or target.block_reason not in QUOTA_BLOCKS):
                    raise ValueError("target must be a different eligible job on the same site")
                target.allowed = True
                target.block_reason = None
                target.block_evidence = {"lease_action_id": action["action_id"]}
                target.virtual_offset_ms = offset
            _block(source, "lease_transferred" if kind == "transfer" else
                   "lease_cancelled", action["action_id"])
        elif kind == "expire":
            site_id = action["source_hash"]
            site_leases = [lease for lease in plan.leases if lease.site_id == site_id]
            if not site_leases or at < parse_utc(site_leases[0].expires_at):
                raise ValueError("site lease has not expired")
            pending = [item for item in plan.requests if item.site_id == site_id
                       and item.allowed and item.url_hash not in completed_hashes]
            if not pending:
                raise ValueError("site has no unspent live grants to expire")
            for item in pending:
                _block(item, "lease_expired", action["action_id"])
        else:
            raise ValueError(f"unknown lease action: {kind}")

    for lease in plan.leases:
        granted = sum(item.allowed for item in plan.requests
                      if item.lease_id == lease.lease_id)
        lease.granted_requests = int(granted)
        lease.remaining_requests = int(granted)
        if granted == 0 and any(
            item.lease_id == lease.lease_id and item.block_reason == "lease_expired"
            for item in plan.requests
        ):
            lease.reason = "lease_expired"


def record_action(
    config: Any, storage: Storage, run_id: str, *, action_id: str, kind: str,
    source_hash: str, target_hash: str = "", as_of: str, reason: str,
    reports_dir: str = "var/reports",
) -> dict[str, Any]:
    """Record one validated change on a staged run, then refresh its plan."""
    existing = storage.get_run(run_id)
    if (existing is None or not existing["dry_run"]
            or storage.get_request_events(run_id) or storage.get_intents(run_id)):
        raise ValueError("lease actions require a staged run before HTTP dispatch")
    if (existing["config_fingerprint"] != config.config_fingerprint or
            existing["seeds_fingerprint"] != config.seeds_fingerprint or
            existing["robots_fingerprint"] != config.robots_fingerprint):
        raise ValueError("staged run fixtures changed")
    action = {
        "action_id": action_id, "run_id": run_id, "kind": kind,
        "source_hash": source_hash, "target_hash": target_hash,
        "as_of": as_of, "reason": reason,
    }
    prior_action = next((row for row in storage.get_lease_actions(run_id)
                         if row["action_id"] == action_id), None)
    if prior_action is not None:
        if any(prior_action[key] != value for key, value in action.items()):
            raise ValueError("action id was already used for a different decision")
        return prior_action
    day = virtual_date(parse_utc(existing["started_at"]))
    prior = {site_id: storage.daily_quota_used(site_id, day, exclude_run_id=run_id)
             for site_id in config.sites}
    plan = build_plan(config, run_id, existing["started_at"], prior)
    replay_actions(plan, [*storage.get_lease_actions(run_id), action])
    storage.insert_lease_action(action)
    from .pipeline import run_pipeline
    run_pipeline(config, storage, run_id, dry_run=True, reports_dir=reports_dir)
    return action


def refresh_accounting(storage: Storage, plan: Plan, *, settled_at: str) -> None:
    """Store business state beside the raw grant and request ledgers."""
    actions = storage.get_lease_actions(plan.run_id)
    rows = storage.get_planned(plan.run_id)
    leases = storage.get_leases(plan.run_id)
    by_hash = {row["url_hash"]: row for row in rows}
    counts: dict[str, Counter[str]] = {
        lease["lease_id"]: Counter() for lease in leases
    }
    for row in rows:
        if row["block_reason"] == "lease_expired":
            counts[row["lease_id"]]["expired"] += 1
        if row["final_outcome"] == "receipt_quarantined":
            counts[row["lease_id"]]["quarantined"] += 1
    for action in actions:
        if action["kind"] != "expire":
            source = by_hash[action["source_hash"]]
            key = "cancelled" if action["kind"] == "cancel" else "transferred_out"
            counts[source["lease_id"]][key] += 1
            if action["kind"] == "transfer":
                target = by_hash[action["target_hash"]]
                counts[target["lease_id"]]["transferred_in"] += 1
    run = storage.get_run(plan.run_id)
    for lease in leases:
        count = counts[lease["lease_id"]]
        state = ("settled" if run and run["status"] in {"completed", "partial"}
                 else "expired" if count["expired"] else "active")
        storage.upsert_lease_accounting({
            "run_id": plan.run_id, "lease_id": lease["lease_id"],
            "site_id": lease["site_id"], "job_id": lease["job_id"],
            "state": state, "granted": lease["granted_requests"],
            "consumed": lease["used_requests"],
            "available": lease["remaining_requests"],
            "cancelled": count["cancelled"],
            "transferred_in": count["transferred_in"],
            "transferred_out": count["transferred_out"],
            "expired": count["expired"], "quarantined": count["quarantined"],
            "settled_at": settled_at,
        })
