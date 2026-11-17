"""Descriptive basket price and availability across captured catalog days."""

from __future__ import annotations

from typing import Any

from .catalog_offers import quote_basket


def build_basket_history(
    daily_catalogs: list[dict[str, Any]], basket: dict[str, int],
    *, price_ceiling_cents: int,
) -> dict[str, Any]:
    if price_ceiling_cents < 0:
        raise ValueError("price ceiling must be non-negative")
    days = []
    seen: set[str] = set()
    for snapshot in daily_catalogs:
        day = str(snapshot["date"])
        if day in seen:
            raise ValueError("duplicate catalog day")
        seen.add(day)
        quote = quote_basket(snapshot["catalogs"], basket)
        within_ceiling = bool(
            quote["fulfillable"] and quote["total_cents"] <= price_ceiling_cents
        )
        days.append({
            "date": day, "quote": quote, "within_ceiling": within_ceiling,
        })
    days.sort(key=lambda row: row["date"])
    return {
        "basket": basket, "price_ceiling_cents": price_ceiling_cents,
        "days": days,
        "within_ceiling_days": sum(day["within_ceiling"] for day in days),
        "estimate_kind": "historical_descriptive_only",
    }
