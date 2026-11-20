"""Command-line interface for crawlbudget."""

from __future__ import annotations

import argparse
import json
import logging
import sys
from dataclasses import asdict
from pathlib import Path
from typing import Any

from .budget import quota_available
from .config import load_config
from .demo_site import start_mock_servers, stop_mock_servers
from .errors import (
    CLI_UNEXPECTED,
    STORE_NOT_FOUND,
    CliError,
    ConfigError,
    CrawlBudgetError,
    StorageError,
)
from .pipeline import (
    DEFAULT_VIRTUAL_START,
    inspect_run,
    reset_state,
    run_demo,
    run_pipeline,
)
from .planner import build_plan
from .golden import check_golden_from_storage
from .representation_cli import capture_command, representation_command
from .review_server import serve_review
from .storage import Storage
from .util import parse_utc, virtual_date

LOG = logging.getLogger("crawlbudget")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m crawlbudget",
        description="Local-only crawl budget planning, guarded execution, and review.",
    )
    parser.add_argument("--verbose", action="store_true", help="enable verbose logging")
    sub = parser.add_subparsers(dest="command", required=True)

    demo = sub.add_parser("demo", help="start mock sites and run the full demo")
    _common(demo)
    demo.add_argument("--reset", action="store_true", help="reset ledger and reports first")
    demo.add_argument("--run-id", default="demo-run")
    demo.add_argument("--virtual-start", default=DEFAULT_VIRTUAL_START)
    demo.add_argument("--dry-run", action="store_true", help="plan only; no HTTP requests")

    run = sub.add_parser("run", help="plan and execute against registered local mock sites")
    _common(run)
    run.add_argument("--run-id", default="run-local")
    run.add_argument("--virtual-start", default=DEFAULT_VIRTUAL_START)
    run.add_argument("--dry-run", action="store_true")
    run.add_argument("--no-mock", action="store_true", help="do not start built-in mock sites")

    plan = sub.add_parser("plan", help="print a deterministic plan without executing")
    _common(plan)
    plan.add_argument("--run-id", default="plan-preview")
    plan.add_argument("--virtual-start", default=DEFAULT_VIRTUAL_START)

    serve = sub.add_parser("serve", help="start the read-only review console")
    serve.add_argument("--db", default="var/crawlbudget.sqlite3")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8765)
    serve.add_argument("--project-root", default=".")

    inspect = sub.add_parser("inspect-run", help="print stored run summary as JSON")
    inspect.add_argument("--db", default="var/crawlbudget.sqlite3")
    inspect.add_argument("--run-id", help="run id; defaults to latest run")
    inspect.add_argument("--project-root", default=".")

    golden = sub.add_parser("check-golden", help="compare a run report with golden fixtures")
    golden.add_argument("--db", default="var/crawlbudget.sqlite3")
    golden.add_argument("--run-id", help="run id; defaults to latest run")
    golden.add_argument("--expected-dir", default="fixtures/expected")
    golden.add_argument("--project-root", default=".")

    capture = sub.add_parser(
        "capture", help="offline conditional GET against a registered loopback site",
    )
    _common(capture)
    capture.add_argument("--run-id", default="capture-local")
    capture.add_argument("--site", required=True, help="registered site_id")
    capture.add_argument("--url", required=True, help="in-scope loopback URL")
    capture.add_argument(
        "--header", action="append", default=[], metavar="Name: Value",
        help="request header; only Accept-Language and If-None-Match are allowed",
    )
    capture.add_argument("--bodies-dir", default="var/bodies")
    capture.add_argument("--recorded-at", default=DEFAULT_VIRTUAL_START)
    capture.add_argument("--no-mock", action="store_true")

    attest = sub.add_parser(
        "representation",
        help="recompute offline representation attestations from the ledger",
    )
    attest.add_argument("--db", default="var/crawlbudget.sqlite3")
    attest.add_argument("--run-id", required=True)
    attest.add_argument("--bodies-dir", action="append", default=[])
    attest.add_argument("--out", help="write the JSON report here instead of stdout")
    attest.add_argument("--project-root", default=".")
    return parser


