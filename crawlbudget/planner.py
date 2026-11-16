"""Deterministic planning across scope, robots, quotas, leases, and rate time."""

from __future__ import annotations

from pathlib import Path
from urllib.parse import urlsplit

from .budget import (
    VirtualRateLimiter,
    choose_lease_reason,
    compute_lease_expiry,
    make_lease_id,
    quota_available,
    slot_block_reason,
)
from .errors import BUDGET_JOB_DISABLED, BUDGET_SITE_DISABLED, ROBOTS_DISALLOW
from .models import ConfigBundle, JobConfig, Lease, PlannedRequest, Plan, RobotsMatch
from .robots import load_robots
from .scope import scope_url
from .util import read_fixture_text

WARNING_FIELDS = ("code", "message", "context")


def load_seed_urls(path: Path) -> list[str]:
    return [
        line.strip()
        for line in read_fixture_text(path).splitlines()
        if line.strip() and not line.strip().startswith("#")
    ]


def _warning(code: str, message: str, **context: object) -> dict[str, object]:
    return {"code": code, "message": message, "context": context}


def _duplicate_warning(job: JobConfig, url: str, scope: str) -> dict[str, object]:
    return _warning(
        "SEED_DUPLICATE", f"duplicate seed skipped for {job.job_id}",
        job_id=job.job_id, site_id=job.site_id, scope=scope, url=url,
    )


def _read_seeds(job: JobConfig, warnings: list[dict[str, object]]) -> list[str]:
    try:
        return load_seed_urls(job.seed_file)
    except (OSError, UnicodeDecodeError) as exc:
        warnings.append(_warning(
            "SEED_FILE_UNREADABLE", f"cannot read seed file for {job.job_id}: {exc}",
            job_id=job.job_id,
        ))
        return []


def _blocked_reason(
    job: JobConfig, site, scope, robots: RobotsMatch | None
) -> str | None:
    if not job.enabled:
        return BUDGET_JOB_DISABLED
    if not site.enabled:
        return BUDGET_SITE_DISABLED
    if not scope.allowed:
        return scope.reason
    if robots is not None and not robots.allowed:
        return ROBOTS_DISALLOW if robots.rule else "robots_missing"
    return None


