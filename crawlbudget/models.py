"""Typed records used across planning, execution, storage, and reporting."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from .util import normalize_path


@dataclass(frozen=True)
class SiteConfig:
    site_id: str
    base_url: str
    robots_file: Path
    daily_quota: int
    max_requests_per_run: int
    max_rps: float
    burst: int
    crawl_delay_ms: int
    max_response_bytes: int
    max_retries: int = 0
    timeout_ms: int = 2000
    enabled: bool = True
    robots_ref: str = ""

    @property
    def parsed_base(self):
        return urlsplit(self.base_url)

    @property
    def host(self) -> str:
        return (self.parsed_base.hostname or "").lower()

    @property
    def port(self) -> int:
        return self.parsed_base.port or 80

    @property
    def base_path(self) -> str:
        return normalize_path(self.parsed_base.path)


@dataclass(frozen=True)
class JobConfig:
    job_id: str
    site_id: str
    priority: int
    seed_file: Path
    enabled: bool = True


@dataclass
class ConfigBundle:
    sites: dict[str, SiteConfig]
    jobs: tuple[JobConfig, ...]
    config_fingerprint: str
    seeds_fingerprint: str
    robots_fingerprint: str
    source_path: Path


@dataclass(frozen=True)
class RobotsRule:
    rule_id: str
    directive: str
    pattern: str
    user_agents: tuple[str, ...]
    group_id: str
    specificity: int


@dataclass
class RobotsMatch:
    allowed: bool
    path: str
    reason: str
    rule: RobotsRule | None = None
    group_user_agent: str = ""
    note: str = ""

    def evidence(self) -> dict[str, Any]:
        rule = self.rule
        return {
            "decision": "allow" if self.allowed else "disallow",
            "reason": self.reason,
            "path": self.path,
            "rule_id": rule.rule_id if rule else None,
            "pattern": rule.pattern if rule else None,
            "group_user_agent": self.group_user_agent,
            "note": self.note,
        }


@dataclass
class RobotsPolicy:
    site_id: str
    rules: tuple[RobotsRule, ...]
    crawl_delay_ms: int
    matched_user_agent: str
    warnings: tuple[dict[str, str], ...] = ()
    available: bool = True
    unavailable_reason: str = ""

    def evaluate(self, path: str) -> RobotsMatch:
        if not self.available:
            return RobotsMatch(
                allowed=False,
                path=path,
                reason="robots_missing",
                note=self.unavailable_reason or "robots fixture unavailable",
            )
        matches = [rule for rule in self.rules if _rule_matches(rule.pattern, path)]
        if not matches:
            return RobotsMatch(allowed=True, path=path, reason="allowed_no_rule")
        matches.sort(
            key=lambda rule: (
                -rule.specificity,
                0 if rule.directive == "allow" else 1,
                rule.rule_id,
            )
        )
        winner = matches[0]
        return RobotsMatch(
            allowed=winner.directive == "allow",
            path=path,
            reason="allowed_by_rule" if winner.directive == "allow" else "disallowed_by_rule",
            rule=winner,
            group_user_agent=", ".join(winner.user_agents),
        )


def _rule_matches(pattern: str, path: str) -> bool:
    import re

    anchored = pattern.endswith("$")
    body = pattern[:-1] if anchored else pattern
    regex = "".join(".*" if char == "*" else re.escape(char) for char in body)
    if anchored:
        regex += "$"
    return re.match("^" + regex, path) is not None


@dataclass(frozen=True)
class ScopeOutcome:
    allowed: bool
    original_url: str
    normalized_url: str
    url_hash: str
    reason: str | None = None
    detail: str = ""

    def evidence(self) -> dict[str, Any]:
        return {
            "reason": self.reason,
            "detail": self.detail,
            "normalized_url": self.normalized_url,
            "url_hash": self.url_hash,
        }


@dataclass
class Lease:
    lease_id: str
    run_id: str
    site_id: str
    job_id: str
    granted_requests: int
    used_requests: int
    remaining_requests: int
    expires_at: str
    reason: str

@dataclass
class PlannedRequest:
    run_id: str
    site_id: str
    job_id: str
    request_order: int
    url: str
    url_hash: str
    lease_id: str
    allowed: bool
    block_reason: str | None
    block_evidence: dict[str, Any]
    virtual_offset_ms: int | None
    robots_rule_id: str | None = None
    robots_evidence: dict[str, Any] = field(default_factory=dict)
    seeded_url: str = ""
    completed: bool = False
    final_outcome: str | None = None
    attempt: int = 1

    def as_dict(self) -> dict[str, Any]:
        keys = (
            "request_order", "site_id", "job_id", "url", "url_hash", "lease_id",
            "allowed", "block_reason", "block_evidence", "virtual_offset_ms",
            "robots_rule_id", "robots_evidence",
        )
        return {key: getattr(self, key) for key in keys}


@dataclass
class Plan:
    run_id: str
    requests: list[PlannedRequest]
    leases: list[Lease]
    warnings: list[dict[str, Any]]
    quota_used_before: dict[str, int]
    attempt_limits: dict[str, int]
    virtual_start: str

@dataclass
class RequestEvent:
    run_id: str
    site_id: str
    job_id: str
    url_hash: str
    attempt: int
    url: str
    status_code: int | None
    outcome: str
    elapsed_ms: int
    response_bytes: int
    content_type: str
    body_sha256: str
    error_code: str | None
    evidence: dict[str, Any]
    recorded_at: str

@dataclass
class BlockedEvent:
    run_id: str
    site_id: str
    job_id: str
    url_hash: str
    url: str
    stage: str
    reason: str
    evidence: dict[str, Any]
    recorded_at: str

@dataclass
class WarningRecord:
    run_id: str
    code: str
    message: str
    context: dict[str, Any]
    recorded_at: str

@dataclass
class RunRecord:
    run_id: str
    created_at: str
    started_at: str
    finished_at: str
    virtual_date: str
    dry_run: bool
    status: str
    config_fingerprint: str
    seeds_fingerprint: str
    robots_fingerprint: str
    report_fingerprint: str = ""


TERMINAL_OUTCOMES = frozenset({
    "success", "http_error", "redirect_blocked", "body_too_large",
    "transport_error_final", "preflight_blocked", "budget_exhausted",
})
