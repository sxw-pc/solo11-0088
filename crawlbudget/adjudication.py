"""Resume admission: adjudicate persisted dispatch intents against receipts.

A dispatch intent is persisted before its HTTP attempt. If the process stops
between the remote response and the local event write, the intent dangles. On
resume this module asks the site for a verifiable receipt of that exact
identity and then settles, re-dispatches, or quarantines the amount:

- verified receipt + audit contract: settle the consumed amount while keeping
  the truthful "response not delivered" conclusion;
- verified receipt + recovery contract: retrieve the body under the same
  identity, verify it against the receipt, and mark a recovered delivery
  without recording new collection consumption;
- definitive signed absence: the amount was never spent; dispatch may resume;
- unverifiable credential or identity/ownership conflict: quarantine the
  amount so an empty local event ledger is never read as "unspent".
"""

from __future__ import annotations

from typing import Any, Callable

from .errors import EXEC_RECEIPT_CONFLICT, EXEC_RECEIPT_UNVERIFIABLE
from .models import BlockedEvent, DispatchIntent, RequestEvent
from .receipts import (
    CONTRACT_RECOVER,
    RECEIPT_CONTRACTS,
    VERDICT_CONFLICT,
    VERDICT_NOT_RECEIVED,
    VERDICT_RECEIVED,
    SiteReceiptVerifier,
    make_intent_id,
    receipt_evidence,
)
from .storage import Storage
from .util import sha256_bytes


