"""Subscriber billing CLI: exact shared-session plans and two signed contracts.

Usage::

    python -m crawlbudget.subscriber_billing --fixture fixtures/subscriber_billing/intake.json

This is a downstream settlement step.  It never issues an HTTP GET, never
re-authorizes a request, and never mutates lease usage; it re-verifies executor
receipts that already exist and refuses to bill anything unverified.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from .billing_contracts import settle
from .billing_inputs import (
    ParsedInputs,
    billing_error,
    capacity_value,
    parse_inputs,
)
from .billing_model import CoalitionTable
from .billing_receipts import (
    Receipt,
    expected_urls,
    load_receipts,
    receipts_from_storage,
)
from .billing_verify import verify_receipts
from .config import load_config
from .demo_site import load_page_manifest
from .storage import Storage

DEFAULT_FIXTURE = Path("fixtures/subscriber_billing/intake.json")
DEFAULT_CONFIG = Path("fixtures/demo_sites.yaml")
DEFAULT_MANIFEST = Path("fixtures/pages/manifest.yaml")
DEFAULT_RECEIPTS = Path("fixtures/subscriber_billing/receipts.json")


def solo_quotes(data: dict) -> dict[str, int]:
    """Backward-compatible solo (one subscriber per session) quote draft."""
    depot = data["depot"]
    dist = data["navigation_cost"]
    return {s["subscriber_id"]: dist[depot][s["stop"]] + dist[s["stop"]][depot]
            for s in data["subscribers"]}


def build_report(
    data: dict,
    *,
    project_root: Path,
    config_path: Path | None = None,
    manifest_path: Path | None = None,
    receipts: list[Receipt] | None = None,
    storage: Storage | None = None,
    receipts_path: Path | None = None,
    capacity: int | None = None,
) -> dict[str, Any]:
    """Compute the full shared-session plan, proofs, receipts, and invoices."""
    root = project_root.resolve()
    parsed = parse_inputs(data)
    if capacity is not None:
        parsed = _with_capacity(parsed, capacity)
    config = load_config(config_path or root / DEFAULT_CONFIG, project_root=root)
    manifest = load_page_manifest(manifest_path or root / DEFAULT_MANIFEST)
    urls = expected_urls(parsed, config, manifest)
    if receipts is None:
        if storage is not None:
            receipts = receipts_from_storage(storage, _run_id(data, storage), urls)
        else:
            source = receipts_path or root / DEFAULT_RECEIPTS
            receipts = load_receipts(source) if source.exists() else []
    verification = verify_receipts(
        parsed, config, receipts,
        project_root=root, manifest=manifest, urls=urls,
    )
    table = CoalitionTable(parsed)
    report: dict[str, Any] = {
        "depot": parsed.depot,
        "session_capacity": parsed.capacity,
        "subscribers": [
            {
                "subscriber_id": subscriber.subscriber_id,
                "site_id": subscriber.site_id,
                "stop": subscriber.stop,
                "page_path": subscriber.page_path,
                "body_bytes": subscriber.body_bytes,
                "body_sha256": subscriber.body_sha256,
            }
            for subscriber in parsed.subscribers
        ],
        "solo_quotes": solo_quotes(data),
        "full_plan": table.plan(table.full),
        "subset_costs": table.subset_costs(),
        "subset_proof": _subset_proofs(table),
        "receipts": {
            "source": ("executor storage" if storage is not None
                       else str(receipts_path or DEFAULT_RECEIPTS)),
            "required": True,
            "all_verified": verification["all_verified"],
            "accepted": verification["accepted"],
            "rejected": verification["rejected"],
            "identities": [receipt.identity() for receipt in receipts],
        },
    }
    if not verification["all_verified"]:
        report["settlement"] = {
            "contracts": {}, "refusals": [{
                "contract": "all", "reason": "unverified_delivery",
                "evidence": {"rejected": verification["rejected"]},
            }],
            "all_signed": False,
            "full_cost_cents": table.cost(table.full) * 100,
            "conservation": {},
        }
        report["refused"] = True
        report["refusal_reasons"] = [{
            "scope": "delivery", "code": "unverified_delivery",
            "detail": "one or more executor receipts failed attestation; nothing is billed",
            "rejected": verification["rejected"],
        }]
        return report
    settlement = settle(table, verification["accepted"])
    report["settlement"] = settlement
    report["refused"] = not settlement["all_signed"]
    report["refusal_reasons"] = [
        {"scope": "contract", **item} for item in settlement["refusals"]
    ]
    return report


def _with_capacity(parsed: ParsedInputs, capacity: int) -> ParsedInputs:
    return ParsedInputs(parsed.depot, capacity_value(capacity),
                        parsed.subscribers, parsed.dist)


def _run_id(data: dict, storage: Storage) -> str:
    run_id = data.get("run_id")
    if isinstance(run_id, str) and run_id:
        return run_id
    runs = storage.list_runs()
    if not runs:
        raise billing_error("billing_receipts_missing",
                            "no run id provided and no stored runs exist")
    return str(runs[0]["run_id"])


def _subset_proofs(table: CoalitionTable) -> list[dict[str, Any]]:
    rows = []
    for mask in range(table.size):
        ids = sorted(table.ids_for(mask))
        if table.costs[mask] is None:
            rows.append({"subscribers": ids, "key": "+".join(ids) if ids else "empty",
                         "cost": None, "feasible": False, "sessions": []})
            continue
        rows.append({
            "subscribers": ids,
            "key": "+".join(ids) if ids else "empty",
            "cost": int(table.costs[mask]),
            "feasible": True,
            "sessions": [
                {
                    "subscriber_ids": sorted(table.ids_for(group)),
                    "route": table.route_of(group)[1],
                    "navigation_cost": table.route_of(group)[0],
                }
                for group in table.group_masks(mask)
            ],
        })
    rows.sort(key=lambda row: (len(row["subscribers"]), row["subscribers"]))
    return rows


def _resolve(value: str | Path | None, root: Path, default: Path) -> Path:
    candidate = Path(value) if value is not None else default
    return candidate if candidate.is_absolute() else (root / candidate)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--fixture", type=Path, default=DEFAULT_FIXTURE)
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    parser.add_argument("--receipts", type=Path, default=DEFAULT_RECEIPTS)
    parser.add_argument("--db", type=Path, help="read executor receipts from this ledger")
    parser.add_argument("--run-id", help="run id to read from the ledger")
    parser.add_argument("--capacity", type=int, help="override session capacity")
    parser.add_argument("--project-root", type=Path, default=Path("."))
    parser.add_argument("--json", action="store_true", help="emit the full report")
    args = parser.parse_args(argv)
    root = args.project_root.resolve()
    fixture = _resolve(args.fixture, root, DEFAULT_FIXTURE)
    data = json.loads(fixture.read_text(encoding="utf-8"))
    if args.run_id:
        data = {**data, "run_id": args.run_id}
    storage = None
    try:
        if args.db is not None:
            storage = Storage(_resolve(args.db, root, Path("var/crawlbudget.sqlite3")))
        report = build_report(
            data, project_root=root,
            config_path=_resolve(args.config, root, DEFAULT_CONFIG),
            manifest_path=_resolve(args.manifest, root, DEFAULT_MANIFEST),
            receipts_path=None if storage is not None else _resolve(
                args.receipts, root, DEFAULT_RECEIPTS),
            storage=storage,
            capacity=args.capacity,
        )
    finally:
        if storage is not None:
            storage.close()
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    else:
        _print_summary(report)
    if report["refused"]:
        reasons = json.dumps(report["refusal_reasons"], ensure_ascii=False, sort_keys=True)
        print(f"REFUSED {reasons}", file=sys.stderr)
        return 2
    return 0


def _print_summary(report: dict[str, Any]) -> None:
    full = report["full_plan"]
    print(f"depot={report['depot']} capacity={report['session_capacity']} "
          f"subscribers={full['subscriber_count']}")
    print(f"solo_quotes_total={sum(report['solo_quotes'].values())} "
          f"shared_full_cost={full['total_cost']}")
    for session in full["sessions"]:
        print("session {}: {} route={} cost={} capacity={}".format(
            session["session_index"], ",".join(session["subscriber_ids"]),
            "->".join(session["route"]), session["navigation_cost"],
            session["capacity"],
        ))
    print("subset costs: " + json.dumps(
        {row["key"]: row["cost"] for row in report["subset_proof"]},
        sort_keys=True,
    ))
    receipts = report["receipts"]
    print(f"receipts: verified={receipts['all_verified']} "
          f"accepted={len(receipts['accepted'])} rejected={len(receipts['rejected'])}")
    for name, contract in report["settlement"]["contracts"].items():
        print(f"contract {name}: {contract['status']} total={contract['total_cents'] / 100:.2f}")
        for invoice in contract.get("invoices") or []:
            print("  {} {:.2f} cents={} remainder={:.6f}".format(
                invoice["subscriber_id"], invoice["cents"] / 100,
                invoice["cents"], invoice["rounding_remainder"],
            ))
    for reason in report.get("refusal_reasons", []):
        print(f"REFUSED {json.dumps(reason, ensure_ascii=False, sort_keys=True)}")


if __name__ == "__main__":
    raise SystemExit(main())
