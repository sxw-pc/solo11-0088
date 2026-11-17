import json
from datetime import date, timedelta
from pathlib import Path

import pytest

from crawlbudget.catalog_offers import extract_offers, quote_basket
from crawlbudget.basket_history import build_basket_history
from crawlbudget.util import sha256_bytes


FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "catalog"


def _event(body, run_id):
    return {
        "run_id": run_id, "site_id": "mock-alpha", "url_hash": "catalog-page",
        "outcome": "success", "body_sha256": sha256_bytes(body),
    }


def test_offline_catalog_extraction_retains_offer_values_and_capture_identity():
    first = (FIXTURE / "day-one.html").read_bytes()
    second = (FIXTURE / "day-two.html").read_bytes()
    old = extract_offers(first, _event(first, "catalog-01"))
    new = extract_offers(second, _event(second, "catalog-02"))
    assert (old["site_id"], old["url_hash"]) == ("mock-alpha", "catalog-page")
    assert old["offers"][0]["price_cents"] == 3000
    assert new["offers"][0]["price_cents"] == 3200
    assert new["offers"][0]["stock"] == 6
    quote = quote_basket([old], {"LAMP-A": 1, "MUG-C": 1})
    assert quote["fulfillable"]
    assert quote["total_cents"] == 4200
    assert quote["estimate_kind"] == "current_snapshot_only"


def test_offer_extraction_rejects_nonmatching_or_failed_capture():
    first = (FIXTURE / "day-one.html").read_bytes()
    second = (FIXTURE / "day-two.html").read_bytes()
    with pytest.raises(ValueError, match="does not match"):
        extract_offers(second, _event(first, "catalog-01"))
    failed = _event(first, "catalog-01") | {"outcome": "body_too_large"}
    with pytest.raises(ValueError, match="successful"):
        extract_offers(first, failed)


def test_synthetic_multistore_cohort_has_two_price_ceiling_failures():
    cohort = json.loads((FIXTURE / "basket_cohort.json").read_text(encoding="utf-8"))
    totals = []
    daily_catalogs = []
    for day in range(1, cohort["days"] + 1):
        catalogs = []
        for site_id, offers in cohort["stores"].items():
            lines = []
            for sku, details in offers.items():
                stock = details["stock"]
                if day in cohort["exception_days"]:
                    stock = cohort["stock_overrides_on_exception_days"].get(site_id, {}).get(sku, stock)
                lines.append(
                    f'<article data-sku="{sku}" data-title="{sku}" '
                    f'data-price-cents="{details["price_cents"]}" '
                    f'data-currency="CNY" data-stock="{stock}"></article>'
                )
            body = ("<html><body>" + "".join(lines) + "</body></html>").encode()
            event = _event(body, f"basket-{day:02d}") | {"site_id": site_id}
            catalogs.append(extract_offers(body, event))
        quote = quote_basket(catalogs, cohort["basket"])
        assert quote["fulfillable"]
        totals.append(quote["total_cents"])
        daily_catalogs.append({
            "date": (date(2026, 1, 1) + timedelta(days=day - 1)).isoformat(),
            "catalogs": catalogs,
        })
    assert totals.count(3700) == 62
    assert totals.count(4200) == 2
    assert [index + 1 for index, total in enumerate(totals) if total > 4000] == [4, 17]
    history = build_basket_history(
        daily_catalogs, cohort["basket"], price_ceiling_cents=4000
    )
    assert history["within_ceiling_days"] == 62
    assert history["estimate_kind"] == "historical_descriptive_only"
