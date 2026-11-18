"""Shared builders for the whole-batch promotion test modules."""
import copy
import json
from pathlib import Path

from crawlbudget.catalog_offers import extract_offers
from crawlbudget.catalog_promotion import build_batch_report
from crawlbudget.util import sha256_bytes

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "fixtures" / "catalog_promotion" / "intake.json"
SHIPPING = {"free_threshold_cents": 4000, "fee_cents": 600, "merchant_cost_cents": 400}


def load_fixture():
    return json.loads(FIXTURE.read_text())


def catalog_body(rows):
    parts = [
        f'<article data-sku="{sku}" data-title="{sku}" data-price-cents="{price}" '
        f'data-currency="CNY" data-stock="{stock}"></article>'
        for sku, price, stock in rows
    ]
    return ("<!doctype html><html><body>" + "".join(parts) + "</body></html>").encode()


def catalog_for(body, site_id="mock-alpha", run_id="promo"):
    event = {"run_id": run_id, "site_id": site_id, "url_hash": "catalog",
             "outcome": "success", "body_sha256": sha256_bytes(body)}
    return extract_offers(body, event)


def fixture_catalog(data):
    body = (ROOT / data["source_page_path"]).read_bytes()
    return body, catalog_for(body, data["source_site"])


def fixture_report(data=None):
    data = data or load_fixture()
    body, catalog = fixture_catalog(data)
    return data, catalog, build_batch_report(data, catalog, body)


def plan_of(report, contract_id):
    return report["contract_plans"]["contracts"][contract_id]


def signed(report, contract_id):
    plan = plan_of(report, contract_id)
    return {cr["cart_id"]: (cr["offer_id"], cr["funder"])
            for cr in plan["cart_reports"] if cr["signed"]}


def synthetic_intake(carts, offers, contracts, min_contribution=300):
    return {
        "source_page_path": "unused.html", "source_site": "mock-alpha",
        "source_body_sha256": "", "carts": carts, "offers": offers,
        "shipping": copy.deepcopy(SHIPPING),
        "minimum_merchant_contribution_cents": min_contribution,
        "contracts": contracts,
    }
