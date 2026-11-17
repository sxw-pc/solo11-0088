from pathlib import Path

import pytest

from crawlbudget.catalog_offers import extract_offers
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


def test_offer_extraction_rejects_nonmatching_or_failed_capture():
    first = (FIXTURE / "day-one.html").read_bytes()
    second = (FIXTURE / "day-two.html").read_bytes()
    with pytest.raises(ValueError, match="does not match"):
        extract_offers(second, _event(first, "catalog-01"))
    failed = _event(first, "catalog-01") | {"outcome": "body_too_large"}
    with pytest.raises(ValueError, match="successful"):
        extract_offers(first, failed)
