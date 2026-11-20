"""Handlers for the offline capture and representation CLI subcommands."""

from __future__ import annotations

import argparse
from pathlib import Path

from .conditional_capture import ALLOWED_REQUEST_HEADERS, capture_and_record
from .errors import CliError
from .models import RunRecord
from .representation import build_representation_report, dump_representation_json
from .storage import Storage
from .util import iso_z, parse_utc, virtual_date


def parse_header_args(raw_headers: list[str]) -> dict[str, str]:
    headers: dict[str, str] = {}
    for raw in raw_headers:
        if ":" not in raw:
            raise CliError(
                "header must be 'Name: Value'", details={"header": raw}
            )
        name, value = raw.split(":", 1)
        name = name.strip()
        if name not in ALLOWED_REQUEST_HEADERS:
            raise CliError(
                "unsupported conditional request header",
                details={"header": name,
                         "allowed": sorted(ALLOWED_REQUEST_HEADERS)},
            )
        if name in headers:
            raise CliError("duplicate request header", details={"header": name})
        headers[name] = value.strip()
    return headers


def ensure_run_row(storage: Storage, run_id: str, recorded_at: str) -> None:
    if storage.get_run(run_id) is not None:
        return
    stamp = iso_z(parse_utc(recorded_at))
    storage.create_run(RunRecord(
        run_id=run_id, created_at=stamp, started_at=stamp, finished_at=stamp,
        virtual_date=virtual_date(parse_utc(recorded_at)), dry_run=False,
        status="capture", config_fingerprint="", seeds_fingerprint="",
        robots_fingerprint="",
    ))


def capture_command(args: argparse.Namespace, config, root: Path) -> int:
    site = config.sites.get(args.site)
    if site is None:
        raise CliError("unknown site id", details={"site_id": args.site})
    headers = parse_header_args(args.header)
    bodies = _resolve(root, args.bodies_dir)
    with Storage(_resolve(root, args.db)) as storage:
        ensure_run_row(storage, args.run_id, args.recorded_at)
        receipt = capture_and_record(
            site, args.url, headers, bodies, storage, args.run_id,
            args.recorded_at,
        )
    payload = {
        "run_id": args.run_id, "site_id": site.site_id, "url": receipt.url,
        "status": receipt.status, "etag": receipt.etag, "vary": receipt.vary,
        "request_headers": list(receipt.request_headers),
        "body_sha256": receipt.body_sha256, "body_path": receipt.body_path,
    }
    print(dump_line(payload))
    return 0


def representation_command(args: argparse.Namespace, root: Path) -> int:
    body_dirs = [_resolve(root, value) for value in args.bodies_dir]
    with Storage(_resolve(root, args.db)) as storage:
        if storage.get_run(args.run_id) is None:
            raise CliError("run not found",
                           details={"run_id": args.run_id})
        report = build_representation_report(
            storage, args.run_id, body_dirs=body_dirs
        )
        text = dump_representation_json(report)
    if args.out:
        target = _resolve(root, args.out)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8", newline="\n")
        print(f"REPRESENTATION_OK {args.run_id} {target}")
    else:
        print(text, end="")
    return 0


def _resolve(root: Path, value: str) -> Path:
    candidate = Path(value)
    return candidate if candidate.is_absolute() else (root / candidate)


def dump_line(payload: dict) -> str:
    import json

    return json.dumps(payload, ensure_ascii=False, sort_keys=True)
