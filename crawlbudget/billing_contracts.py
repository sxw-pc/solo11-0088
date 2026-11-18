"""Independent subscriber charging contracts with exact, conserved cents.

Two contracts are supported:

``byte_share``
    Split the verified full-coalition cost in proportion to each subscriber's
    attested successful body bytes.  Each share is floored to the cent, then
    the remaining cents go to the largest fractional remainders (ties by
    ``subscriber_id``).

``marginal_share``
    The exact Shapley value of the session-cost game: each subscriber's average
    marginal cost over every join order.  Every subset cost is re-solved by
    ``billing_model``; no full-route stop is ever simply deleted.

Both contracts refuse to sign instead of trimming a bill when the rule would
exceed a subscriber's solo price or fail to conserve the full cost.
"""

from __future__ import annotations

from fractions import Fraction
from itertools import permutations
from typing import Any, Mapping

from .billing_inputs import BillingError, ParsedInputs, billing_error
from .billing_model import CoalitionTable


def _format(cents: int) -> str:
    sign = "-" if cents < 0 else ""
    cents = abs(cents)
    return f"{sign}{cents // 100}.{cents % 100:02d}"


def _allocate_cents(raw: Mapping[str, Fraction], total_cents: int) -> list[dict[str, Any]]:
    """Floor exact shares, then hand out the residue by remainder and id."""
    floored = {key: int(value) for key, value in raw.items()}
    residue = total_cents - sum(floored.values())
    if residue < 0 or residue > len(floored):
        raise billing_error(
            "billing_rounding_invalid",
            "rounded shares cannot be reconciled to the billable total",
            residue=residue, subscribers=len(floored),
        )
    ranked = sorted(raw, key=lambda key: (-(raw[key] - floored[key]), key))
    for index in range(residue):
        floored[ranked[index]] += 1
    return [
        {
            "subscriber_id": key,
            "cents": floored[key],
            "amount": _format(floored[key]),
            "exact_cents": raw[key].numerator / raw[key].denominator,
            "rounding_remainder": float(raw[key] - int(raw[key])),
        }
        for key in sorted(floored)
    ]


def solo_prices(table: CoalitionTable) -> dict[str, int]:
    return {subscriber_id: table.cost(table.mask_for([subscriber_id]))
            for subscriber_id in table.ids}


def _check_and_sign(
    contract: str,
    rows: list[dict[str, Any]],
    table: CoalitionTable,
    full_cost: int,
    *,
    evidence: dict[str, Any] | None = None,
) -> dict[str, Any]:
    solo = solo_prices(table)
    violations: list[dict[str, Any]] = []
    for row in rows:
        subscriber_id = row["subscriber_id"]
        if row["cents"] < 0:
            violations.append({
                "subscriber_id": subscriber_id, "code": "negative_charge",
                "cents": row["cents"],
            })
        if row["cents"] > solo[subscriber_id] * 100:
            violations.append({
                "subscriber_id": subscriber_id, "code": "solo_price_exceeded",
                "cents": row["cents"], "solo_cents": solo[subscriber_id] * 100,
            })
    total_cents = sum(row["cents"] for row in rows)
    if violations:
        return {
            "contract": contract, "status": "refused", "signed": False,
            "reason": "solo_price_conflict",
            "evidence": {
                "violations": violations,
                "rule_total_cents": total_cents,
                "full_cost_cents": full_cost * 100,
                "note": ("refused rather than trimming invoices; trimming would "
                         "break revenue conservation"),
                **(evidence or {}),
            },
            "invoices": None,
            "total_cents": total_cents,
            "full_cost_cents": full_cost * 100,
            "solo_prices": solo,
        }
    if total_cents != full_cost * 100:
        return {
            "contract": contract, "status": "refused", "signed": False,
            "reason": "conservation_failed",
            "evidence": {
                "rule_total_cents": total_cents,
                "full_cost_cents": full_cost * 100,
                **(evidence or {}),
            },
            "invoices": None,
            "total_cents": total_cents,
            "full_cost_cents": full_cost * 100,
            "solo_prices": solo,
        }
    return {
        "contract": contract, "status": "signed", "signed": True, "reason": None,
        "invoices": rows,
        "total_cents": total_cents,
        "full_cost_cents": full_cost * 100,
        "solo_prices": solo,
        "upper_bound_respected": all(
            row["cents"] <= solo[row["subscriber_id"]] * 100 for row in rows
        ),
        "conserved": total_cents == full_cost * 100,
        "evidence": evidence or {},
    }


