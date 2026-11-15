"""Deterministic token-bucket scheduling and lease helpers."""

from __future__ import annotations

import math
from collections import Counter

from .errors import (
    BUDGET_GRANTED,
    BUDGET_JOB_DISABLED,
    BUDGET_QUOTA_EXCEEDED,
    BUDGET_RUN_LIMIT_EXCEEDED,
    BUDGET_SITE_DISABLED,
)
from .util import lease_expiry, parse_utc, sha256_text


class VirtualRateLimiter:
    """Integer-only token bucket evaluated against a virtual millisecond clock.

    The refill interval is ceil(1000 / max_rps), which is conservative for
    fractional rates (it never exceeds the configured rate). Crawl delay is a
    hard lower bound between two grants from the same site.
    """

    def __init__(self, max_rps: float, burst: int, crawl_delay_ms: int = 0) -> None:
        if max_rps <= 0:
            raise ValueError("max_rps must be positive")
        if burst <= 0:
            raise ValueError("burst must be positive")
        self.max_rps = float(max_rps)
        self.burst = int(burst)
        self.crawl_delay_ms = max(0, int(crawl_delay_ms))
        self.interval_ms = max(1, int(math.ceil(1000.0 / self.max_rps)))
        self.tokens = self.burst
        self.now_ms = 0
        self.last_request_ms: int | None = None
        self.last_refill_ms = 0
        self.carry_ms = 0

    def _refill(self, at_ms: int) -> None:
        elapsed = max(0, at_ms - self.last_refill_ms)
        total = self.carry_ms + elapsed
        gained, self.carry_ms = divmod(total, self.interval_ms)
        self.tokens = min(self.burst, self.tokens + gained)
        self.last_refill_ms = at_ms

    def next_offset_ms(self) -> int:
        at_ms = self.now_ms
        if self.last_request_ms is not None:
            at_ms = max(at_ms, self.last_request_ms + self.crawl_delay_ms)
        self._refill(at_ms)
        if self.tokens == 0:
            wait_ms = self.interval_ms - self.carry_ms
            if wait_ms <= 0:
                wait_ms = self.interval_ms
            at_ms += wait_ms
            self._refill(at_ms)
            if self.tokens == 0:
                self.tokens = 1
        self.tokens -= 1
        self.now_ms = at_ms
        self.last_request_ms = at_ms
        return at_ms

    def schedule(self, count: int) -> list[int]:
        if count < 0:
            raise ValueError("count must be non-negative")
        return [self.next_offset_ms() for _ in range(count)]


def quota_available(daily_quota: int, used_before: int) -> int:
    return max(0, int(daily_quota) - max(0, int(used_before)))


def attempt_limit(daily_quota: int, used_before: int, max_requests_per_run: int) -> int:
    """Maximum HTTP attempts allowed for this run after prior daily usage."""
    return min(int(max_requests_per_run), quota_available(daily_quota, used_before))


def slot_block_reason(
    eligible_index: int,
    granted_count: int,
    *,
    quota_left: int,
    run_limit: int,
) -> str | None:
    """Return None for a granted slot, otherwise the limiting reason."""
    if eligible_index < granted_count:
        return None
    if run_limit <= quota_left:
        return BUDGET_RUN_LIMIT_EXCEEDED
    return BUDGET_QUOTA_EXCEEDED


def make_lease_id(run_id: str, site_id: str, job_id: str) -> str:
    digest = sha256_text(f"{run_id}\x1f{site_id}\x1f{job_id}")
    return f"lease-{digest[:16]}"


def compute_lease_expiry(virtual_start: str) -> str:
    return lease_expiry(parse_utc(virtual_start))


def choose_lease_reason(
    *,
    granted: int,
    eligible: int,
    job_enabled: bool,
    site_enabled: bool,
    quota_left: int,
    run_limit: int,
    block_reasons: list[str],
) -> str:
    if not job_enabled:
        return BUDGET_JOB_DISABLED
    if not site_enabled:
        return BUDGET_SITE_DISABLED
    if granted > 0:
        return BUDGET_GRANTED
    if eligible == 0:
        return (Counter(block_reasons).most_common(1)[0][0] if block_reasons
                else "no_eligible_requests")
    if quota_left <= 0:
        return BUDGET_QUOTA_EXCEEDED
    if run_limit <= 0:
        return BUDGET_RUN_LIMIT_EXCEEDED
    return Counter(block_reasons).most_common(1)[0][0] if block_reasons else "no_eligible_requests"


def pacing_floor_ms(max_rps: float, crawl_delay_ms: int) -> int:
    return max(int(crawl_delay_ms), max(1, int(math.ceil(1000.0 / float(max_rps)))))
