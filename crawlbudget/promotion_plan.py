"""Whole-batch promotion contract planning on a verified offline catalog.

Unlike ``catalog_promotion.preview_promotions`` (an independent per-cart
preview), this module signs at most one offer per cart across the whole batch
under one funding contract.  The search is a bounded exhaustive branch-and-
bound enumeration: every feasible assignment of "no offer" or one
(offer, funder) pair per cart is reachable, so the returned plan is a
reviewable global optimum rather than a greedy per-cart pick.

``uplift_points`` are fictional quote-priority scores used only for ranking;
they do not claim any real-world causal effect.
"""
from __future__ import annotations

import copy
from typing import Any

from .promotion_economics import MERCHANT, VENDOR, fill_options, prepare_carts


def _rank(option: dict[str, Any] | None) -> tuple:
    """Deterministic DFS order: unsigned first, then (offer_id, funder)."""
    if option is None:
        return (0, "", "")
    return (1, option["offer_id"], option["funder"])


def _select_best(best: dict[str, Any], leaf: dict[str, Any]) -> dict[str, Any]:
    """Max uplift, then max contribution, then smallest stable decision key."""
    if best is None:
        return leaf
    if leaf["uplift_points"] != best["uplift_points"]:
        return leaf if leaf["uplift_points"] > best["uplift_points"] else best
    if leaf["merchant_contribution_cents"] != best["merchant_contribution_cents"]:
        return leaf if leaf["merchant_contribution_cents"] > \
            best["merchant_contribution_cents"] else best
    # Stable break: smallest (cart_id, offer_id, funder) lexicographically.
    return leaf if leaf["decision_key"] < best["decision_key"] else best


def plan_contract(
    contract_id: str, contract: dict[str, int], prepared: dict[str, Any],
    data: dict[str, Any],
) -> dict[str, Any]:
    """Exhaustively enumerate cart choices under one contract's budgets."""
    rows = copy.deepcopy(prepared["cart_rows"])
    for row in rows:
        fill_options(row, data, contract["vendor_cap_cents"] > 0)
    choices = [
        sorted(([None] + [o for o in row["options"] if o["eligible"]]), key=_rank)
        if row["fulfillable"] else [None]
        for row in rows
    ]
    stats = {
        "search_method": "bounded_exhaustive_depth_first_branch_and_bound",
        "combination_bound": 1, "feasible_combinations_evaluated": 0,
        "budget_prunes": 0, "bound_prunes": 0,
    }
    for options in choices:
        stats["combination_bound"] *= len(options)
    # Per-cart optimistic uplift; the suffix sum bounds every subtree.
    per_cart_max = [
        max((o["uplift_points"] for o in row["options"] if o["eligible"]),
            default=0)
        for row in rows
    ]
    suffix_max = [0] * (len(rows) + 1)
    for idx in range(len(rows) - 1, -1, -1):
        suffix_max[idx] = suffix_max[idx + 1] + per_cart_max[idx]
    best: dict[str, Any] | None = None

    def visit(index: int, m_used: int, v_used: int, uplift: int,
              contribution: int, decisions: list[tuple]) -> None:
        nonlocal best
        # Safe bound: prune only when even the subtree's optimistic uplift is
        # strictly below the incumbent.  Equal-score subtrees survive, so every
        # tie-break candidate is compared explicitly at its leaf.
        incumbent_uplift = best["uplift_points"] if best else 0
        if uplift + suffix_max[index] < incumbent_uplift:
            stats["bound_prunes"] += 1
            return
        if index == len(rows):
            stats["feasible_combinations_evaluated"] += 1
            leaf = {
                "uplift_points": uplift,
                "merchant_contribution_cents": contribution,
                # Structured per-cart entry in sorted cart_id order; signed
                # (0, ...) sorts before unsigned (1, "", "") for stable breaks.
                "decision_key": tuple(decisions),
                "merchant_used": m_used, "vendor_used": v_used,
            }
            best = _select_best(best, leaf)
            return
        for option in choices[index]:  # None (no offer) is explored first
            if option is None:
                decisions.append((1, "", ""))
                visit(index + 1, m_used, v_used, uplift, contribution, decisions)
                decisions.pop()
                continue
            m_charge = option["discount_cents"] if option["funder"] == MERCHANT else 0
            v_charge = option["discount_cents"] if option["funder"] == VENDOR else 0
            if (m_used + m_charge > contract["merchant_cap_cents"]
                    or v_used + v_charge > contract["vendor_cap_cents"]):
                stats["budget_prunes"] += 1
                continue
            decisions.append((0, option["offer_id"], option["funder"]))
            visit(index + 1, m_used + m_charge, v_used + v_charge,
                  uplift + option["uplift_points"],
                  contribution + option["merchant_contribution_cents"], decisions)
            decisions.pop()

    visit(0, 0, 0, 0, 0, [])
    assert best is not None
    chosen = {
        rows[index]["cart_id"]: (entry[1], entry[2])
        for index, entry in enumerate(best["decision_key"])
        if entry[0] == 0
    }
    return {
        "contract_id": contract_id,
        "merchant_cap_cents": contract["merchant_cap_cents"],
        "vendor_cap_cents": contract["vendor_cap_cents"],
        "merchant_budget_used_cents": best["merchant_used"],
        "vendor_budget_used_cents": best["vendor_used"],
        "total_uplift_points": best["uplift_points"],
        "total_merchant_contribution_cents": best["merchant_contribution_cents"],
        "signed_carts": sorted(chosen),
        "optimality": stats,
        "cart_reports": [_cart_report(row, chosen.get(row["cart_id"])) for row in rows],
    }


