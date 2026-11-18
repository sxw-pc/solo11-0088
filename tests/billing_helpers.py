"""Shared helpers for subscriber-billing tests."""

import json
from pathlib import Path

from crawlbudget.billing_inputs import parse_inputs
from crawlbudget.billing_receipts import Receipt, expected_urls, load_receipts
from crawlbudget.config import load_config
from crawlbudget.demo_site import load_page_manifest
from crawlbudget.subscriber_billing import build_report

ROOT = Path(__file__).parents[1]
INTAKE = ROOT / "fixtures/subscriber_billing/intake.json"
RECEIPTS = ROOT / "fixtures/subscriber_billing/receipts.json"


def data() -> dict:
    return json.loads(INTAKE.read_text(encoding="utf-8"))


def context():
    config = load_config(ROOT / "fixtures/demo_sites.yaml", project_root=ROOT)
    manifest = load_page_manifest(ROOT / "fixtures/pages/manifest.yaml")
    parsed = parse_inputs(data())
    urls = expected_urls(parsed, config, manifest)
    return config, manifest, parsed, urls


def receipts() -> list[Receipt]:
    return load_receipts(RECEIPTS)


def verified_weights(parsed):
    return {s.subscriber_id: {"response_bytes": s.body_bytes}
            for s in parsed.subscribers}


def report(payload=None, **kwargs):
    return build_report(payload if payload is not None else data(),
                        project_root=ROOT, receipts=receipts(), **kwargs)


def replace_receipt(**changes) -> list[Receipt]:
    items = receipts()
    wanted = changes.pop("subscriber_id", None)
    for index, receipt in enumerate(items):
        if wanted is None or receipt.subscriber_id == wanted:
            items[index] = Receipt(**{**receipt.__dict__, **changes})
            return items
    raise AssertionError("subscriber not found")
