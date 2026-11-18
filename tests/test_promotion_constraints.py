"""Constraint tests: shipping, vendor eligibility, contribution floor, stock."""
import pytest

from crawlbudget.catalog_offers import extract_offers
from crawlbudget.catalog_promotion import build_batch_report
from crawlbudget.promotion_plan import plan_promotions
from crawlbudget.util import sha256_bytes

from tests.promotion_helpers import (
    catalog_body, catalog_for, fixture_catalog, fixture_report, load_fixture,
    plan_of, synthetic_intake,
)


def test_shipping_threshold_flips_on_discount_size():
    data = load_fixture()
    body, catalog = fixture_catalog(data)
    plan = plan_promotions(data, catalog, {})
    cart1 = next(c for c in plan["contracts"]["vendor_eligible"]["cart_reports"]
                 if c["cart_id"] == "cart-1")
    by_id = {(o["offer_id"], o["funder"]): o for o in cart1["options"]}
    assert by_id[("A", "merchant")]["shipping_fee_cents"] == 600
    assert by_id[("B", "merchant")]["shipping_fee_cents"] == 600
    tiny = synthetic_intake(
        [{"cart_id": "c1", "items": {"LAMP-A": 1, "MUG-C": 1},
          "merchant_cost_cents": 3200}],
        [{"offer_id": "T", "cart_id": "c1", "discount_cents": 100,
          "uplift_points": 1, "vendor_eligible": False}],
        {"m": {"merchant_cap_cents": 1000, "vendor_cap_cents": 0}})
    small_body = catalog_body([("LAMP-A", 3000, 8), ("MUG-C", 1200, 12)])
    row = plan_promotions(tiny, catalog_for(small_body), {})["contracts"]["m"][
        "cart_reports"][0]
    opt = row["options"][0]
    assert opt["net_merchandise_cents"] == 4100
    assert opt["shipping_fee_cents"] == 0  # 4100 >= 4000 free-shipping threshold
    assert opt["customer_payable_cents"] == 4100


def test_vendor_funding_only_for_vendor_eligible_offers():
    _, _, report = fixture_report()
    for plan in report["contract_plans"]["contracts"].values():
        for cr in plan["cart_reports"]:
            for option in cr["options"]:
                if option["funder"] == "vendor":
                    assert option["vendor_eligible"] is True
    mp = plan_of(report, "merchant_pays")
    cart2 = next(c for c in mp["cart_reports"] if c["cart_id"] == "cart-2")
    # vendor_cap 0: no vendor-funded option exists even for eligible offers.
    assert all(o["funder"] != "vendor" for o in cart2["options"])
    cart1 = next(c for c in mp["cart_reports"] if c["cart_id"] == "cart-1")
    assert all(o["funder"] != "vendor" for o in cart1["options"])
    # Offer C is not vendor eligible even where a vendor budget does exist.
    ve = plan_of(report, "vendor_eligible")
    ve_cart2 = next(c for c in ve["cart_reports"] if c["cart_id"] == "cart-2")
    assert all(not (o["offer_id"] == "C" and o["funder"] == "vendor")
               for o in ve_cart2["options"])


def test_merchant_funded_a_rejected_below_floor_but_vendor_passes():
    _, _, report = fixture_report()
    for contract_id in ("merchant_pays", "vendor_eligible"):
        cart1 = next(c for c in plan_of(report, contract_id)["cart_reports"]
                     if c["cart_id"] == "cart-1")
        merchant_a = next(o for o in cart1["options"]
                          if o["offer_id"] == "A" and o["funder"] == "merchant")
        assert merchant_a["merchant_contribution_cents"] == 200
        assert merchant_a["eligible"] is False
        vendor_a = next((o for o in cart1["options"]
                         if o["offer_id"] == "A" and o["funder"] == "vendor"), None)
        if contract_id == "vendor_eligible":
            assert vendor_a is not None
            assert vendor_a["merchant_contribution_cents"] == 1200
            assert vendor_a["eligible"] is True
        else:
            assert vendor_a is None


