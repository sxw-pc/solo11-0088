"""Exact session routing and coalition costs for synthetic subscriber billing.

Every session starts and ends at the depot, follows a directed navigation
matrix, and serves at most ``session_capacity`` subscribers.  A coalition cost
is recomputed from scratch for every subset; it is never derived by deleting a
stop from the full route.
"""

from __future__ import annotations

from itertools import permutations
from typing import Any, Iterable, Mapping

from .billing_inputs import (
    MAX_SESSION_CAPACITY,
    BillingError,
    ParsedInputs,
    billing_error,
    capacity_value,
    parse_inputs,
)


def route_for(
    dist: Mapping[str, Mapping[str, int]], depot: str, stops: Iterable[str]
) -> tuple[int, tuple[str, ...]] | None:
    """Minimum directed depot -> stops -> depot route for one session."""
    unique = sorted(set(stops))
    if not unique:
        return (0, ())
    best: tuple[int, tuple[str, ...]] | None = None
    for order in permutations(unique):
        total = 0
        previous = depot
        feasible = True
        for stop in order:
            edge = dist.get(previous, {}).get(stop)
            if edge is None:
                feasible = False
                break
            total += edge
            previous = stop
        if not feasible:
            continue
        back = dist.get(previous, {}).get(depot)
        if back is None:
            continue
        candidate = (total + back, order)
        if best is None or candidate < best:
            best = candidate
    return best