def build_plan(
    config: ConfigBundle,
    run_id: str,
    virtual_start: str,
    quota_used_before: dict[str, int] | None = None,
) -> Plan:
    prior = {sid: int((quota_used_before or {}).get(sid, 0)) for sid in config.sites}
    robots = {sid: load_robots(site) for sid, site in config.sites.items()}
    warnings: list[dict[str, object]] = [
        _warning(
            str(item.get("code", "ROBOTS_WARNING")), str(item.get("message", "")),
            site_id=site_id, line=item.get("line", 0),
        )
        for site_id in sorted(robots) for item in robots[site_id].warnings
    ]
    jobs = sorted(config.jobs, key=lambda job: (job.priority, job.job_id))
    requests: list[PlannedRequest] = []
    leases: list[Lease] = []
    seen_global: set[tuple[str, str]] = set()
    lease_by_job: dict[str, Lease] = {}
    eligible_count: dict[str, int] = {}
    block_reasons: dict[str, list[str]] = {}
    order = 0
    for job in jobs:
        site = config.sites[job.site_id]
        lease = Lease(
            lease_id=make_lease_id(run_id, job.site_id, job.job_id),
            run_id=run_id,
            site_id=job.site_id,
            job_id=job.job_id,
            granted_requests=0,
            used_requests=0,
            remaining_requests=0,
            expires_at=compute_lease_expiry(virtual_start),
            reason="pending",
        )
        leases.append(lease)
        lease_by_job[job.job_id] = lease
        seen_local: set[str] = set()
        for raw_url in _read_seeds(job, warnings):
            if raw_url in seen_local:
                warnings.append(_duplicate_warning(job, raw_url, "same_job"))
                continue
            seen_local.add(raw_url)
            scope = scope_url(raw_url, site)
            key = (job.site_id, scope.url_hash)
            if key in seen_global:
                warnings.append(_duplicate_warning(job, raw_url, "across_jobs"))
                continue
            seen_global.add(key)
            robots_match = None
            if scope.allowed:
                robots_match = robots[site.site_id].evaluate(
                    urlsplit(scope.normalized_url).path
                )
            reason = _blocked_reason(job, site, scope, robots_match)
            order += 1
            if reason in {BUDGET_JOB_DISABLED, BUDGET_SITE_DISABLED}:
                evidence = {
                    "scope": scope.evidence(), "job_enabled": job.enabled,
                    "site_enabled": site.enabled,
                }
            elif robots_match is not None:
                evidence = {"scope": scope.evidence(), "robots": robots_match.evidence()}
            else:
                evidence = scope.evidence()
            requests.append(PlannedRequest(
                run_id=run_id,
                site_id=job.site_id,
                job_id=job.job_id,
                request_order=order,
                url=scope.normalized_url or raw_url.strip(),
                url_hash=scope.url_hash,
                lease_id=lease.lease_id,
                allowed=False,
                block_reason=reason,
                block_evidence=evidence,
                virtual_offset_ms=None,
                robots_rule_id=robots_match.rule.rule_id
                if robots_match is not None and robots_match.rule else None,
                robots_evidence=robots_match.evidence() if robots_match is not None else {},
                seeded_url=raw_url,
            ))
            if reason is None:
                eligible_count[job.job_id] = eligible_count.get(job.job_id, 0) + 1
            else:
                block_reasons.setdefault(job.job_id, []).append(reason)

    attempt_limits: dict[str, int] = {}
    for site_id in sorted(config.sites):
        site = config.sites[site_id]
        quota_left = quota_available(site.daily_quota, prior[site_id])
        run_limit = min(site.max_requests_per_run, quota_left)
        attempt_limits[site_id] = run_limit
        eligible = [
            request for request in requests
            if request.site_id == site_id
            and request.allowed is False and request.block_reason is None
        ]
        grant_count = min(len(eligible), run_limit)
        limiter = VirtualRateLimiter(
            site.max_rps, site.burst,
            max(site.crawl_delay_ms, robots[site_id].crawl_delay_ms),
        )
        for index, request in enumerate(eligible):
            if index < grant_count:
                request.allowed = True
                request.virtual_offset_ms = limiter.next_offset_ms()
            else:
                request.block_reason = slot_block_reason(
                    index, grant_count, quota_left=quota_left,
                    run_limit=site.max_requests_per_run,
                ) or "quota_exceeded"
                request.block_evidence = {
                    "eligible_index": index, "granted_count": grant_count,
                    "quota_left": quota_left, "run_limit": site.max_requests_per_run,
                }

    for job in jobs:
        lease = lease_by_job[job.job_id]
        granted = sum(1 for request in requests
                      if request.job_id == job.job_id and request.allowed)
        site = config.sites[job.site_id]
        lease.granted_requests = lease.remaining_requests = granted
        lease.reason = choose_lease_reason(
            granted=granted,
            eligible=eligible_count.get(job.job_id, 0),
            job_enabled=job.enabled,
            site_enabled=site.enabled,
            quota_left=quota_available(site.daily_quota, prior[job.site_id]),
            run_limit=site.max_requests_per_run,
            block_reasons=block_reasons.get(job.job_id, []),
        )

    unique: dict[tuple[object, object, str], dict[str, object]] = {}
    for item in warnings:
        key = (item.get("code"), item.get("message"), repr(item.get("context", {})))
        unique.setdefault(key, item)
    return Plan(
        run_id=run_id,
        requests=sorted(requests, key=lambda request: request.request_order),
        leases=leases,
        warnings=list(unique.values()),
        quota_used_before=prior,
        attempt_limits=attempt_limits,
        virtual_start=virtual_start,
    )
