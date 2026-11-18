"""Validated input records for exact coalition-based subscriber billing."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

MAX_SUBSCRIBERS = 6
MAX_SESSION_CAPACITY = 3


class BillingError(ValueError):
    """A deterministic, machine-readable billing input or settlement error."""

    def __init__(self, code: str, message: str, evidence: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.evidence = evidence or {}

    def as_dict(self) -> dict[str, Any]:
        return {"code": self.code, "message": self.message, "evidence": self.evidence}


def billing_error(code: str, message: str, **evidence: Any) -> BillingError:
    return BillingError(code, message, evidence)


@dataclass(frozen=True)
class Subscriber:
    subscriber_id: str
    site_id: str
    stop: str
    page_path: str
    body_bytes: int
    body_sha256: str


@dataclass(frozen=True)
class ParsedInputs:
    depot: str
    capacity: int
    subscribers: tuple[Subscriber, ...]
    dist: Mapping[str, Mapping[str, int]]

    @property
    def ids(self) -> tuple[str, ...]:
        return tuple(subscriber.subscriber_id for subscriber in self.subscribers)

    @property
    def by_id(self) -> dict[str, Subscriber]:
        return {subscriber.subscriber_id: subscriber for subscriber in self.subscribers}


def int_field(value: Any, field: str, *, minimum: int | None = None) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise billing_error("billing_invalid_input", f"{field} must be an integer", field=field)
    if minimum is not None and value < minimum:
        raise billing_error(
            "billing_invalid_input", f"{field} must be >= {minimum}",
            field=field, value=value, minimum=minimum,
        )
    return value


def capacity_value(value: Any) -> int:
    """Validate a session capacity against the supported search bound."""
    capacity = int_field(value, "session_capacity", minimum=1)
    if capacity > MAX_SESSION_CAPACITY:
        raise billing_error(
            "billing_capacity_exceeded",
            f"session_capacity exceeds the supported bound of {MAX_SESSION_CAPACITY}",
            capacity=capacity, limit=MAX_SESSION_CAPACITY,
        )
    return capacity


def parse_inputs(data: Mapping[str, Any]) -> ParsedInputs:
    """Validate the intake fixture and return a canonical, sorted view."""
    if not isinstance(data, Mapping):
        raise billing_error("billing_invalid_input", "intake must be a JSON object")
    depot = data.get("depot")
    if not isinstance(depot, str) or not depot:
        raise billing_error("billing_invalid_input", "depot must be a non-empty string")
    capacity = capacity_value(data.get("session_capacity", MAX_SESSION_CAPACITY))
    raw_subscribers = data.get("subscribers")
    if not isinstance(raw_subscribers, list):
        raise billing_error("billing_invalid_input", "subscribers must be a list")
    if len(raw_subscribers) > MAX_SUBSCRIBERS:
        raise billing_error(
            "billing_too_many_subscribers",
            f"at most {MAX_SUBSCRIBERS} subscribers are supported",
            count=len(raw_subscribers), limit=MAX_SUBSCRIBERS,
        )
    dist = parse_dist(data.get("navigation_cost"), depot)
    subscribers: list[Subscriber] = []
    seen: set[str] = set()
    for index, raw in enumerate(raw_subscribers):
        subscriber = parse_subscriber(raw, index, dist)
        if subscriber.subscriber_id in seen:
            raise billing_error(
                "billing_duplicate_subscriber",
                f"duplicate subscriber_id {subscriber.subscriber_id}",
                subscriber_id=subscriber.subscriber_id, index=index,
            )
        seen.add(subscriber.subscriber_id)
        subscribers.append(subscriber)
    subscribers.sort(key=lambda item: item.subscriber_id)
    return ParsedInputs(depot=depot, capacity=capacity,
                        subscribers=tuple(subscribers), dist=dist)


def parse_dist(raw: Any, depot: str) -> dict[str, dict[str, int]]:
    if not isinstance(raw, Mapping):
        raise billing_error("billing_invalid_input", "navigation_cost must be an object")
    dist: dict[str, dict[str, int]] = {}
    for source, edges in raw.items():
        if not isinstance(source, str) or not isinstance(edges, Mapping):
            raise billing_error("billing_invalid_input",
                                "navigation_cost entries must be mappings")
        dist[source] = {}
        for target, value in edges.items():
            if not isinstance(target, str):
                raise billing_error("billing_invalid_input",
                                    "navigation_cost target must be a string")
            if isinstance(value, bool) or not isinstance(value, int):
                raise billing_error(
                    "billing_invalid_input", "navigation_cost values must be integers",
                    source=source, target=target, value=value,
                )
            if value < 0:
                raise billing_error(
                    "billing_negative_cost", "negative navigation costs are not billable",
                    source=source, target=target, value=value,
                )
            dist[source][target] = value
    if depot not in dist:
        raise billing_error("billing_invalid_input",
                            "depot has no navigation edges", depot=depot)
    return dist


def parse_subscriber(raw: Any, index: int,
                     dist: Mapping[str, Mapping[str, int]]) -> Subscriber:
    if not isinstance(raw, Mapping):
        raise billing_error("billing_invalid_input",
                            "subscriber entry must be a mapping", index=index)
    for field in ("subscriber_id", "site_id", "stop", "page_path", "body_sha256"):
        if not isinstance(raw.get(field), str):
            raise billing_error("billing_invalid_input",
                                f"subscriber {field} must be a string", index=index)
    if not isinstance(raw.get("body_bytes"), int) or isinstance(raw.get("body_bytes"), bool):
        raise billing_error("billing_invalid_input",
                            "subscriber body_bytes must be an integer", index=index)
    subscriber_id = str(raw["subscriber_id"])
    stop = str(raw["stop"])
    if not subscriber_id or not stop:
        raise billing_error("billing_invalid_input",
                            "subscriber_id and stop must be non-empty", index=index)
    if stop not in dist:
        raise billing_error(
            "billing_unknown_stop", f"stop {stop} has no navigation node",
            subscriber_id=subscriber_id, stop=stop,
        )
    return Subscriber(
        subscriber_id=subscriber_id,
        site_id=str(raw["site_id"]),
        stop=stop,
        page_path=str(raw["page_path"]),
        body_bytes=int_field(raw["body_bytes"], "body_bytes", minimum=0),
        body_sha256=str(raw["body_sha256"]),
    )
