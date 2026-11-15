"""Loopback-only, budget-aware, resumable HTTP collection."""

from __future__ import annotations

import time
from datetime import timedelta
from typing import Any, Callable
from urllib.parse import urljoin, urlsplit

import requests

from .budget import pacing_floor_ms, quota_available
from .errors import (
    BUDGET_QUOTA_EXCEEDED,
    BUDGET_RUN_LIMIT_EXCEEDED,
    EXEC_BODY_TOO_LARGE,
    EXEC_PREFLIGHT_BLOCKED,
    EXEC_REDIRECT_BLOCKED,
    EXEC_TIMEOUT,
    EXEC_TRANSPORT_ERROR,
)
from .models import BlockedEvent, Lease, Plan, RequestEvent, SiteConfig, TERMINAL_OUTCOMES
from .scope import external_location, scope_url
from .storage import Storage
from .util import elapsed_ms, iso_z, parse_utc, sha256_bytes

RETRYABLE_STATUSES = frozenset({408, 425, 429, 500, 502, 503, 504})
USER_AGENT = "crawlbudget/1.0 (+local-only)"


class ControlledExecutor:
    def __init__(
        self,
        storage: Storage,
        sites: dict[str, SiteConfig],
        plan: Plan,
        *,
        session: Any | None = None,
        sleeper: Callable[[float], None] | None = None,
    ) -> None:
        self.storage = storage
        self.sites = sites
        self.plan = plan
        self.session = session or requests.Session()
        if session is None:
            self.session.trust_env = False
        self.sleeper = sleeper or time.sleep
        self._start = parse_utc(plan.virtual_start)
        self._paced: set[str] = set()
        self.stats = {"attempts": 0, "completed": 0, "skipped": 0, "blocked": 0}

    def run(self) -> dict[str, int]:
        rows = {
            (row["site_id"], row["url_hash"]): row
            for row in self.storage.get_planned(self.plan.run_id)
        }
        for request in sorted(self.plan.requests, key=lambda item: item.request_order):
            if not request.allowed:
                continue
            row = rows.get((request.site_id, request.url_hash))
            if row is not None and row["completed"]:
                self.stats["skipped"] += 1
                continue
            self._execute_request(request, row)
        return dict(self.stats)

    def _execute_request(self, request, row: dict[str, Any] | None) -> None:
        site = self.sites[request.site_id]
        recheck = scope_url(request.url, site)
        if not recheck.allowed:
            self._block(
                request, "execute", EXEC_PREFLIGHT_BLOCKED,
                {"scope_reason": recheck.reason, "scope_detail": recheck.detail},
               request.virtual_offset_ms or 0,
            )
            self._complete(request, "preflight_blocked")
            self.stats["blocked"] += 1
            return
        existing = self.storage.get_request_events(
            request.run_id, site_id=request.site_id, url_hash=request.url_hash
        )
        if existing and existing[-1]["outcome"] in TERMINAL_OUTCOMES:
            self._complete(request, existing[-1]["outcome"])
            self.stats["skipped"] += 1
            return
        max_attempts = site.max_retries + 1
        attempt = (existing[-1]["attempt"] + 1) if existing else 1
        while attempt <= max_attempts:
            remaining = self._remaining_attempts(site)
            if remaining <= 0:
                reason = self._budget_reason(site)
                self._block(
                    request, "execute", reason,
                    {"attempt": attempt, "remaining_attempts": 0},
                    (request.virtual_offset_ms or 0) + attempt * 1000,
                )
                self._complete(request, "budget_exhausted")
                self.stats["blocked"] += 1
                return
            self._pace(site)
            outcome, terminal, event = self._attempt(request, site, attempt)
            self.storage.insert_request_event(event)
            self.stats["attempts"] += 1
            if terminal:
                self._complete(request, outcome)
                self.stats["completed"] += 1
                return
            attempt += 1
        self._complete(request, "http_error")
        self.stats["completed"] += 1

    def _complete(self, request, outcome: str) -> None:
        self.storage.complete_planned(
            request.run_id, request.site_id, request.url_hash, outcome
        )

    def _attempt(self, request, site: SiteConfig, attempt: int) -> tuple[str, bool, RequestEvent]:
        started = time.monotonic()
        status_code: int | None = None
        try:
            response = self.session.get(
                request.url,
                timeout=site.timeout_ms / 1000.0,
                allow_redirects=False,
                stream=True,
                headers={"User-Agent": USER_AGENT},
            )
            status_code = int(response.status_code)
            elapsed = elapsed_ms(started)
            if 300 <= status_code <= 399:
                return self._redirect_attempt(request, site, attempt, response, elapsed)
            if 200 <= status_code <= 299:
                return self._body_attempt(request, site, attempt, response, elapsed)
            if status_code in RETRYABLE_STATUSES and attempt < site.max_retries + 1:
                event = self._event(
                    request, attempt, status_code, "retryable_error", elapsed,
                    error_code=EXEC_TRANSPORT_ERROR, evidence={"retryable_status": status_code},
                )
                response.close()
                return "retryable_error", False, event
            event = self._event(
                request, attempt, status_code, "http_error", elapsed,
                error_code=None, evidence={"status": status_code},
            )
            response.close()
            return "http_error", True, event
        except requests.RequestException as exc:
            elapsed = elapsed_ms(started)
            terminal = attempt >= site.max_retries + 1
            timeout = isinstance(exc, requests.Timeout)
            outcome = "transport_error_final" if terminal else (
                "retryable_timeout" if timeout else "retryable_transport"
            )
            evidence = {"retryable": not terminal}
            if not timeout:
                evidence["exception"] = type(exc).__name__
            event = self._event(
                request, attempt, status_code, outcome, elapsed,
                error_code=EXEC_TIMEOUT if timeout else EXEC_TRANSPORT_ERROR,
                evidence=evidence,
            )
            return outcome, terminal, event

    def _redirect_attempt(self, request, site, attempt, response, elapsed):
        location = str(response.headers.get("Location", ""))
        target = urljoin(request.url, location) if location else ""
        target_host = (urlsplit(target).hostname or "") if target else ""
        reason = "redirect_non_loopback" if external_location(target) else "redirect_blocked"
        evidence = {
            "reason": reason, "status": int(response.status_code), "location": location,
            "target_host": target_host, "followed": False,
        }
        self._block(
            request, "execute", reason, evidence,
            (request.virtual_offset_ms or 0) + attempt * 1000,
        )
        event = self._event(
            request, attempt, int(response.status_code), "redirect_blocked", elapsed,
            error_code=EXEC_REDIRECT_BLOCKED, evidence=evidence,
        )
        response.close()
        self.stats["blocked"] += 1
        return "redirect_blocked", True, event

    def _body_attempt(self, request, site, attempt, response, elapsed):
        declared = response.headers.get("Content-Length")
        if declared is not None and str(declared).isdigit() and int(declared) > site.max_response_bytes:
            return self._oversized(request, attempt, response, elapsed, int(declared), True)
        chunks: list[bytes] = []
        total = 0
        try:
            for chunk in response.iter_content(chunk_size=4096):
                if not chunk:
                    continue
                total += len(chunk)
                if total > site.max_response_bytes:
                    return self._oversized(request, attempt, response, elapsed, total, False)
                chunks.append(chunk)
        finally:
            response.close()
        body = b"".join(chunks)
        content_type = str(response.headers.get("Content-Type", ""))
        event = self._event(
            request, attempt, int(response.status_code), "success", elapsed,
            response_bytes=len(body), content_type=content_type,
            body_sha256=sha256_bytes(body),
            evidence={"status": int(response.status_code), "retained_bytes": len(body)},
        )
        return "success", True, event

    def _oversized(self, request, attempt, response, elapsed, observed, declared):
        status = int(response.status_code)
        self._block(
            request, "execute", "response_body_too_large",
            {"status": status, "observed_bytes": observed,
             "limit": self.sites[request.site_id].max_response_bytes,
             "declared_content_length": declared, "retained": False},
            (request.virtual_offset_ms or 0) + attempt * 1000,
        )
        self.stats["blocked"] += 1
        event = self._event(
            request, attempt, status, "body_too_large", elapsed,
            error_code=EXEC_BODY_TOO_LARGE,
            evidence={"observed_bytes": observed,
                      "limit": self.sites[request.site_id].max_response_bytes},
        )
        response.close()
        return "body_too_large", True, event

    def _event(
        self, request, attempt: int, status_code: int | None, outcome: str,
        elapsed_ms: int, *, response_bytes: int = 0, content_type: str = "",
        body_sha256: str = "", error_code: str | None = None,
        evidence: dict[str, Any] | None = None,
    ) -> RequestEvent:
        offset = (request.virtual_offset_ms or 0) + (attempt - 1) * 1000
        recorded = iso_z(self._start + timedelta(milliseconds=offset))
        return RequestEvent(
            run_id=request.run_id, site_id=request.site_id, job_id=request.job_id,
            url_hash=request.url_hash, attempt=attempt, url=request.url,
            status_code=status_code, outcome=outcome, elapsed_ms=elapsed_ms,
            response_bytes=response_bytes, content_type=content_type,
            body_sha256=body_sha256, error_code=error_code,
            evidence=evidence or {}, recorded_at=recorded,
        )

    def _block(self, request, stage: str, reason: str, evidence: dict[str, Any], offset: int):
        recorded = iso_z(self._start + timedelta(milliseconds=offset))
        self.storage.insert_blocked(
            BlockedEvent(
                run_id=request.run_id, site_id=request.site_id, job_id=request.job_id,
                url_hash=request.url_hash, url=request.url, stage=stage,
                reason=reason, evidence=evidence, recorded_at=recorded,
            )
        )

    def _pace(self, site: SiteConfig) -> None:
        if site.site_id in self._paced:
            self.sleeper(pacing_floor_ms(site.max_rps, site.crawl_delay_ms) / 1000.0)
        self._paced.add(site.site_id)

    def _remaining_attempts(self, site: SiteConfig) -> int:
        used = self.storage.attempt_count(self.plan.run_id, site.site_id)
        allowed = self.plan.attempt_limits.get(site.site_id, 0)
        return max(0, allowed - used)

    def _budget_reason(self, site: SiteConfig) -> str:
        prior = self.plan.quota_used_before.get(site.site_id, 0)
        daily_left = quota_available(site.daily_quota, prior)
        if daily_left <= site.max_requests_per_run:
            return BUDGET_QUOTA_EXCEEDED
        return BUDGET_RUN_LIMIT_EXCEEDED
