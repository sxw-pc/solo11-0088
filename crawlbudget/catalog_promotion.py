"""Independent per-cart preview and whole-batch promotion contract reports."""
from __future__ import annotations
import argparse
import json
from pathlib import Path
from .catalog_offers import extract_offers, quote_basket
from .promotion_plan import plan_promotions
from .util import sha256_bytes


def preview_promotions(data: dict, catalog: dict) -> list[dict]:
    """Greedy per-cart preview: highest uplift offer, ignoring shared rules.

    Retained as the documented baseline.  Its picks can exceed a contract
    budget or fail the merchant contribution floor, so callers that need a
    signable customer offer must use :func:`build_batch_report`.
    """
    offers_by_cart = {}
    for offer in data["offers"]:
        offers_by_cart.setdefault(offer["cart_id"], []).append(offer)
    result = []
    for cart in data["carts"]:
        quote = quote_basket([catalog], cart["items"])
        if not quote["fulfillable"]:
            continue
        offer = sorted(
            offers_by_cart[cart["cart_id"]],
            key=lambda o: (-o["uplift_points"], o["offer_id"]),
        )[0]
        net = quote["total_cents"] - offer["discount_cents"]
        fee = data["shipping"]["fee_cents"] if net < data["shipping"]["free_threshold_cents"] else 0
        result.append({"cart_id": cart["cart_id"], "offer_id": offer["offer_id"],
                       "customer_payable_cents": net + fee,
                       "uplift_points": offer["uplift_points"]})
    return result


def build_batch_report(data: dict, catalog: dict, body: bytes) -> dict:
    """Verify page identity, then plan every authorized funding contract."""
    actual_sha = sha256_bytes(body)
    if actual_sha != data["source_body_sha256"]:
        raise ValueError("captured body does not match fixture source_body_sha256")
    source = {
        "source_page_path": data["source_page_path"],
        "source_site": data["source_site"],
        "source_body_sha256": actual_sha,
        "catalog_run_id": catalog["run_id"],
        "catalog_url_hash": catalog["url_hash"],
        "extracted_skus": [offer["sku"] for offer in catalog["offers"]],
        "extracted_offers": catalog["offers"],
    }
    return {
        "verified_source": source,
        "greedy_per_cart_preview": preview_promotions(data, catalog),
        "contract_plans": plan_promotions(data, catalog, source),
    }


def main(argv=None) -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--fixture", type=Path,
                   default=Path("fixtures/catalog_promotion/intake.json"))
    args = p.parse_args(argv)
    data = json.loads(args.fixture.read_text())
    body = Path(data["source_page_path"]).read_bytes()
    event = {"run_id": "promotion-demo", "site_id": data["source_site"],
             "url_hash": "promotion-catalog", "outcome": "success",
             "body_sha256": sha256_bytes(body)}
    catalog = extract_offers(body, event)
    print(json.dumps(build_batch_report(data, catalog, body),
                     sort_keys=True, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
