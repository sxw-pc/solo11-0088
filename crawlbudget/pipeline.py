"""End-to-end orchestration for demo, plan, and controlled run commands."""

from __future__ import annotations

import shutil
from collections import Counter
from dataclasses import dataclass, field
from datetime import timedelta
from pathlib import Path
from typing import Any

from .config import load_config
from .demo_site import start_mock_servers, stop_mock_servers
from .executor import ControlledExecutor
from .lease_lifecycle import replay_actions, refresh_accounting
from .models import BlockedEvent, Plan, RunRecord, WarningRecord
from .planner import build_plan
from .report import build_summary, persist_report
from .storage import Storage
from .util import iso_z, parse_utc, virtual_date

DEFAULT_VIRTUAL_START = "2026-01-15T00:00:00Z"


@dataclass
class ExecutionResult:
    run_id: str
    plan: Plan
    summary: dict[str, Any]
    report_paths: dict[str, Path]
    status: str
    executor_stats: dict[str, int] = field(default_factory=dict)


def reset_state(storage: Storage, reports_dir: str | Path, *, project_root: Path) -> None:
    storage.reset()
    target = Path(reports_dir).resolve()
    if target in {Path.cwd().resolve(), project_root.resolve()}:
        raise ValueError("refusing to reset the project root as a reports directory")
    if target.exists():
        for child in target.iterdir():
            if child.is_dir():
                shutil.rmtree(child)
            else:
                child.unlink()
        target.mkdir(parents=True, exist_ok=True)


def run_pipeline(
    config,
    storage: Storage,
    run_id: str,
    *,
    virtual_start: str = DEFAULT_VIRTUAL_START,
    reports_dir: str | Path = "var/reports",
    dry_run: bool = False,
    session: Any | None = None,
    sleeper=None,
    receipt_contract: str = "audit",
    receipt_verifier: Any | None = None,
) -> ExecutionResult:
    existing = storage.get_run(run_id)
    effective_start = existing["started_at"] if existing else virtual_start
    day = virtual_date(parse_utc(effective_start))
    prior = {
        site_id: storage.daily_quota_used(site_id, day, exclude_run_id=run_id)
        for site_id in sorted(config.sites)
    }
    plan = build_plan(config, run_id, effective_start, prior)
    completed_hashes = {
        row["url_hash"] for row in storage.get_planned(run_id) if row["completed"]
    }
    replay_actions(plan, storage.get_lease_actions(run_id), completed_hashes)
    planned_finish = _planned_finish(plan)
    if existing:
        _assert_same_fingerprints(existing, config)
    created_at = existing["created_at"] if existing else effective_start
    status = "dry_run" if dry_run else "running"
    run = RunRecord(
        run_id=run_id,
        created_at=created_at,
        started_at=effective_start,
        finished_at=planned_finish,
        virtual_date=day,
        dry_run=dry_run,
        status=status,
        config_fingerprint=config.config_fingerprint,
        seeds_fingerprint=config.seeds_fingerprint,
        robots_fingerprint=config.robots_fingerprint,
    )
    storage.create_run(run)
    for lease in plan.leases:
        storage.upsert_lease(lease)
    storage.clear_plan_blocks(run_id)
    for request in plan.requests:
        storage.insert_planned(request)
        if not request.allowed:
            storage.insert_blocked(BlockedEvent(
                run_id=run_id, site_id=request.site_id, job_id=request.job_id,
                url_hash=request.url_hash, url=request.url, stage="plan",
                reason=request.block_reason or "unknown_block",
                evidence=request.block_evidence, recorded_at=effective_start,
            ))
    for warning in plan.warnings:
        storage.add_warning(WarningRecord(
            run_id=run_id, code=str(warning.get("code", "WARNING")),
            message=str(warning.get("message", "")),
            context=dict(warning.get("context", {})), recorded_at=effective_start,
        ))
    stats: dict[str, int] = {}
    if not dry_run:
        executor = ControlledExecutor(
            storage, config.sites, plan, session=session, sleeper=sleeper,
            receipt_contract=receipt_contract, receipt_verifier=receipt_verifier,
        )
        stats = executor.run()
        granted = sum(1 for request in plan.requests if request.allowed)
        completed = sum(
            1 for row in storage.get_planned(run_id) if row["allowed"] and row["completed"]
        )
        final_status = "completed" if completed >= granted else "partial"
    else:
        final_status = "dry_run"
    finished = planned_finish if not dry_run else effective_start
    storage.update_run(run_id, status=final_status, finished_at=finished)
    refresh_accounting(storage, plan, settled_at=finished)
    run_row = storage.get_run(run_id)
    assert run_row is not None
    summary = build_summary(config, plan, storage, run_row)
    paths = persist_report(
        storage, reports_dir, run_id, summary, created_at=effective_start
    )
    storage.update_run(run_id, report_fingerprint=summary["fingerprints"]["report"])
    return ExecutionResult(
        run_id=run_id,
        plan=plan,
        summary=summary,
        report_paths=paths,
        status=final_status,
        executor_stats=stats,
    )


def run_demo(
    config_path: str | Path,
    *,
    project_root: str | Path,
    db_path: str | Path,
    reports_dir: str | Path,
    pages_dir: str | Path,
    manifest_path: str | Path,
    run_id: str = "demo-run",
    virtual_start: str = DEFAULT_VIRTUAL_START,
    reset: bool = False,
    dry_run: bool = False,
    session: Any | None = None,
    sleeper=None,
    receipt_contract: str = "audit",
) -> ExecutionResult:
    root = Path(project_root).resolve()
    config = load_config(config_path, project_root=root)
    with Storage(db_path) as storage:
        if reset:
            reset_state(storage, reports_dir, project_root=root)
        servers = []
        try:
            if session is None and not dry_run:
                servers = start_mock_servers(config, pages_dir, manifest_path)
            return run_pipeline(
                config,
                storage,
                run_id,
                virtual_start=virtual_start,
                reports_dir=reports_dir,
                dry_run=dry_run,
                session=session,
                sleeper=sleeper,
                receipt_contract=receipt_contract,
            )
        finally:
            stop_mock_servers(servers)


def inspect_run(storage: Storage, run_id: str) -> dict[str, Any]:
    run = storage.get_run(run_id)
    if run is None:
        return {}
    events = storage.get_request_events(run_id)
    blocked = storage.get_blocked(run_id)
    reasons = Counter(event["reason"] for event in blocked)
    return {
        "run": run,
        "leases": storage.get_leases(run_id),
        "planned_requests": storage.count_rows("planned_requests", run_id),
        "request_events": len(events),
        "blocked_events": len(blocked),
        "warnings": storage.count_rows("warnings", run_id),
        "dispatch_intents": storage.count_rows("dispatch_intents", run_id),
        "blocked_reasons": dict(sorted(reasons.items())),
        "report_available": storage.get_report(run_id) is not None,
    }


def _planned_finish(plan: Plan) -> str:
    offsets = [request.virtual_offset_ms or 0 for request in plan.requests if request.allowed]
    offset = max(offsets, default=0) + 1000
    return iso_z(parse_utc(plan.virtual_start) + timedelta(milliseconds=offset))


def _assert_same_fingerprints(existing: dict[str, Any], config) -> None:
    if (
        existing["config_fingerprint"] != config.config_fingerprint
        or existing["seeds_fingerprint"] != config.seeds_fingerprint
        or existing["robots_fingerprint"] != config.robots_fingerprint
    ):
        raise ValueError(
            "run_id was previously used with different config/seed/robots fingerprints; "
            "choose a new run id"
        )
