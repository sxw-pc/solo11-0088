import pytest

from crawlbudget.budget import (
    VirtualRateLimiter, attempt_limit, choose_lease_reason, compute_lease_expiry,
    make_lease_id, pacing_floor_ms, quota_available, slot_block_reason,
)
from crawlbudget.errors import (
    BUDGET_GRANTED, BUDGET_JOB_DISABLED, BUDGET_QUOTA_EXCEEDED, BUDGET_RUN_LIMIT_EXCEEDED,
)


@pytest.mark.parametrize(
    "max_rps, burst, crawl_delay_ms, count, expected",
    [
        (10, 2, 0, 4, [0, 0, 100, 200]),  # burst, then conservative refill
        (10, 3, 30, 4, [0, 30, 60, 100]),  # crawl delay is a hard floor
        (3, 1, 0, 3, [0, 334, 668]),  # fractional rate rounds the interval up
    ],
)
def test_token_bucket_schedule(max_rps, burst, crawl_delay_ms, count, expected):
    limiter = VirtualRateLimiter(max_rps=max_rps, burst=burst, crawl_delay_ms=crawl_delay_ms)
    assert limiter.schedule(count) == expected


def test_schedule_is_reproducible_and_pacing_floor_matches_rate():
    assert VirtualRateLimiter(20, 3, 20).schedule(8) == VirtualRateLimiter(20, 3, 20).schedule(8)
    assert pacing_floor_ms(3, 0) == 334


def test_quota_and_attempt_limits():
    assert (quota_available(10, 3), quota_available(3, 9)) == (7, 0)
    assert [attempt_limit(10, used, 5) for used in (3, 8, 20)] == [5, 2, 0]


@pytest.mark.parametrize(
    "used, limit, quota_left, run_limit, expected",
    [
        (0, 2, 5, 5, None),
        (2, 2, 5, 5, BUDGET_RUN_LIMIT_EXCEEDED),
        (1, 1, 1, 5, BUDGET_QUOTA_EXCEEDED),
    ],
)
def test_slot_block_reason_precedence(used, limit, quota_left, run_limit, expected):
    assert slot_block_reason(used, limit, quota_left=quota_left, run_limit=run_limit) == expected


def test_lease_id_expiry_and_reason_selection():
    first = make_lease_id("run-1", "mock-alpha", "job-1")
    assert first == make_lease_id("run-1", "mock-alpha", "job-1")
    assert first.startswith("lease-")
    assert compute_lease_expiry("2026-01-15T12:30:00Z") == "2026-01-16T00:00:00Z"
    base = dict(eligible=2, job_enabled=True, site_enabled=True, quota_left=5,
                run_limit=5, block_reasons=[])
    assert choose_lease_reason(granted=2, **base) == BUDGET_GRANTED
    assert choose_lease_reason(
        granted=0, **{**base, "job_enabled": False, "block_reasons": ["robots_disallow"]}
    ) == BUDGET_JOB_DISABLED
    assert choose_lease_reason(
        granted=0, **{**base, "eligible": 3, "quota_left": 0, "block_reasons": ["robots_disallow"]}
    ) == BUDGET_QUOTA_EXCEEDED
    assert choose_lease_reason(granted=0, **{**base, "run_limit": 0}) == BUDGET_RUN_LIMIT_EXCEEDED
