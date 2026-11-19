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
    RECEIPT_QUARANTINE,
)
from .models import BlockedEvent, Plan, RequestEvent, SiteConfig, TERMINAL_OUTCOMES
from .receipt import (
    DELIVERY_NOT_DELIVERED,
    DELIVERY_RECOVERED,
    DISPATCH_HEADER,
    INTENT_DISPATCHED,
    INTENT_QUARANTINED,
    INTENT_SETTLED,
    INTENT_SUPERSEDED,
    RECEIPT_AUDIT,
    RECEIPT_DEFINITELY_ABSENT,
    RECEIPT_IDENTITY_CONFLICT,
    RECEIPT_MATCHED,
    RECEIPT_REDELIVER,
    RECEIPT_STATE_AUDIT_SETTLED,
    RECEIPT_STATE_QUARANTINED,
    RECEIPT_STATE_RECORDED,
    RECEIPT_STATE_REDELIVERED,
    RECEIPT_UNVERIFIABLE,
    DispatchIntent,
    ReceiptVerdict,
    UrlReceiptClient,
    make_dispatch_id,
    verify_intent,
)
from .scope import external_location, scope_url
from .storage import Storage
from .util import canonical_json, elapsed_ms, iso_z, parse_utc, sha256_bytes

RETRYABLE_STATUSES = frozenset({408, 425, 429, 500, 502, 503, 504})
USER_AGENT = "crawlbudget/1.0 (+local-only)"

# Authorization contracts for a matched remote receipt whose body never
# reached the local ledger.  Audit is the safe default: settle spend without
# ever claiming a delivery the process did not retain.
RECEIPT_CONTRACTS = frozenset({RECEIPT_AUDIT, RECEIPT_REDELIVER})


