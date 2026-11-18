"""Verified-catalog economics for whole-batch promotion planning.

Pure helpers: shared stock reservation and the per-order customer/merchant
money math (free-shipping threshold, vendor rebate, contribution floor).
Kept separate from the combinatorial search in :mod:`promotion_plan`.
"""
from __future__ import annotations

from typing import Any

from .catalog_offers import quote_basket

MERCHANT = "merchant"
VENDOR = "vendor"


class StockLedger:
    """Reserves catalog stock so one SKU cannot serve two real orders twice."""

    def __init__(self, catalog: dict[str, Any]) -> None:
        self._stock = {o["sku"]: o["stock"] for o in catalog["offers"]}

    def reserve(self, quantities: dict[str, int]) -> tuple[bool, list[str]]:
        reasons: list[str] = []
        for sku in sorted(quantities):
            asked = quantities[sku]
            if sku not in self._stock:
                reasons.append(f"sku {sku} absent from verified catalog")
            elif asked > self._stock[sku]:
                reasons.append(
                    f"sku {sku} requests {asked}, remaining stock "
                    f"{self._stock[sku]} already committed to earlier carts"
                )
        if reasons:
            return False, reasons
        for sku, asked in quantities.items():
            self._stock[sku] -= asked
        return True, []

    def remaining_stock(self) -> dict[str, int]:
        return dict(self._stock)


def offer_economics(
    *, gross: int, discount: int, shipping: dict[str, int], funder: str
) -> dict[str, int]:
    """Customer payable and vendor rebate for one signed order."""
    if discount > gross:
        raise ValueError("discount cannot exceed the pre-discount merchandise total")
    net = gross - discount
    fee = shipping["fee_cents"] if net < shipping["free_threshold_cents"] else 0
    return {
        "net_merchandise_cents": net,
        "shipping_fee_cents": fee,
        "customer_payable_cents": net + fee,
        "vendor_rebate_cents": discount if funder == VENDOR else 0,
    }


def prepare_carts(data: dict[str, Any], catalog: dict[str, Any]) -> dict[str, Any]:
    """Quote all carts and reserve shared stock in deterministic cart order."""
    ledger = StockLedger(catalog)
    rows: list[dict[str, Any]] = []
    for cart in sorted(data["carts"], key=lambda c: c["cart_id"]):
        quote = quote_basket([catalog], cart["items"])
        row: dict[str, Any] = {
            "cart_id": cart["cart_id"],
            "items": dict(sorted(cart["items"].items())),
            "merchant_goods_cost_cents": cart["merchant_cost_cents"],
            "lines": quote["lines"], "fulfillable": quote["fulfillable"],
            "gross_cents": quote["total_cents"], "blocked_reason_code": None,
            "blocked_reasons": [], "options": [],
        }
        if not quote["fulfillable"]:
            row["blocked_reason_code"] = "basket_unfulfillable"
            row["blocked_reasons"] = [
                f"sku {sku} missing or understocked in verified catalog"
                for sku in quote["missing_skus"]
            ]
        else:
            ok, reasons = ledger.reserve(cart["items"])
            if not ok:
                row.update({
                    "fulfillable": False, "gross_cents": None, "lines": [],
                    "blocked_reason_code": "stock_already_committed",
                    "blocked_reasons": reasons,
                })
        rows.append(row)
    return {"cart_rows": rows, "remaining_stock": ledger.remaining_stock()}


def fill_options(
    row: dict[str, Any], data: dict[str, Any], vendor_funding_available: bool
) -> None:
    """Populate contract-specific (offer, funder) economics on a quoted cart."""
    if not row["fulfillable"]:
        return
    shipping = data["shipping"]
    min_contribution = data["minimum_merchant_contribution_cents"]
    offers = sorted(
        (o for o in data["offers"] if o["cart_id"] == row["cart_id"]),
        key=lambda o: o["offer_id"],
    )
    for offer in offers:
        can_use_vendor = bool(offer.get("vendor_eligible")) and vendor_funding_available
        funders = [MERCHANT, VENDOR] if can_use_vendor else [MERCHANT]
        for funder in funders:
            econ = offer_economics(
                gross=row["gross_cents"], discount=offer["discount_cents"],
                shipping=shipping, funder=funder)
            contribution = (
                econ["net_merchandise_cents"] + econ["shipping_fee_cents"]
                - row["merchant_goods_cost_cents"] - shipping["merchant_cost_cents"]
                + econ["vendor_rebate_cents"])
            rejects = []
            if contribution < min_contribution:
                rejects.append(
                    f"merchant_contribution_{contribution}_below_min_{min_contribution}")
            row["options"].append({
                "offer_id": offer["offer_id"], "funder": funder,
                "discount_cents": offer["discount_cents"],
                "uplift_points": offer["uplift_points"],
                "vendor_eligible": bool(offer.get("vendor_eligible")),
                **econ,
                "merchant_contribution_cents": contribution,
                "eligible": not rejects, "rejection_reasons": rejects,
            })
