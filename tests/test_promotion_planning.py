"""Tie-breaks, reordering, catalog propagation, scale, and a brute-force oracle."""
import copy
import itertools
import random
import time

from crawlbudget.catalog_offers import extract_offers
from crawlbudget.catalog_promotion import build_batch_report
from crawlbudget.promotion_plan import plan_promotions
from crawlbudget.util import sha256_bytes

from tests.promotion_helpers import (
    catalog_body, catalog_for, fixture_catalog, fixture_report, load_fixture,
    plan_of, signed, synthetic_intake,
)


def test_equal_uplift_tie_breaks_on_total_merchant_contribution():
    # Gross 3000, fee 600, shipping cost 400: margin = 3200 - goods_cost - disc.
    data = synthetic_intake(
        [{"cart_id": "cart-x", "items": {"LAMP-A": 1}, "merchant_cost_cents": 2500},
         {"cart_id": "cart-y", "items": {"LAMP-A": 1}, "merchant_cost_cents": 2100}],
        [{"offer_id": "X", "cart_id": "cart-x", "discount_cents": 300,
          "uplift_points": 7, "vendor_eligible": False},
         {"offer_id": "Y", "cart_id": "cart-y", "discount_cents": 300,
          "uplift_points": 7, "vendor_eligible": False}],
        {"m": {"merchant_cap_cents": 300, "vendor_cap_cents": 0}})
    body = catalog_body([("LAMP-A", 3000, 8)])
    plan = plan_promotions(data, catalog_for(body), {})["contracts"]["m"]
    assert plan["total_uplift_points"] == 7
    assert plan["signed_carts"] == ["cart-y"]
    chosen = next(c for c in plan["cart_reports"] if c["cart_id"] == "cart-y")
    # X: 2700+600-2500-400 = 400; Y: 2700+600-2100-400 = 800.
    x_row = next(c for c in plan["cart_reports"] if c["cart_id"] == "cart-x")
    assert x_row["options"][0]["merchant_contribution_cents"] == 400
    assert chosen["offer_id"] == "Y"
    assert chosen["merchant_contribution_cents"] == 800


def test_equal_uplift_and_contribution_tie_breaks_on_offer_id_then_cart():
    # Both carts: gross 3000, disc 300, goods cost 2400 -> margin 500 each.
    data = synthetic_intake(
        [{"cart_id": "cart-p", "items": {"LAMP-A": 1}, "merchant_cost_cents": 2400},
         {"cart_id": "cart-q", "items": {"LAMP-A": 1}, "merchant_cost_cents": 2400}],
        [{"offer_id": "P2", "cart_id": "cart-p", "discount_cents": 300,
          "uplift_points": 5, "vendor_eligible": False},
         {"offer_id": "Q1", "cart_id": "cart-q", "discount_cents": 300,
          "uplift_points": 5, "vendor_eligible": False}],
        {"m": {"merchant_cap_cents": 300, "vendor_cap_cents": 0}})
    body = catalog_body([("LAMP-A", 3000, 8)])
    plan = plan_promotions(data, catalog_for(body), {})["contracts"]["m"]
    assert plan["signed_carts"] == ["cart-p"]  # cart-p sorts before cart-q
    data2 = synthetic_intake(
        [{"cart_id": "cart-z", "items": {"LAMP-A": 1}, "merchant_cost_cents": 2500}],
        [{"offer_id": "Z9", "cart_id": "cart-z", "discount_cents": 200,
          "uplift_points": 5, "vendor_eligible": False},
         {"offer_id": "Z1", "cart_id": "cart-z", "discount_cents": 200,
          "uplift_points": 5, "vendor_eligible": False}],
        {"m": {"merchant_cap_cents": 200, "vendor_cap_cents": 0}})
    plan2 = plan_promotions(data2, catalog_for(body), {})["contracts"]["m"]
    assert plan2["cart_reports"][0]["offer_id"] == "Z1"


def test_input_reordering_yields_identical_plans():
    data = load_fixture()
    _, _, baseline = fixture_report(data)
    shuffled = copy.deepcopy(data)
    shuffled["carts"] = list(reversed(shuffled["carts"]))
    shuffled["offers"] = list(reversed(shuffled["offers"]))
    body, catalog = fixture_catalog(shuffled)
    reordered = build_batch_report(shuffled, catalog, body)
    for contract_id in data["contracts"]:
        a = plan_of(baseline, contract_id)
        b = plan_of(reordered, contract_id)
        assert b["total_uplift_points"] == a["total_uplift_points"]
        assert b["total_merchant_contribution_cents"] == \
            a["total_merchant_contribution_cents"]
        assert signed(reordered, contract_id) == signed(baseline, contract_id)
        assert b["cart_reports"] == a["cart_reports"]


def test_changed_catalog_prices_recompute_eligibility_and_optimum():
    data = load_fixture()
    # LAMP-A 3000 -> 3400, MUG-C 1200 -> 1400.  New gross: 4800 / 3400 / 2800.
    # Merchant-funded A now earns 800 (was 200), so the merchant contract
    # shifts from C+F (14) to A+F (16); vendor contract keeps A+C at 20.
    new_body = catalog_body([("LAMP-A", 3400, 8), ("MUG-C", 1400, 12)])
    plan = plan_promotions(data, catalog_for(new_body),
                           {"source_body_sha256": sha256_bytes(new_body)})
    mp, ve = plan["contracts"]["merchant_pays"], plan["contracts"]["vendor_eligible"]
    cart1 = next(c for c in mp["cart_reports"] if c["cart_id"] == "cart-1")
    assert cart1["gross_cents"] == 4800
    merchant_a = next(o for o in cart1["options"]
                      if o["offer_id"] == "A" and o["funder"] == "merchant")
    assert merchant_a["merchant_contribution_cents"] == 800
    assert merchant_a["eligible"] is True
    assert mp["total_uplift_points"] == 16
    assert signed({"contract_plans": plan}, "merchant_pays") == {
        "cart-1": ("A", "merchant"), "cart-3": ("F", "merchant")}
    assert ve["total_uplift_points"] == 20
    assert signed({"contract_plans": plan}, "vendor_eligible") == {
        "cart-1": ("A", "vendor"), "cart-2": ("C", "merchant")}
    bad_event = {"run_id": "r", "site_id": "s", "url_hash": "u",
                 "outcome": "body_too_large", "body_sha256": sha256_bytes(new_body)}
    try:
        extract_offers(new_body, bad_event)
    except ValueError as exc:
        assert "successful" in str(exc)
    else:
        raise AssertionError("failed capture must not extract offers")