def _refused(contract: str, reason: str, full_cost: int,
             evidence: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "contract": contract, "status": "refused", "signed": False,
        "reason": reason, "evidence": dict(evidence), "invoices": None,
        "total_cents": 0, "full_cost_cents": full_cost * 100,
    }


def byte_share(
    parsed: ParsedInputs, table: CoalitionTable, verified: Mapping[str, Mapping[str, Any]]
) -> dict[str, Any]:
    full_cost = table.cost(table.full)
    weights = {subscriber_id: int(verified[subscriber_id]["response_bytes"])
               for subscriber_id in table.ids}
    total_bytes = sum(weights.values())
    if total_bytes <= 0:
        return _refused("byte_share", "no_delivered_bytes", full_cost,
                        {"weights": weights})
    raw = {subscriber_id: Fraction(full_cost * 100 * weights[subscriber_id],
                                   total_bytes)
           for subscriber_id in table.ids}
    rows = _allocate_cents(raw, full_cost * 100)
    evidence = {
        "charge_rule": "body_bytes_proportional",
        "weight_source": "executor_success_response_bytes_verified_against_local_fixture",
        "weights": weights,
        "total_bytes": total_bytes,
        "full_cost_cents": full_cost * 100,
        "rounding": "floor_each_share_then_largest_remainder_by_subscriber_id",
    }
    return _check_and_sign("byte_share", rows, table, full_cost, evidence=evidence)


def marginal_share(table: CoalitionTable) -> dict[str, Any]:
    full_cost = table.cost(table.full)
    ids = list(table.ids)
    totals = {subscriber_id: Fraction(0) for subscriber_id in ids}
    orders = list(permutations(ids))
    for order in orders:
        for position, subscriber_id in enumerate(order):
            prefix = list(order[:position])
            before = table.cost(table.mask_for(prefix))
            with_member = table.cost(table.mask_for([*prefix, subscriber_id]))
            totals[subscriber_id] += with_member - before
    count = len(orders)
    exact = {subscriber_id: totals[subscriber_id] / count for subscriber_id in ids}
    raw = {subscriber_id: value * 100 for subscriber_id, value in exact.items()}
    rows = _allocate_cents(raw, full_cost * 100)
    evidence = {
        "charge_rule": "session_coalition_shapley",
        "weight_source": "exact_shapley_over_recomputed_subset_costs",
        "permutations": count,
        "exact_marginals": {
            key: str(exact[key]) for key in ids
        },
        "subset_costs_used": {
            "+".join(table.ids_for(mask)) or "empty": int(table.costs[mask])
            for mask in range(table.size) if table.costs[mask] is not None
        },
        "rounding": "floor_each_share_then_largest_remainder_by_subscriber_id",
        "note": ("each marginal is c(prefix + member) - c(prefix) with the "
                 "subset re-planned from scratch; not c(all) - c(all minus member)"),
    }
    return _check_and_sign("marginal_share", rows, table, full_cost, evidence=evidence)


def settle(
    table: CoalitionTable,
    verified: Mapping[str, Mapping[str, Any]],
) -> dict[str, Any]:
    contracts = {
        "byte_share": byte_share(table.parsed, table, verified),
        "marginal_share": marginal_share(table),
    }
    refusals = [
        {"contract": name, "reason": result["reason"], "evidence": result.get("evidence")}
        for name, result in contracts.items() if not result["signed"]
    ]
    return {
        "contracts": contracts,
        "refusals": refusals,
        "all_signed": not refusals,
        "full_cost_cents": table.cost(table.full) * 100,
        "conservation": {
            name: {
                "signed": result["signed"],
                "sum_cents": result["total_cents"],
                "full_cost_cents": result["full_cost_cents"],
                "conserved": result["signed"]
                and result["total_cents"] == result["full_cost_cents"],
            }
            for name, result in contracts.items()
        },
    }


__all__ = [
    "BillingError",
    "byte_share",
    "marginal_share",
    "settle",
    "solo_prices",
]
