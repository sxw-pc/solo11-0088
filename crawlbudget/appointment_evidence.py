"""Verify a captured synthetic appointment page against an executed GET.

This intake deliberately does not compute bookable occurrences. It only fixes
the page identity and the two predeclared time commitments for a later feed.
"""

from __future__ import annotations

import argparse
import json
from html.parser import HTMLParser
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .storage import Storage
from .util import canonical_json, sha256_bytes, sha256_text


class _TemplateParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.inside = False
        self.fragments: list[str] = []
        self.count = 0

    def handle_starttag(self, tag, attrs):
        attributes = dict(attrs)
        if tag == "script" and attributes.get("id") == "appointment-template":
            if attributes.get("type") != "application/json":
                raise ValueError("appointment script must be JSON")
            self.count += 1
            self.inside = True

    def handle_endtag(self, tag):
        if tag == "script":
            self.inside = False

    def handle_data(self, data):
        if self.inside:
            self.fragments.append(data)


def verify_appointment_evidence(
    storage: Storage, run_id: str, body_path: str | Path, contract_path: str | Path
) -> dict:
    body = Path(body_path).read_bytes()
    contract = json.loads(Path(contract_path).read_text(encoding="utf-8"))
    if not isinstance(contract, dict):
        raise ValueError("contract must be an object")
    events = [
        event for event in storage.get_request_events(run_id)
        if event["url"] == contract.get("source_url")
        and event["outcome"] == "success"
        and event["body_sha256"] == sha256_bytes(body)
        and str(event["content_type"]).startswith("text/html")
        and int(event["response_bytes"]) == len(body)
    ]
    if len(events) != 1:
        raise ValueError("exactly one successful matching HTML receipt is required")
    parser = _TemplateParser()
    parser.feed(body.decode("utf-8"))
    if parser.count != 1:
        raise ValueError("exactly one appointment template is required")
    template = json.loads("".join(parser.fragments))
    if not isinstance(template, dict):
        raise ValueError("template must be an object")
    for key in ("provider_id", "slot_id", "first_local", "time_zone", "weekly_count"):
        if template.get(key) != contract.get(key):
            raise ValueError(f"template and contract disagree on {key}")
    if contract.get("cadences") != ["same_local_hour", "fixed_168h"]:
        raise ValueError("unexpected cadence commitments")
    if contract.get("gap_rule") != "skip":
        raise ValueError("unsupported gap commitment")
    if not (1 <= int(template["weekly_count"]) <= 12):
        raise ValueError("weekly count out of range")
    try:
        ZoneInfo(template["time_zone"])
    except ZoneInfoNotFoundError as exc:
        raise ValueError("unknown time zone") from exc
    notice = template.get("cancel_notice")
    if not isinstance(notice, dict) or notice.get("slot_id") != template["slot_id"]:
        raise ValueError("cancellation references another slot")
    if not (1 <= int(notice.get("occurrence_index", 0)) <= template["weekly_count"]):
        raise ValueError("cancellation index out of range")
    return {
        "run_id": run_id,
        "site_id": events[0]["site_id"],
        "job_id": events[0]["job_id"],
        "source_url": contract["source_url"],
        "body_sha256": events[0]["body_sha256"],
        "contract_id": contract["contract_id"],
        "contract_fingerprint": sha256_text(canonical_json(contract)),
        "provider_id": template["provider_id"],
        "slot_id": template["slot_id"],
        "first_local": template["first_local"],
        "time_zone": template["time_zone"],
        "weekly_count": template["weekly_count"],
        "cadences": contract["cadences"],
        "gap_rule": contract["gap_rule"],
        "cancel_notice": notice,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Verify a synthetic appointment receipt")
    parser.add_argument("--db", required=True)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--body", required=True)
    parser.add_argument("--contract", required=True)
    args = parser.parse_args()
    with Storage(args.db) as storage:
        evidence = verify_appointment_evidence(storage, args.run_id, args.body, args.contract)
    print(canonical_json(evidence))


if __name__ == "__main__":
    main()