class ControlledExecutor:
    def __init__(
        self,
        storage: Storage,
        sites: dict[str, SiteConfig],
        plan: Plan,
        *,
        session: Any | None = None,
        sleeper: Callable[[float], None] | None = None,
        receipt_contract: str = RECEIPT_AUDIT,
        receipt_sources: dict[str, Any] | None = None,
    ) -> None:
        if receipt_contract not in RECEIPT_CONTRACTS:
            raise ValueError(f"unknown receipt contract: {receipt_contract}")
        self.storage = storage
        self.sites = sites
        self.plan = plan
        self.session = session or requests.Session()
        if session is None:
            self.session.trust_env = False
        self.sleeper = sleeper or time.sleep
        self.receipt_contract = receipt_contract
        # Injected sources (e.g. offline fakes) keyed by site id. When absent
        # the executor builds an HTTP client against the mock site endpoints.
        self.receipt_sources = dict(receipt_sources or {})
        self._start = parse_utc(plan.virtual_start)
        self._paced: set[str] = set()
        self.stats = {"attempts": 0, "completed": 0, "skipped": 0, "blocked": 0,
                      "receipt_verified": 0, "receipt_quarantined": 0}

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

        # Resume admission: an in-flight intent means a GET may already have
        # reached the site. Verify the remote claim before spending budget on
        # a second GET for the same identity.
        open_intents = self._open_intents_for(request)
        if open_intents and self._admit_or_settle(request, site, open_intents):
            return

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
            intent = self._persist_intent(request, attempt)
            outcome, terminal, event = self._attempt(request, site, attempt, intent)
            self.storage.insert_request_event(event)
            self.stats["attempts"] += 1
            if terminal:
                # The local response is now durably recorded: the pre-dispatch
                # identity is settled and any later receipt probe is moot.
                self.storage.update_dispatch_intent_state(
                    intent.dispatch_id, INTENT_SETTLED, self._now(request, attempt)
                )
                self._complete(request, outcome)
                self.stats["completed"] += 1
                return
            # A non-terminal attempt (retryable failure) still consumed one
            # site attempt. Supersede its identity so the next retry gets a
            # fresh one rather than inheriting an ambiguous receipt.
            self.storage.update_dispatch_intent_state(
                intent.dispatch_id, INTENT_SUPERSEDED, self._now(request, attempt)
            )
            attempt += 1
        self._complete(request, "http_error")
        self.stats["completed"] += 1

    def _open_intents_for(self, request) -> list[dict[str, Any]]:
        return [
            row for row in self.storage.get_open_dispatch_intents(request.run_id)
            if row["site_id"] == request.site_id and row["url_hash"] == request.url_hash
        ]

    def _admit_or_settle(self, request, site: SiteConfig,
                         intents: list[dict[str, Any]]) -> bool:
        """Verify stored dispatch identities before re-sending a GET.

        Returns True when the request was finalized (settled via a receipt or
        quarantined), False when the admission gate allows a fresh dispatch.
        """
        source = self._receipt_source(request, site)
        for row in intents:
            intent = DispatchIntent(
                dispatch_id=row["dispatch_id"], run_id=row["run_id"],
                site_id=row["site_id"], job_id=row["job_id"],
                url_hash=row["url_hash"], url=row["url"], attempt=row["attempt"],
                lease_id=row["lease_id"], state=row["state"],
            )
            verdict = verify_intent(source, intent)
            if verdict.decision == RECEIPT_DEFINITELY_ABSENT:
                # The site answers that this identity never arrived. Retire
                # the stale identity and let the normal loop re-dispatch.
                self.storage.update_dispatch_intent_state(
                    intent.dispatch_id, INTENT_SUPERSEDED,
                    iso_z(parse_utc(self.plan.virtual_start)),
                )
                continue
            if verdict.decision == RECEIPT_MATCHED:
                self._settle_receipt(request, site, intent, verdict.receipt)
                return True
            self._quarantine_receipt(request, site, intent, verdict)
            return True
        return False

    def _settle_receipt(self, request, site: SiteConfig, intent: DispatchIntent,
                        receipt: Any) -> None:
        """Settle a verified remote receipt under the chosen authorization."""
        recorded_at = iso_z(parse_utc(self.plan.virtual_start))
        self.storage.insert_dispatch_receipt({
            "dispatch_id": intent.dispatch_id, "run_id": intent.run_id,
            "site_id": intent.site_id, "job_id": intent.job_id,
            "url_hash": intent.url_hash, "attempt": intent.attempt,
            "lease_id": intent.lease_id, "receipt_state": RECEIPT_STATE_RECORDED,
            "delivery_conclusion": DELIVERY_NOT_DELIVERED,
            "receipt_json": canonical_json(receipt.as_dict()),
            "recorded_at": recorded_at,
        })
        self.stats["receipt_verified"] += 1
        if self.receipt_contract == RECEIPT_REDELIVER:
            self._redeliver(request, site, intent, receipt, recorded_at)
            return
        self._audit_settle(request, site, intent, receipt, recorded_at)

    def _audit_settle(self, request, site: SiteConfig, intent: DispatchIntent,
                      receipt: Any, recorded_at: str) -> None:
        """Audit contract: bill the spent quota, keep the undelivered truth."""
        event = self._event(
            request, intent.attempt, None, "receipt_audit_settled", 0,
            error_code=None,
            evidence={
                "receipt_dispatch_id": intent.dispatch_id,
                "remote_status_code": receipt.status_code,
                "remote_body_sha256": receipt.body_sha256,
                "remote_response_bytes": receipt.response_bytes,
                "delivery": DELIVERY_NOT_DELIVERED,
                "contract": RECEIPT_AUDIT,
                "note": "site accepted one collection; response was never stored locally",
            },
        )
        self.storage.insert_request_event(event)
        self.storage.update_dispatch_receipt(
            intent.dispatch_id, receipt_state=RECEIPT_STATE_AUDIT_SETTLED,
            delivery_conclusion=DELIVERY_NOT_DELIVERED, settled_at=recorded_at,
            recovery_contract=RECEIPT_AUDIT,
        )
        self.storage.update_dispatch_intent_state(
            intent.dispatch_id, INTENT_SETTLED, recorded_at
        )
        self._complete(request, "receipt_audit_settled")
        self.stats["completed"] += 1

    def _redeliver(self, request, site: SiteConfig, intent: DispatchIntent,
                   receipt: Any, recorded_at: str) -> None:
        """Re-delivery contract: recover the body with no new collection cost."""
        source = self._receipt_source(request, site)
        try:
            recovered = source.fetch_recovery(intent.site_id, intent.dispatch_id)
        except Exception as exc:
            # The receipt proves spend; a failed recovery cannot turn into a
            # re-dispatch that bills the site a second time.
            self._quarantine_receipt(
                request, site, intent,
                ReceiptVerdict(RECEIPT_UNVERIFIABLE, receipt=receipt,
                               detail=f"recovery failed: {exc}"),
            )
            return
        if recovered["body_sha256"] != receipt.body_sha256:
            self._quarantine_receipt(
                request, site, intent,
                ReceiptVerdict(RECEIPT_IDENTITY_CONFLICT, receipt=receipt,
                               detail="recovery body hash differs from receipt"),
            )
            return
        event = self._event(
            request, intent.attempt, int(recovered["status_code"]),
            "receipt_recovered", 0,
            response_bytes=int(recovered["response_bytes"]),
            content_type=str(recovered.get("content_type", "")),
            body_sha256=recovered["body_sha256"],
            evidence={
                "receipt_dispatch_id": intent.dispatch_id,
                "remote_status_code": receipt.status_code,
                "delivery": DELIVERY_RECOVERED,
                "contract": RECEIPT_REDELIVER,
                "new_collection_cost": 0,
                "note": "body recovered after receipt; original response was never stored",
            },
        )
        self.storage.insert_request_event(event)
        self.storage.update_dispatch_receipt(
            intent.dispatch_id, receipt_state=RECEIPT_STATE_REDELIVERED,
            delivery_conclusion=DELIVERY_RECOVERED, settled_at=recorded_at,
            recovery_contract=RECEIPT_REDELIVER,
            recovered_body_sha256=recovered["body_sha256"],
        )
        self.storage.update_dispatch_intent_state(
            intent.dispatch_id, INTENT_SETTLED, recorded_at
        )
        self._complete(request, "receipt_recovered")
        self.stats["completed"] += 1

    def _quarantine_receipt(self, request, site: SiteConfig,
                            intent: DispatchIntent, verdict: Any) -> None:
        """Isolate the quota: an unanswerable claim must not be re-spent."""
        recorded_at = iso_z(parse_utc(self.plan.virtual_start))
        receipt = verdict.receipt
        # The planned row is the authoritative lease binding. A tampered
        # intent must never move the isolated spend onto a phantom lease.
        owner_lease_id = self._lease_id_for(request)
        self.storage.upsert_dispatch_receipt({
            "dispatch_id": intent.dispatch_id, "run_id": intent.run_id,
            "site_id": intent.site_id, "job_id": request.job_id,
            "url_hash": intent.url_hash, "attempt": intent.attempt,
            "lease_id": owner_lease_id,
            "receipt_state": RECEIPT_STATE_QUARANTINED,
            "delivery_conclusion": DELIVERY_NOT_DELIVERED,
            "receipt_json": canonical_json(receipt.as_dict()) if receipt else "",
            "recorded_at": recorded_at, "settled_at": recorded_at,
            "conflict_detail": verdict.detail,
        })
        self._block(
            request, "execute", RECEIPT_QUARANTINE,
            {"receipt_dispatch_id": intent.dispatch_id,
             "verdict": verdict.decision, "detail": verdict.detail},
            (request.virtual_offset_ms or 0) + intent.attempt * 1000,
        )
        event = self._event(
            request, intent.attempt,
            receipt.status_code if receipt else None,
            "receipt_quarantined", 0,
            evidence={"receipt_dispatch_id": intent.dispatch_id,
                      "verdict": verdict.decision, "detail": verdict.detail,
                      "delivery": DELIVERY_NOT_DELIVERED},
        )
        self.storage.insert_request_event(event)
        self.storage.update_dispatch_intent_state(
            intent.dispatch_id, INTENT_QUARANTINED, recorded_at
        )
        self._complete(request, "receipt_quarantined")
        self.stats["blocked"] += 1
        self.stats["receipt_quarantined"] += 1

    def _persist_intent(self, request, attempt: int) -> DispatchIntent:
        lease_id = self._lease_id_for(request)
        dispatch_id = make_dispatch_id(
            request.run_id, request.site_id, request.job_id,
            request.url_hash, attempt, lease_id,
        )
        now = self._now(request, attempt)
        intent = DispatchIntent(
            dispatch_id=dispatch_id, run_id=request.run_id,
            site_id=request.site_id, job_id=request.job_id,
            url_hash=request.url_hash, url=request.url, attempt=attempt,
            lease_id=lease_id, state=INTENT_DISPATCHED,
        )
        self.storage.insert_dispatch_intent({
            "dispatch_id": dispatch_id, "run_id": request.run_id,
            "site_id": request.site_id, "job_id": request.job_id,
            "url_hash": request.url_hash, "url": request.url,
            "attempt": attempt, "lease_id": lease_id,
            "state": INTENT_DISPATCHED, "created_at": now, "updated_at": now,
        })
        return intent

    def _lease_id_for(self, request) -> str:
        rows = self.storage.get_planned(request.run_id)
        for row in rows:
            if row["site_id"] == request.site_id and row["url_hash"] == request.url_hash:
                return str(row["lease_id"])
        return str(request.lease_id)

    def _receipt_source(self, request, site: SiteConfig) -> Any:
        source = self.receipt_sources.get(request.site_id)
        if source is not None:
            return source
        return UrlReceiptClient(
            self.session, site.base_url, request.url, request.site_id,
            timeout_ms=site.timeout_ms,
        )

    def _now(self, request, attempt: int) -> str:
        offset = (request.virtual_offset_ms or 0) + (attempt - 1) * 1000
        return iso_z(self._start + timedelta(milliseconds=offset))

    def _complete(self, request, outcome: str) -> None:
        self.storage.complete_planned(
            request.run_id, request.site_id, request.url_hash, outcome
        )

    def _attempt(self, request, site: SiteConfig, attempt: int,
                 intent: DispatchIntent) -> tuple[str, bool, RequestEvent]:
        started = time.monotonic()
        status_code: int | None = None
        try:
            response = self.session.get(
                request.url,
                timeout=site.timeout_ms / 1000.0,
                allow_redirects=False,
                stream=True,
                headers={
                    "User-Agent": USER_AGENT,
                    DISPATCH_HEADER: intent.dispatch_id,
                    "X-CB-Run-Id": request.run_id,
                    "X-CB-Job-Id": request.job_id,
                    "X-CB-Url-Hash": request.url_hash,
                    "X-CB-Attempt": str(attempt),
                    "X-CB-Lease-Id": intent.lease_id,
                },
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