def _cart_report(row: dict[str, Any], chosen: tuple[str, str] | None) -> dict[str, Any]:
    report = {k: row[k] for k in (
        "cart_id", "items", "fulfillable", "gross_cents", "lines",
        "blocked_reason_code", "blocked_reasons", "options")}
    report.update({
        "signed": chosen is not None, "offer_id": None, "funder": None,
        "customer_payable_cents": row["gross_cents"], "shipping_fee_cents": None,
        "merchant_contribution_cents": 0, "merchant_budget_charge_cents": 0,
        "vendor_budget_charge_cents": 0, "uplift_points": 0,
        "decision_reasons": [],
    })
    if not row["fulfillable"]:
        report["decision_reasons"] = [
            "not_signed:cart_blocked_by_verified_stock_or_catalog"]
    elif chosen is None:
        eligible = [o for o in row["options"] if o["eligible"]]
        if not eligible:
            report["decision_reasons"] = [
                "not_signed:no_option_meets_contribution_floor_or_funding_rules"]
        else:
            report["decision_reasons"] = [
                "not_signed:budget_or_global_uplift_tradeoff_favored_other_carts"]
    else:
        offer_id, funder = chosen
        option = next(o for o in row["options"]
                      if o["offer_id"] == offer_id and o["funder"] == funder)
        report.update({
            "offer_id": offer_id, "funder": funder,
            "customer_payable_cents": option["customer_payable_cents"],
            "shipping_fee_cents": option["shipping_fee_cents"],
            "merchant_contribution_cents": option["merchant_contribution_cents"],
            "merchant_budget_charge_cents":
                option["discount_cents"] if funder == MERCHANT else 0,
            "vendor_budget_charge_cents":
                option["discount_cents"] if funder == VENDOR else 0,
            "uplift_points": option["uplift_points"],
            "decision_reasons": [
                f"signed:eligible_{funder}_funded_option_in_global_optimum"],
        })
    return report


def plan_promotions(
    data: dict[str, Any], catalog: dict[str, Any], source: dict[str, Any]
) -> dict[str, Any]:
    """Plan every authorized contract against the same verified catalog."""
    prepared = prepare_carts(data, catalog)
    contracts = {
        contract_id: plan_contract(contract_id, contract, prepared, data)
        for contract_id, contract in sorted(data["contracts"].items())
    }
    return {
        "verified_source": source,
        "shipping": data["shipping"],
        "minimum_merchant_contribution_cents":
            data["minimum_merchant_contribution_cents"],
        "remaining_stock_after_batch": prepared["remaining_stock"],
        "contracts": contracts,
    }