def _common(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--config", default="fixtures/demo_sites.yaml")
    parser.add_argument("--db", default="var/crawlbudget.sqlite3")
    parser.add_argument("--reports-dir", default="var/reports")
    parser.add_argument("--pages-dir", default="fixtures/pages")
    parser.add_argument("--manifest", default="fixtures/pages/manifest.yaml")
    parser.add_argument("--project-root", default=".")


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.WARNING,
        format="%(levelname)s %(name)s: %(message)s",
    )
    logging.getLogger("werkzeug").setLevel(logging.WARNING)
    try:
        return _dispatch(args)
    except ConfigError as exc:
        for issue in exc.issues:
            print(f"{issue.code} {issue.path}: {issue.message}", file=sys.stderr)
        return exc.exit_code
    except CrawlBudgetError as exc:
        print(f"{exc.code}: {exc.message}", file=sys.stderr)
        if exc.details:
            print(json.dumps(exc.details, ensure_ascii=False, sort_keys=True), file=sys.stderr)
        return exc.exit_code
    except KeyboardInterrupt:
        print("interrupted", file=sys.stderr)
        return 130
    except Exception as exc:  # pragma: no cover - defensive CLI boundary
        if args.verbose:
            LOG.exception("unexpected command failure")
        print(f"{CLI_UNEXPECTED}: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1


def _dispatch(args: argparse.Namespace) -> int:
    root = Path(args.project_root).resolve()
    if args.command in {"demo", "run"}:
        config_path = _path(root, args.config)
        db_path = _path(root, args.db)
        reports = _path(root, args.reports_dir)
        pages = _path(root, args.pages_dir)
        manifest = _path(root, args.manifest)
        if args.command == "demo":
            result = run_demo(
                config_path,
                project_root=root,
                db_path=db_path,
                reports_dir=reports,
                pages_dir=pages,
                manifest_path=manifest,
                run_id=args.run_id,
                virtual_start=args.virtual_start,
                reset=args.reset,
                dry_run=args.dry_run,
            )
        else:
            result = _run_command(args, config_path, db_path, reports, pages, manifest, root)
        _print_run_result(result)
        return 0
    if args.command == "plan":
        return _plan_command(args, root)
    if args.command == "inspect-run":
        return _inspect_command(args, root)
    if args.command == "check-golden":
        return _golden_command(args, root)
    if args.command == "capture":
        config = load_config(_path(root, args.config), project_root=root)
        return capture_command(args, config, root)
    if args.command == "representation":
        return representation_command(args, root)
    db_path = _path(root, args.db)
    Storage(db_path).close()
    try:
        serve_review(db_path, host=args.host, port=args.port)
    except ValueError as exc:
        raise CliError(str(exc), details={"host": args.host, "port": args.port}) from exc
    return 0


def _run_command(args, config_path, db_path, reports, pages, manifest, root) -> Any:
    config = load_config(config_path, project_root=root)
    with Storage(db_path) as storage:
        servers = []
        try:
            if not args.no_mock and not args.dry_run:
                servers = start_mock_servers(config, pages, manifest)
            return run_pipeline(
                config,
                storage,
                args.run_id,
                virtual_start=args.virtual_start,
                reports_dir=reports,
                dry_run=args.dry_run,
            )
        finally:
            stop_mock_servers(servers)


def _plan_command(args: argparse.Namespace, root: Path) -> int:
    config = load_config(_path(root, args.config), project_root=root)
    db_path = _path(root, args.db)
    with Storage(db_path) as storage:
        day = virtual_date(parse_utc(args.virtual_start))
        prior = {site_id: storage.daily_quota_used(
            site_id, day, exclude_run_id=args.run_id
        ) for site_id in sorted(config.sites)}
        plan = build_plan(config, args.run_id, args.virtual_start, prior)
        payload = {
            "run_id": plan.run_id, "virtual_start": plan.virtual_start,
            "quota_used_before": plan.quota_used_before,
            "quota_available": {site_id: quota_available(
                config.sites[site_id].daily_quota, plan.quota_used_before[site_id]
            ) for site_id in sorted(config.sites)},
            "attempt_limits": plan.attempt_limits,
            "leases": [asdict(lease) for lease in plan.leases],
            "requests": [request.as_dict() for request in plan.requests],
            "warnings": plan.warnings,
        }
    sys.stdout.write(json.dumps(payload, ensure_ascii=False, indent=2) + "\n")
    return 0


def _inspect_command(args: argparse.Namespace, root: Path) -> int:
    with Storage(_path(root, args.db)) as storage:
        run_id = args.run_id or _latest_run_id(storage)
        payload = inspect_run(storage, run_id)
        if not payload:
            raise StorageError("run not found", details={"run_id": run_id, "code": STORE_NOT_FOUND})
    print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


def _golden_command(args: argparse.Namespace, root: Path) -> int:
    with Storage(_path(root, args.db)) as storage:
        run_id = args.run_id or _latest_run_id(storage)
        differences = check_golden_from_storage(
            storage, run_id, _path(root, args.expected_dir)
        )
    if differences:
        for diff in differences:
            print(f"GOLDEN_MISMATCH {diff['path']} "
                  f"expected={json.dumps(diff['expected'], ensure_ascii=False)} "
                  f"actual={json.dumps(diff['actual'], ensure_ascii=False)}",
                  file=sys.stderr)
        return 3
    print(f"GOLDEN_OK {run_id}")
    return 0


def _latest_run_id(storage: Storage) -> str:
    runs = storage.list_runs()
    if not runs:
        raise StorageError("no runs found", details={"code": STORE_NOT_FOUND})
    return str(runs[0]["run_id"])


def _print_run_result(result: Any) -> None:
    totals = result.summary["totals"]
    print(f"run: {result.run_id} status: {result.status}")
    print("planned={planned_requests} granted={granted_requests} "
          "executed={executed_requests} attempts={http_attempts} "
          "blocked={blocked_events}".format(**totals))
    print("reports: {} | {} | {}".format(
        result.report_paths["json"], result.report_paths["markdown"],
        result.report_paths["html"],
    ))


def _path(root: Path, value: str | Path) -> Path:
    candidate = Path(value)
    return candidate if candidate.is_absolute() else (root / candidate)
