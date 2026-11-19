"""Deterministic in-memory HTTP doubles for executor and report tests."""

import json
from pathlib import Path
from typing import Any

import requests

from crawlbudget.receipt import (
    RECEIPT_QUERY_MARK,
    RECEIPT_RECOVER_MARK,
    sign_receipt,
)
from crawlbudget.util import fixture_bytes, sha256_bytes

PAGES = Path(__file__).resolve().parent.parent / "fixtures" / "pages"


class FakeResponse:
    def __init__(self, status_code: int, *, body: bytes = b"",
                 headers: dict[str, str] | None = None) -> None:
        self.status_code = status_code
        self.body = body
        self.headers = headers or {}

    @property
    def content(self) -> bytes:
        return self.body

    def iter_content(self, chunk_size: int = 4096):
        for index in range(0, len(self.body), chunk_size):
            yield self.body[index:index + chunk_size]

    def close(self) -> None:
        pass


class ReplaySession:
    def __init__(self, routes: dict[str, list[FakeResponse]]) -> None:
        self.routes = {url: list(responses) for url, responses in routes.items()}
        self.calls: list[str] = []
        self.trust_env = False

    def get(self, url: str, **kwargs: Any) -> FakeResponse:
        if kwargs.get("allow_redirects") is not False:
            raise AssertionError("executor must disable automatic redirects")
        self.calls.append(url)
        responses = self.routes.get(url)
        if not responses:
            raise AssertionError(f"unexpected request: {url}")
        return responses.pop(0) if len(responses) > 1 else responses[0]


def _page(name: str, status: int = 200, **headers: str) -> FakeResponse:
    body = fixture_bytes(PAGES / name)
    merged = {"Content-Type": "text/html; charset=utf-8", "Content-Length": str(len(body))}
    merged.update(headers)
    return FakeResponse(status, body=body, headers=merged)


def demo_replay_session() -> ReplaySession:
    a = "http://127.0.0.1:5021/mock-alpha"
    b = "http://127.0.0.1:5022/mock-beta"
    g = "http://127.0.0.1:5023/mock-gamma"
    routes = {
        f"{a}/index.html": [_page("mock-alpha_page_index.html")],
        f"{a}/docs/page-1.html": [_page("mock-alpha_page_docs_1.html")],
        f"{a}/docs/page-2.html": [_page("mock-alpha_page_docs_2.html")],
        f"{a}/private/public-note.html": [_page("mock-alpha_page_private_public_note.html")],
        f"{a}/missing.html": [FakeResponse(404)],
        f"{a}/redirect-out.html": [
            FakeResponse(302, headers={"Location": "http://example.invalid/outside"})
        ],
        f"{b}/hidden/allowed/note.html": [_page("mock-beta_page_hidden_allowed_note.html")],
        f"{b}/public/a.html": [_page("mock-beta_page_public_a.html")],
        f"{b}/public/b.html": [_page("mock-beta_page_public_b.html")],
        f"{b}/flaky.html": [_page("mock-beta_page_flaky.html", 500),
                            _page("mock-beta_page_flaky.html")],
        f"{b}/always-fail.html": [_page("mock-beta_page_public_a.html", 500)] * 3,
        f"{b}/big.html": [_page("mock-beta_page_big.html")],
        f"{g}/public/one.html": [_page("mock-gamma_page_one.html")],
        f"{g}/public/two.html": [FakeResponse(404)],
    }
    return ReplaySession(routes)


