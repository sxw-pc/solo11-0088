"""Acceptance tests: fixture identity, greedy over-budget draft, 14/20 optima."""
from crawlbudget.catalog_offers import quote_basket
from crawlbudget.catalog_promotion import preview_promotions
from crawlbudget.util import sha256_bytes

from tests.promotion_helpers import (
    fixture_catalog, fixture_report, load_fixture, plan_of, signed,
)


def test_promotion_carts_quote_actual_captured_catalog():
    data = load_fixture()
    body, catalog = fixture_catalog(data)
    assert sha256_bytes(body) == data["source_body_sha256"]
    totals = [quote_basket([catalog], cart["items"])["total_cents"]
              for cart in data["carts"]]
    assert totals == [4200, 3000, 2400]
    assert [q["offer_id"] for q in preview_promotions(data, catalog)] == ["A", "C", "E"]


def test_independent_promotions_exceed_shared_budget():
    data = load_fixture()
    assert sum(o["discount_cents"] for o in data["offers"]
               if o["offer_id"] in {"A", "C", "E"}) == 2500
    assert data["contracts"]["merchant_pays"]["merchant_cap_cents"] == 1300
    assert 4200 - 1000 + 600 - 3200 - 400 == 200 < 300


def test_merchant_pays_contract_optimum_is_c_and_f_uplift_14():
    _, _, report = fixture_report()
    plan = plan_of(report, "merchant_pays")
    assert plan["total_uplift_points"] == 14
    assert signed(report, "merchant_pays") == {
        "cart-2": ("C", "merchant"), "cart-3": ("F", "merchant")}
    assert plan["merchant_budget_used_cents"] == 1100
    assert plan["vendor_budget_used_cents"] == 0
    assert plan["signed_carts"] == ["cart-2", "cart-3"]
    assert plan["merchant_cap_cents"] == 1300 and plan["vendor_cap_cents"] == 0
    cart1 = next(c for c in plan["cart_reports"] if c["cart_id"] == "cart-1")
    assert cart1["signed"] is False
    option_a = next(o for o in cart1["options"]
                    if o["offer_id"] == "A" and o["funder"] == "merchant")
    assert option_a["eligible"] is False
    assert option_a["merchant_contribution_cents"] == 200
    assert any("below_min_300" in r for r in option_a["rejection_reasons"])
    # High uplift must not mask the ineligible merchant-funded option.
    assert not any(o["eligible"] for o in cart1["options"]
                   if o["offer_id"] == "A" and o["funder"] == "merchant")


def test_vendor_eligible_contract_optimum_is_vendor_a_and_merchant_c_uplift_20():
    _, _, report = fixture_report()
    plan = plan_of(report, "vendor_eligible")
    assert plan["total_uplift_points"] == 20
    assert signed(report, "vendor_eligible") == {
        "cart-1": ("A", "vendor"), "cart-2": ("C", "merchant")}
    assert plan["vendor_budget_used_cents"] == 1000
    assert plan["merchant_budget_used_cents"] == 800
    cart1 = next(c for c in plan["cart_reports"] if c["cart_id"] == "cart-1")
    assert cart1["customer_payable_cents"] == 3800
    assert cart1["shipping_fee_cents"] == 600
    assert cart1["merchant_contribution_cents"] == 1200
    assert cart1["vendor_budget_charge_cents"] == 1000
    assert cart1["merchant_budget_charge_cents"] == 0
    cart3 = next(c for c in plan["cart_reports"] if c["cart_id"] == "cart-3")
    assert cart3["signed"] is False
    # One offer is funded by exactly one party, never both budgets at once.
    for cr in plan["cart_reports"]:
        assert not (cr["merchant_budget_charge_cents"]
                    and cr["vendor_budget_charge_cents"])


def test_contracts_keep_separate_budget_pools_and_customer_quotes():
    _, _, report = fixture_report()
    mp = plan_of(report, "merchant_pays")
    ve = plan_of(report, "vendor_eligible")
    assert mp["merchant_budget_used_cents"] <= mp["merchant_cap_cents"]
    assert ve["merchant_budget_used_cents"] <= ve["merchant_cap_cents"]
    assert ve["vendor_budget_used_cents"] <= ve["vendor_cap_cents"]
    # The vendor contract changes the actual customer offer set rather than
    # acting as a blanket block switch on the merchant-only contract.
    mp_cart1 = next(c for c in mp["cart_reports"] if c["cart_id"] == "cart-1")
    ve_cart1 = next(c for c in ve["cart_reports"] if c["cart_id"] == "cart-1")
    assert mp_cart1["signed"] is False and ve_cart1["signed"] is True
    assert ve_cart1["customer_payable_cents"] == 3800


def test_optimality_evidence_documents_exhaustive_bounded_search():
    _, _, report = fixture_report()
    for plan in report["contract_plans"]["contracts"].values():
        opt = plan["optimality"]
        assert opt["search_method"] == \
            "bounded_exhaustive_depth_first_branch_and_bound"
        assert opt["combination_bound"] >= opt["feasible_combinations_evaluated"]
        assert opt["combination_bound"] <= 9 ** len(plan["cart_reports"])
        assert opt["budget_prunes"] >= 0 and opt["bound_prunes"] >= 0


def test_cli_main_emits_full_verified_batch_report(tmp_path, monkeypatch, capsys):
    import json
    from crawlbudget import catalog_promotion as module
    from tests.promotion_helpers import ROOT
    fixture_copy = tmp_path / "intake.json"
    data = load_fixture()
    data["source_page_path"] = str(ROOT / data["source_page_path"])
    fixture_copy.write_text(json.dumps(data))
    monkeypatch.chdir(tmp_path)
    assert module.main(["--fixture", str(fixture_copy)]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["verified_source"]["source_body_sha256"] == \
        load_fixture()["source_body_sha256"]
    assert report["verified_source"]["extracted_skus"] == ["LAMP-A", "MUG-C"]
    assert [q["offer_id"] for q in report["greedy_per_cart_preview"]] == ["A", "C", "E"]
    assert plan_of(report, "merchant_pays")["total_uplift_points"] == 14
    assert plan_of(report, "vendor_eligible")["total_uplift_points"] == 20