class ReceiptAdjudicator:
    """Decides whether a resumed request dispatches, settles, or quarantines."""

    def __init__(
        self,
        storage: Storage,
        sites: dict[str, Any],
        *,
        stats: dict[str, int],
        clock: Callable[[Any, int], str],
        contract: str = "audit",
        session: Any | None = None,
        verifier: Any | None = None,
    ) -> None:
        if contract not in RECEIPT_CONTRACTS:
            raise ValueError(f"unknown receipt contract: {contract}")
        self.storage = storage
        self.sites = sites
        self.stats = stats
        self.clock = clock
        self.contract = contract
        self.session = session
        self._verifier = verifier

    # -- pre-dispatch persistent intent ----------------------------------

    def persist_intent(self, request: Any, attempt: int) -> dict[str, Any] | None:
        """Record the intent before dispatch; None means do not dispatch."""
        intent = DispatchIntent(
            intent_id=make_intent_id(
                request.run_id, request.site_id, request.job_id,
                request.url_hash, attempt, request.lease_id,
            ),
            run_id=request.run_id, site_id=request.site_id,
            job_id=request.job_id, url_hash=request.url_hash, attempt=attempt,
            lease_id=request.lease_id, url=request.url,
            created_at=self.clock(request, attempt),
        )
        row, conflict = self.storage.record_intent(intent)
        if conflict:
            self._quarantine(request, row, EXEC_RECEIPT_CONFLICT,
                             "receipt_identity_conflict", {
                                 "persisted_intent_id": row["intent_id"],
                                 "persisted_lease_id": row["lease_id"],
                                 "current_lease_id": request.lease_id,
                             })
            return None
        if row["state"] != "dispatched":
            return None  # already resolved; the recorded event governs
        return row

    def settle_intent(self, intent: dict[str, Any], event: RequestEvent) -> None:
        self.storage.resolve_intent(
            intent["intent_id"], state="settled", resolution=event.outcome,
            resolved_at=event.recorded_at,
        )

    # -- resume admission -------------------------------------------------

    def reconcile_recorded(self, run_id: str) -> None:
        """Settle dangling intents whose events are already recorded.

        A crash between the event write and the intent settle (or between the
        planned-row completion and the settle) leaves a dispatched intent
        whose outcome is fully known locally; no site query is needed.
        """
        events = self.storage.get_request_events(run_id)
        by_key = {
            (event["site_id"], event["url_hash"], int(event["attempt"])): event
            for event in events
        }
        for intent in self.storage.get_intents(run_id, state="dispatched"):
            key = (intent["site_id"], intent["url_hash"], int(intent["attempt"]))
            event = by_key.get(key)
            if event is not None:
                self.storage.resolve_intent(
                    intent["intent_id"], state="settled",
                    resolution="event_recorded",
                    resolved_at=event["recorded_at"],
                )

    def admit(self, request: Any, existing: list[dict[str, Any]]) -> bool:
        """Resolve dangling intents; False means the request is finalized."""
        recorded = {int(event["attempt"]) for event in existing}
        for intent in self._dangling(request):
            if int(intent["attempt"]) in recorded:
                self.storage.resolve_intent(
                    intent["intent_id"], state="settled",
                    resolution="event_recorded",
                    resolved_at=self.clock(request, int(intent["attempt"])),
                )
                continue
            if not self._verify_and_settle(request, intent):
                return False
        return True

    def _dangling(self, request: Any) -> list[dict[str, Any]]:
        return [
            intent
            for intent in self.storage.get_intents(request.run_id, state="dispatched")
            if intent["site_id"] == request.site_id
            and intent["url_hash"] == request.url_hash
        ]

    def _verify_and_settle(self, request: Any, intent: dict[str, Any]) -> bool:
        conflict = self._identity_conflict(request, intent)
        if conflict:
            self._quarantine(request, intent, EXEC_RECEIPT_CONFLICT,
                             "receipt_identity_conflict", conflict)
            return False
        verdict = self._get_verifier().verify(intent)
        if verdict.kind == VERDICT_NOT_RECEIVED:
            self.storage.resolve_intent(
                intent["intent_id"], state="voided", resolution="not_received",
                resolved_at=self.clock(request, int(intent["attempt"])),
            )
            return True
        if verdict.kind == VERDICT_CONFLICT:
            self._quarantine(request, intent, EXEC_RECEIPT_CONFLICT,
                             "receipt_identity_conflict",
                             {"receipt": verdict.receipt or {},
                              "detail": verdict.detail})
            return False
        if verdict.kind != VERDICT_RECEIVED:
            self._quarantine(request, intent, EXEC_RECEIPT_UNVERIFIABLE,
                             "receipt_unverifiable", {"detail": verdict.detail})
            return False
        receipt = verdict.receipt or {}
        note = ""
        if self.contract == CONTRACT_RECOVER:
            fetched = self._get_verifier().fetch_body(intent, receipt)
            if fetched is None:
                note = "recovery_unavailable"
            else:
                body, content_type = fetched
                if sha256_bytes(body) == str(receipt.get("body_sha256", "")):
                    self._settle_recovered(request, intent, receipt, body,
                                           content_type)
                    return False
                note = "recovery_body_mismatch"
        self._settle_undelivered(request, intent, receipt, note)
        return False

    def _identity_conflict(self, request: Any, intent: dict[str, Any]) -> dict[str, Any]:
        mismatches: dict[str, Any] = {}
        expected = make_intent_id(
            request.run_id, request.site_id, request.job_id, request.url_hash,
            int(intent["attempt"]), request.lease_id,
        )
        if intent["intent_id"] != expected:
            mismatches["intent_id"] = [intent["intent_id"], expected]
        for field, current in (("job_id", request.job_id),
                               ("lease_id", request.lease_id),
                               ("url", request.url)):
            if intent[field] != current:
                mismatches[field] = [intent[field], current]
        return mismatches

    # -- settlements -------------------------------------------------------

    def _settle_recovered(self, request: Any, intent: dict[str, Any],
                          receipt: dict[str, Any], body: bytes,
                          content_type: str) -> None:
        attempt = int(intent["attempt"])
        status = receipt.get("status")
        event = self._event(
            request, attempt,
            int(status) if status is not None else None,
            "recovered_delivery",
            response_bytes=len(body), content_type=content_type,
            body_sha256=sha256_bytes(body),
            evidence={
                "delivery": "recovered",
                "original_response": "not_recorded_locally",
                "receipt": receipt_evidence(receipt),
            },
        )
        self._finalize(request, intent, event, "recovered_delivery", receipt)
        self.stats["completed"] += 1

    def _settle_undelivered(self, request: Any, intent: dict[str, Any],
                            receipt: dict[str, Any], note: str) -> None:
        evidence: dict[str, Any] = {
            "delivery": "not_delivered",
            "settlement": "audit",
            "receipt": receipt_evidence(receipt),
        }
        if note:
            evidence["recovery_note"] = note
        event = self._event(request, int(intent["attempt"]), None,
                            "receipt_undelivered", evidence=evidence)
        self._finalize(request, intent, event, "receipt_undelivered", receipt)
        self.stats["completed"] += 1

    def _finalize(self, request: Any, intent: dict[str, Any],
                  event: RequestEvent, outcome: str,
                  receipt: dict[str, Any]) -> None:
        self.storage.insert_request_event(event)
        self.storage.complete_planned(request.run_id, request.site_id,
                                      request.url_hash, outcome)
        self.storage.resolve_intent(intent["intent_id"], state="settled",
                                    resolution=outcome, receipt=receipt,
                                    resolved_at=event.recorded_at)

    def _quarantine(self, request: Any, intent: dict[str, Any],
                    error_code: str, reason: str,
                    detail: dict[str, Any]) -> None:
        attempt = int(intent["attempt"])
        evidence = {
            "intent_id": intent["intent_id"], "lease_id": intent["lease_id"],
            "attempt": attempt, **detail,
        }
        self.storage.insert_blocked(BlockedEvent(
            run_id=request.run_id, site_id=request.site_id,
            job_id=request.job_id, url_hash=request.url_hash, url=request.url,
            stage="execute", reason=reason, evidence=evidence,
            recorded_at=self.clock(request, attempt),
        ))
        event = self._event(request, attempt, None, "receipt_quarantined",
                            error_code=error_code, evidence=evidence)
        self.storage.insert_request_event(event)
        self.storage.complete_planned(request.run_id, request.site_id,
                                      request.url_hash, "receipt_quarantined")
        receipt = detail.get("receipt")
        self.storage.resolve_intent(
            intent["intent_id"], state="quarantined", resolution=reason,
            receipt=receipt if isinstance(receipt, dict) else {},
            resolved_at=event.recorded_at,
        )
        self.stats["blocked"] += 1

    # -- helpers -----------------------------------------------------------

    def _event(self, request: Any, attempt: int, status_code: int | None,
               outcome: str, *, response_bytes: int = 0, content_type: str = "",
               body_sha256: str = "", error_code: str | None = None,
               evidence: dict[str, Any] | None = None) -> RequestEvent:
        return RequestEvent(
            run_id=request.run_id, site_id=request.site_id,
            job_id=request.job_id, url_hash=request.url_hash, attempt=attempt,
            url=request.url, status_code=status_code, outcome=outcome,
            elapsed_ms=0, response_bytes=response_bytes,
            content_type=content_type, body_sha256=body_sha256,
            error_code=error_code, evidence=evidence or {},
            recorded_at=self.clock(request, attempt),
        )

    def _get_verifier(self) -> Any:
        if self._verifier is None:
            self._verifier = SiteReceiptVerifier(self.session, self.sites)
        return self._verifier