class CoalitionTable:
    """Exact cost, route partition, and proof for every subscriber subset."""

    def __init__(self, parsed: ParsedInputs) -> None:
        self.parsed = parsed
        self.ids = parsed.ids
        self.index = {subscriber_id: idx for idx, subscriber_id in enumerate(self.ids)}
        self.size = 1 << len(self.ids)
        self.full = self.size - 1
        self.costs: list[int | None] = [None] * self.size
        self.groups: list[int] = [0] * self.size
        self._routes: dict[int, tuple[int, tuple[str, ...]]] = {0: (0, ())}
        self._partitions = 0
        self.costs[0] = 0
        for mask in range(1, self.size):
            self._solve(mask)

    def _route_for_mask(self, mask: int) -> tuple[int, tuple[str, ...]] | None:
        if mask not in self._routes:
            stops = [self.parsed.subscribers[i].stop
                     for i in range(len(self.ids)) if mask & (1 << i)]
            route = route_for(self.parsed.dist, self.parsed.depot, stops)
            if route is not None:
                self._routes[mask] = route
        return self._routes.get(mask)

    def _solve(self, mask: int) -> None:
        lowest = mask & -mask
        submask = mask
        best: tuple[int, tuple[tuple[str, ...], ...]] | None = None
        best_group = 0
        while submask:
            self._partitions += 1
            if (submask & lowest) and submask.bit_count() <= self.parsed.capacity:
                route = self._route_for_mask(submask)
                rest = mask ^ submask
                if route is not None and self.costs[rest] is not None:
                    signature = tuple(sorted(self._signature(rest) + (route[1],)))
                    candidate = (int(self.costs[rest]) + route[0], signature)
                    if best is None or candidate < best:
                        best, best_group = candidate, submask
            submask = (submask - 1) & mask
        if best is not None:
            self.costs[mask] = best[0]
            self.groups[mask] = best_group

    def _signature(self, mask: int) -> tuple[tuple[str, ...], ...]:
        groups: list[tuple[str, ...]] = []
        remaining = mask
        while remaining:
            group = self.groups[remaining]
            route = self._route_for_mask(group)
            groups.append(route[1] if route else ())
            remaining ^= group
        return tuple(groups)

    def cost(self, mask: int) -> int:
        value = self.costs[mask]
        if value is None:
            raise billing_error(
                "billing_no_legal_route", "no legal session route covers the coalition",
                subscriber_ids=self.ids_for(mask),
            )
        return int(value)

    def mask_for(self, subscriber_ids: Iterable[str]) -> int:
        mask = 0
        for subscriber_id in subscriber_ids:
            if subscriber_id not in self.index:
                raise billing_error("billing_unknown_subscriber", "unknown subscriber",
                                    subscriber_id=subscriber_id)
            mask |= 1 << self.index[subscriber_id]
        return mask

    def ids_for(self, mask: int) -> list[str]:
        return [self.ids[i] for i in range(len(self.ids)) if mask & (1 << i)]

    def route_of(self, mask: int) -> tuple[int, tuple[str, ...]]:
        route = self._route_for_mask(mask)
        if route is None:
            raise billing_error("billing_no_legal_route",
                                "no legal session route for the group",
                                subscriber_ids=self.ids_for(mask))
        return route

    def group_masks(self, mask: int) -> list[int]:
        self.cost(mask)
        groups: list[int] = []
        remaining = mask
        while remaining:
            group = self.groups[remaining]
            if not group:
                raise billing_error("billing_no_legal_route",
                                    "coalition plan is incomplete", mask=mask)
            groups.append(group)
            remaining ^= group
        groups.sort(key=lambda group: self.route_of(group)[1])
        return groups

    def plan(self, mask: int) -> dict[str, Any]:
        total = self.cost(mask)
        sessions: list[dict[str, Any]] = []
        for number, group in enumerate(self.group_masks(mask), start=1):
            cost, order = self.route_of(group)
            path = [self.parsed.depot, *order, self.parsed.depot]
            legs = [{"from": source, "to": target,
                     "cost": self.parsed.dist[source][target]}
                    for source, target in zip(path, path[1:])]
            sessions.append({
                "session_index": number,
                "subscriber_ids": sorted(self.ids_for(group)),
                "stops": list(order),
                "route": path,
                "navigation_cost": cost,
                "capacity": self.parsed.capacity,
                "served_count": len(self.ids_for(group)),
                "legs": legs,
            })
        all_ids = sorted(self.ids_for(mask))
        return {
            "subscribers": all_ids,
            "subscriber_count": len(all_ids),
            "session_capacity": self.parsed.capacity,
            "total_cost": total,
            "sessions": sessions,
            "proof": {
                "method": "exhaustive_session_partition_and_directed_route",
                "optimal": True,
                "note": ("cost is the minimum over every partition into sessions of "
                         "size <= session_capacity and every directed stop order; "
                         "it is recomputed for this coalition, not derived by "
                         "deleting a stop from the full route"),
                "partitions_evaluated": self._partitions,
                "distinct_layer_routes": len(self._routes),
            },
        }

    def subset_costs(self) -> list[dict[str, Any]]:
        rows = []
        for mask in range(self.size):
            ids = sorted(self.ids_for(mask))
            rows.append({
                "subscribers": ids,
                "key": "+".join(ids) if ids else "empty",
                "cost": self.costs[mask],
                "feasible": self.costs[mask] is not None,
            })
        rows.sort(key=lambda row: (len(row["subscribers"]), row["subscribers"]))
        return rows


def _table(data: Mapping[str, Any], capacity: int | None) -> CoalitionTable:
    parsed = parse_inputs(data)
    if capacity is not None:
        parsed = ParsedInputs(parsed.depot, capacity_value(capacity),
                              parsed.subscribers, parsed.dist)
    return CoalitionTable(parsed)


def coalition_cost(data: Mapping[str, Any], subscriber_ids: Iterable[str],
                   *, capacity: int | None = None) -> int:
    table = _table(data, capacity)
    return table.cost(table.mask_for(subscriber_ids))


def coalition_costs(data: Mapping[str, Any], *,
                    capacity: int | None = None) -> dict[frozenset[str], int]:
    table = _table(data, capacity)
    return {frozenset(table.ids_for(mask)): int(table.costs[mask])
            for mask in range(table.size) if table.costs[mask] is not None}


def optimal_plan(data: Mapping[str, Any], subscriber_ids: Iterable[str] | None = None,
                 *, capacity: int | None = None) -> dict[str, Any]:
    table = _table(data, capacity)
    mask = table.full if subscriber_ids is None else table.mask_for(subscriber_ids)
    return table.plan(mask)


__all__ = [
    "BillingError",
    "CoalitionTable",
    "MAX_SESSION_CAPACITY",
    "coalition_cost",
    "coalition_costs",
    "optimal_plan",
    "parse_inputs",
    "route_for",
]