class ReceiptAwareSession:
    """Offline mock site double with receipt, recovery, and fault injection.

    Modes, keyed by page URL suffix in ``faults``:

    * ``record`` (default): issue a signed receipt for the dispatched GET.
    * ``omit``: answer the GET but keep no receipt (definite absence probe).
    * ``corrupt``: issue a receipt whose HMAC signature is invalid.
    * ``drop``: the GET itself never answers, but the receipt was recorded,
      simulating loss between site acceptance and local persistence.
    * ``unverifiable``: the receipt endpoint raises instead of answering.
    """

    def __init__(
        self,
        pages: dict[str, FakeResponse],
        *,
        faults: dict[str, str] | None = None,
        site_id: str = "lease-demo",
    ) -> None:
        self.pages = dict(pages)
        self.faults = dict(faults or {})
        self.site_id = site_id
        self.calls: list[str] = []
        self.receipt_calls: list[str] = []
        self.recovery_calls: list[str] = []
        self.trust_env = False
        self._receipts: dict[str, dict[str, Any]] = {}
        self._bodies: dict[str, bytes] = {}
        # Pages whose process-level interruption already happened; a second
        # real GET to them is a test bug because resume must verify first.
        self.interrupted: set[str] = set()

    def get(self, url: str, **kwargs: Any) -> FakeResponse:
        if RECEIPT_QUERY_MARK in url:
            return self._lookup(url)
        if RECEIPT_RECOVER_MARK in url:
            return self._recover(url)
        suffix = self._suffix(url)
        if suffix in self.interrupted:
            raise AssertionError(
                f"resume re-dispatched {url} without verifying its receipt first"
            )
        self.calls.append(url)
        dispatch_id = kwargs["headers"]["X-CB-Dispatch-ID"]
        headers = dict(kwargs["headers"])
        mode = self.faults.get(suffix, "record")
        response = self.pages[url]
        body = response.body
        if mode == "drop":
            # The site accepted and recorded the collection, but the response
            # never reaches the client process. A later GET to this page is a
            # test bug: resume must verify the recorded receipt first.
            self._store(dispatch_id, url, headers, body, response.status_code)
            self.interrupted.add(suffix)
            raise _SilentLoss("simulated response loss after site acceptance")
        if mode == "omit":
            # GET answered, but deliberately no receipt is retained.
            return FakeResponse(response.status_code, body=body,
                                headers=dict(response.headers))
        self._store(dispatch_id, url, headers, body, response.status_code)
        if mode == "corrupt":
            self._receipts[dispatch_id]["signature"] = "f" * 64
        return FakeResponse(response.status_code, body=body,
                            headers={**response.headers, "X-CB-Dispatch-ID": dispatch_id})

    def _store(self, dispatch_id, url, headers, body, status_code) -> None:
        record = {
            "dispatch_id": dispatch_id,
            "site_id": self.site_id,
            "run_id": headers.get("X-CB-Run-Id", ""),
            "job_id": headers.get("X-CB-Job-Id", ""),
            "url_hash": headers.get("X-CB-Url-Hash", ""),
            "url": url,
            "attempt": int(headers.get("X-CB-Attempt", "1")),
            "lease_id": headers.get("X-CB-Lease-Id", ""),
            "status_code": int(status_code),
            "body_sha256": sha256_bytes(body),
            "response_bytes": len(body),
            "received_at": "2026-01-15T00:00:00Z",
        }
        record["signature"] = sign_receipt(self.site_id, dispatch_id)
        self._receipts[dispatch_id] = record
        self._bodies[dispatch_id] = body

    def _lookup(self, url: str) -> FakeResponse:
        self.receipt_calls.append(url)
        dispatch_id = url.rsplit("dispatch_id=", 1)[-1]
        suffix_mode = next(
            (mode for suffix, mode in self.faults.items() if mode == "unverifiable"), ""
        )
        if suffix_mode == "unverifiable":
            raise _ReceiptUnavailable("receipt endpoint cannot answer")
        record = self._receipts.get(dispatch_id)
        if record is None:
            return FakeResponse(
                404, body=json.dumps({"present": False}).encode("utf-8"),
                headers={"Content-Type": "application/json"},
            )
        return FakeResponse(
            200, body=json.dumps(record).encode("utf-8"),
            headers={"Content-Type": "application/json"},
        )

    def _recover(self, url: str) -> FakeResponse:
        self.recovery_calls.append(url)
        dispatch_id = url.rsplit("dispatch_id=", 1)[-1]
        record = self._receipts.get(dispatch_id)
        if record is None:
            return FakeResponse(404, body=b'{"error":"unknown"}',
                                headers={"Content-Type": "application/json"})
        body = self._bodies[dispatch_id]
        return FakeResponse(
            200, body=body,
            headers={"Content-Type": "text/html; charset=utf-8",
                     "X-CB-Recovery-For": dispatch_id},
        )

    @staticmethod
    def _suffix(url: str) -> str:
        return url.rstrip("/").rsplit("/", 1)[-1]

    # ---- ReceiptSource protocol used directly by the executor -------------
    def lookup_receipt(self, site_id: str, dispatch_id: str):
        """Return the signed receipt dict, or None for a definite absence."""
        self.receipt_calls.append(dispatch_id)
        if any(mode == "unverifiable" for mode in self.faults.values()):
            raise _ReceiptUnavailable("receipt endpoint cannot answer")
        record = self._receipts.get(dispatch_id)
        return dict(record) if record is not None else None

    def fetch_recovery(self, site_id: str, dispatch_id: str) -> dict:
        """Re-serve the stored body under the identity, no new collection."""
        self.recovery_calls.append(dispatch_id)
        if dispatch_id not in self._receipts:
            raise _ReceiptUnavailable("unknown dispatch identity")
        body = self._bodies[dispatch_id]
        return {
            "status_code": 200,
            "body": body,
            "content_type": "text/html; charset=utf-8",
            "body_sha256": sha256_bytes(body),
            "response_bytes": len(body),
        }


class _SilentLoss(RuntimeError):
    """Raised when the site accepted a GET but its response was lost.

    A plain RuntimeError deliberately escapes requests' exception hierarchy so
    test wrappers can model a process-level crash that skips the event write.
    """


class _ReceiptUnavailable(requests.RequestException):
    """Raised by the receipt probe when it cannot give an answerable verdict."""


# Alias so tests can simulate the interruption with requests-style semantics.
ReceiptTransportError = _ReceiptUnavailable
SilentDispatchLoss = _SilentLoss
