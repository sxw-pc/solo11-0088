import json
from collections import Counter

import pytest

from crawlbudget.budget import make_lease_id
from crawlbudget.errors import BUDGET_JOB_DISABLED, BUDGET_SITE_DISABLED, BUDGET_GRANTED
from crawlbudget.planner import build_plan


def test_demo_plan_counts_ordering_limits_and_gamma(demo_plan):
    assert len(demo_plan.requests) == 21
    assert sum(request.allowed for request in demo_plan.requests) == 14
    reasons = Counter(r.block_reason for r in demo_plan.requests if r.block_reason)
    assert reasons == {
        "robots_disallow": 3, "quota_exceeded": 1, "run_limit_exceeded": 1,
        "scope_out_of_site": 1, "scope_non_loopback": 1,
    }
    assert demo_plan.attempt_limits == {"mock-alpha": 6, "mock-beta": 10, "mock-gamma": 2}
    assert [r.request_order for r in demo_plan.requests] == list(range(1, 22))
    for site_id in ("mock-alpha", "mock-beta", "mock-gamma"):
        offsets = [r.virtual_offset_ms for r in demo_plan.requests
                   if r.site_id == site_id and r.allowed]
        assert offsets == sorted(offsets)
    gamma = [r for r in demo_plan.requests if r.site_id == "mock-gamma"]
    assert [r.allowed for r in gamma] == [True, True, False, False]
    assert gamma[2].block_reason == "run_limit_exceeded"
    assert gamma[3].block_reason == "robots_disallow"


def test_demo_plan_leases_and_serialization_are_deterministic(demo_plan):
    assert [lease.granted_requests for lease in demo_plan.leases] == [6, 6, 2]
    for lease in demo_plan.leases:
        assert lease.lease_id == make_lease_id(demo_plan.run_id, lease.site_id, lease.job_id)
        assert lease.reason == BUDGET_GRANTED
        assert lease.expires_at == "2026-01-16T00:00:00Z"
    first = json.dumps([r.as_dict() for r in demo_plan.requests], sort_keys=True)
    second = json.dumps([r.as_dict() for r in demo_plan.requests], sort_keys=True)
    assert first == second


def test_robots_evidence_and_missing_robots_conservatism(demo_plan, config_factory):
    note = next(r for r in demo_plan.requests if r.url.endswith("/private/public-note.html"))
    assert note.allowed and note.robots_rule_id == "R002"
    assert note.robots_evidence["pattern"].endswith("$")
    blocked = next(r for r in demo_plan.requests if r.url.endswith("/private/secret.html"))
    assert blocked.block_reason == "robots_disallow"
    assert blocked.robots_evidence["decision"] == "disallow"
    def mutate(raw):
        raw["sites"][0]["robots_file"] = "missing-robots.txt"
    config, path, _raw = config_factory(mutate)
    (path.parent / "missing-robots.txt").unlink(missing_ok=True)
    plan = build_plan(config, "no-robots", "2026-01-15T00:00:00Z")
    alpha = [r for r in plan.requests if r.site_id == "mock-alpha"]
    roboted = [r for r in alpha if r.block_reason == "robots_missing"]
    assert roboted and all(r.robots_evidence["decision"] == "disallow" for r in roboted)


def test_prior_daily_quota_reduces_the_alpha_lease(demo_config):
    plan = build_plan(demo_config, "quota-run", "2026-01-15T00:00:00Z",
                      {"mock-alpha": 5, "mock-beta": 0, "mock-gamma": 0})
    alpha = [r for r in plan.requests if r.site_id == "mock-alpha"]
    assert sum(r.allowed for r in alpha) == 1
    assert next(l for l in plan.leases if l.site_id == "mock-alpha").reason == BUDGET_GRANTED
    blocked = Counter(r.block_reason for r in alpha if r.block_reason)
    assert blocked["quota_exceeded"] == 6


@pytest.mark.parametrize("target, index, expected_reason, site_id", [
    ("job", 0, BUDGET_JOB_DISABLED, "mock-alpha"),
    ("site", 1, BUDGET_SITE_DISABLED, "mock-beta"),
])
def test_disabled_job_or_site_is_blocked_without_network(
    config_factory, target, index, expected_reason, site_id
):
    def mutate(raw):
        raw[f"{target}s"][index]["enabled"] = False
    config, _path, _raw = config_factory(mutate)
    plan = build_plan(config, f"disabled-{target}", "2026-01-15T00:00:00Z")
    requests = [r for r in plan.requests if r.site_id == site_id]
    assert requests and all(r.block_reason == expected_reason for r in requests)
    lease = next(l for l in plan.leases if l.site_id == site_id)
    assert lease.reason == expected_reason and lease.granted_requests == 0


def test_duplicate_seed_warns_and_is_skipped(config_factory, tmp_path):
    seed = tmp_path / "dupes.txt"
    seed.write_text(
        "http://127.0.0.1:5021/mock-alpha/index.html\n"
        "http://127.0.0.1:5021/mock-alpha/index.html\n"
        "http://127.0.0.1:5021/mock-alpha/docs/page-1.html\n",
        encoding="utf-8",
    )
    def mutate(raw):
        raw["jobs"][0]["seed_file"] = str(seed)
    config, _path, _raw = config_factory(mutate)
    plan = build_plan(config, "dupes", "2026-01-15T00:00:00Z")
    alpha = [r for r in plan.requests if r.site_id == "mock-alpha"]
    assert [r.url.rsplit("/", 1)[-1] for r in alpha] == ["index.html", "page-1.html"]
    assert any(w["code"] == "SEED_DUPLICATE" for w in plan.warnings)