def test_budget_is_shared_across_carts_and_forces_global_tradeoff():
    _, _, report = fixture_report()
    plan = plan_of(report, "merchant_pays")
    charges = sum(cr["merchant_budget_charge_cents"]
                  for cr in plan["cart_reports"])
    assert charges == plan["merchant_budget_used_cents"] == 1100 <= 1300
    # The enumeration saw and rejected budget-overflowing greedy combinations.
    assert plan["optimality"]["budget_prunes"] > 0
    assert plan["optimality"]["feasible_combinations_evaluated"] > 1


def test_missing_sku_blocks_cart_and_leaves_it_unsigned():
    data = synthetic_intake(
        [{"cart_id": "c1", "items": {"GHOST": 1}, "merchant_cost_cents": 10}],
        [{"offer_id": "X", "cart_id": "c1", "discount_cents": 5,
          "uplift_points": 9, "vendor_eligible": False}],
        {"m": {"merchant_cap_cents": 100, "vendor_cap_cents": 0}})
    body = catalog_body([("LAMP-A", 100, 5)])
    row = plan_promotions(data, catalog_for(body), {})["contracts"]["m"][
        "cart_reports"][0]
    assert row["fulfillable"] is False
    assert row["blocked_reason_code"] == "basket_unfulfillable"
    assert row["signed"] is False and row["options"] == []
    assert "GHOST" in " ".join(row["blocked_reasons"])


def test_stock_cannot_be_committed_to_two_real_orders():
    # Stock 2; carts reserve in sorted cart_id order: cart-a-low takes 1 first,
    # so cart-b-high (asking for 2) cannot be committed.
    data = synthetic_intake(
        [{"cart_id": "cart-b-high", "items": {"MUG-C": 2}, "merchant_cost_cents": 100},
         {"cart_id": "cart-a-low", "items": {"MUG-C": 1}, "merchant_cost_cents": 50}],
        [{"offer_id": "H", "cart_id": "cart-b-high", "discount_cents": 50,
          "uplift_points": 9, "vendor_eligible": False},
         {"offer_id": "L", "cart_id": "cart-a-low", "discount_cents": 20,
          "uplift_points": 1, "vendor_eligible": False}],
        {"m": {"merchant_cap_cents": 1000, "vendor_cap_cents": 0}})
    body = catalog_body([("MUG-C", 1200, 2)])
    plan = plan_promotions(data, catalog_for(body), {})
    rows = {r["cart_id"]: r for r in plan["contracts"]["m"]["cart_reports"]}
    assert rows["cart-a-low"]["fulfillable"] is True
    assert rows["cart-b-high"]["fulfillable"] is False
    assert rows["cart-b-high"]["blocked_reason_code"] == "stock_already_committed"
    assert rows["cart-b-high"]["signed"] is False
    assert plan["remaining_stock_after_batch"] == {"MUG-C": 1}


def test_understocked_single_cart_is_unfulfillable():
    data = synthetic_intake(
        [{"cart_id": "c1", "items": {"MUG-C": 99}, "merchant_cost_cents": 10}],
        [], {"m": {"merchant_cap_cents": 100, "vendor_cap_cents": 0}})
    body = catalog_body([("MUG-C", 1200, 12)])
    row = plan_promotions(data, catalog_for(body), {})["contracts"]["m"][
        "cart_reports"][0]
    assert row["fulfillable"] is False
    assert row["blocked_reason_code"] == "basket_unfulfillable"


def test_failed_capture_and_sha_mismatch_are_rejected():
    body = catalog_body([("LAMP-A", 100, 1)])
    failed = {"run_id": "r", "site_id": "s", "url_hash": "u",
              "outcome": "body_too_large", "body_sha256": sha256_bytes(body)}
    with pytest.raises(ValueError, match="successful"):
        extract_offers(body, failed)
    other = catalog_body([("LAMP-A", 101, 1)])
    mismatched = {"run_id": "r", "site_id": "s", "url_hash": "u",
                  "outcome": "success", "body_sha256": sha256_bytes(other)}
    with pytest.raises(ValueError, match="does not match"):
        extract_offers(body, mismatched)
    data = load_fixture()
    _, catalog = fixture_catalog(data)
    with pytest.raises(ValueError, match="source_body_sha256"):
        build_batch_report(data, catalog, other)