def test_changed_stock_reduces_fulfillable_carts_and_optimum():
    data = load_fixture()
    # LAMP-A stock 1: cart-1 reserves the only unit (sorted first), so cart-2's
    # LAMP-A basket cannot be committed and C disappears from every plan.
    tight_body = catalog_body([("LAMP-A", 3000, 1), ("MUG-C", 1200, 12)])
    plan = plan_promotions(data, catalog_for(tight_body),
                           {"source_body_sha256": sha256_bytes(tight_body)})
    for contract in plan["contracts"].values():
        rows = {r["cart_id"]: r for r in contract["cart_reports"]}
        assert rows["cart-2"]["fulfillable"] is False
        assert rows["cart-2"]["blocked_reason_code"] == "stock_already_committed"
        assert all(cr["offer_id"] != "C" for cr in contract["cart_reports"])


def test_max_size_batch_stays_bounded_and_fast():
    carts, offers = [], []
    for i in range(8):
        cid = f"cart-{i}"
        carts.append({"cart_id": cid, "items": {"LAMP-A": 1},
                      "merchant_cost_cents": 100})
        for j in range(4):
            offers.append({"offer_id": f"{i}-{j}", "cart_id": cid,
                           "discount_cents": 10, "uplift_points": j + 1,
                           "vendor_eligible": True})
    data = synthetic_intake(
        carts, offers,
        {"c": {"merchant_cap_cents": 100_000, "vendor_cap_cents": 100_000}},
        min_contribution=1)
    body = catalog_body([("LAMP-A", 1000, 50)])
    started = time.monotonic()
    plan = plan_promotions(data, catalog_for(body), {})["contracts"]["c"]
    assert time.monotonic() - started < 2.0
    assert plan["optimality"]["combination_bound"] == 9 ** 8
    assert plan["optimality"]["feasible_combinations_evaluated"] < \
        plan["optimality"]["combination_bound"]
    assert plan["total_uplift_points"] == 8 * 4


def test_planner_matches_independent_brute_force_on_random_batches():
    rng = random.Random(20261118)
    for _ in range(40):
        n_carts = rng.randint(1, 5)
        carts, offers = [], []
        for i in range(n_carts):
            cid = f"cart-{i}"
            carts.append({"cart_id": cid, "items": {"MUG-C": rng.randint(1, 2)},
                          "merchant_cost_cents": rng.randint(50, 900)})
            for j in range(rng.randint(1, 4)):
                offers.append({
                    "offer_id": f"{cid}-{j}", "cart_id": cid,
                    "discount_cents": rng.choice([50, 100, 200, 300]),
                    "uplift_points": rng.randint(1, 9),
                    "vendor_eligible": rng.random() < 0.4})
        contract = {"merchant_cap_cents": rng.choice([0, 200, 600, 5000]),
                    "vendor_cap_cents": rng.choice([0, 300, 1000])}
        data = synthetic_intake(carts, offers, {"c": contract})
        body = catalog_body([("MUG-C", 1000, rng.randint(1, 2 * n_carts + 2))])
        plan = plan_promotions(data, catalog_for(body), {})["contracts"]["c"]
        rows = {r["cart_id"]: r for r in plan["cart_reports"]}
        ordered = sorted(carts, key=lambda c: c["cart_id"])
        domains = [
            [None] if not rows[c["cart_id"]]["fulfillable"]
            else [None] + [(o["offer_id"], o["funder"])
                           for o in rows[c["cart_id"]]["options"] if o["eligible"]]
            for c in ordered
        ]
        best_key = None
        for assignment in itertools.product(*domains):
            m_used = v_used = uplift = contribution = 0
            for cart, choice in zip(ordered, assignment):
                if choice is None:
                    continue
                opt = next(o for o in rows[cart["cart_id"]]["options"]
                           if (o["offer_id"], o["funder"]) == choice)
                m_used += opt["discount_cents"] if opt["funder"] == "merchant" else 0
                v_used += opt["discount_cents"] if opt["funder"] == "vendor" else 0
                uplift += opt["uplift_points"]
                contribution += opt["merchant_contribution_cents"]
            if m_used > contract["merchant_cap_cents"] \
                    or v_used > contract["vendor_cap_cents"]:
                continue
            key = (uplift, contribution, tuple(
                (0, c[0], c[1]) if c is not None else (1, "", "")
                for c in assignment))
            if best_key is None:
                best_key = key
            elif key[0] != best_key[0]:
                best_key = key if key[0] > best_key[0] else best_key
            elif key[1] != best_key[1]:
                best_key = key if key[1] > best_key[1] else best_key
            elif key[2] < best_key[2]:
                best_key = key
        assert plan["total_uplift_points"] == best_key[0]
        assert plan["total_merchant_contribution_cents"] == best_key[1]
