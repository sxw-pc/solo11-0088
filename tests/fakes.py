"""Deterministic in-memory HTTP doubles for executor and report tests."""

from pathlib import Path
from typing import Any

from crawlbudget.receipts import (
    VERDICT_NOT_RECEIVED,
    ReceiptVerdict,
    classify_receipt_reply,
)
from crawlbudget.util import fixture_bytes

PAGES = Path(__file__).resolve().parent.parent / "fixtures" / "pages"


class FakeResponse:
    def __init__(self, status_code: int, *, body: bytes = b"",
                 headers: dict[str, str] | None = None) -> None:
        self.status_code = status_code
        self.body = body
        self.headers = headers or {}

    def iter_content(self, chunk_size: int = 4096):
        for index in range(0, len(self.body), chunk_size):
            yield self.body[index:index + chunk_size]

    def close(self) -> None:
        pass


class FakeReceiptVerifier:
    """Receipt-verifier double; real token/binding checks, no HTTP.

    receipts maps intent_id to a signed receipt dict; bodies maps intent_id
    to the (body, content_type) the site would return for same-identity
    retrieval. forced short-circuits every verdict (e.g. unverifiable).
    """

    def __init__(self, receipts: dict[str, dict] | None = None,
                 bodies: dict[str, tuple[bytes, str]] | None = None,
                 forced: str | None = None) -> None:
        self.receipts = receipts or {}
        self.bodies = bodies or {}
        self.forced = forced
        self.verified: list[str] = []
        self.fetched: list[str] = []

    def verify(self, intent: dict[str, Any]) -> ReceiptVerdict:
        self.verified.append(intent["intent_id"])
        if self.forced is not None:
            return ReceiptVerdict(self.forced, detail="injected verdict")
        receipt = self.receipts.get(intent["intent_id"])
        if receipt is None:
            return ReceiptVerdict(VERDICT_NOT_RECEIVED)
        return classify_receipt_reply(intent, 200, receipt)

    def fetch_body(self, intent: dict[str, Any], receipt: dict[str, Any]):
        self.fetched.append(intent["intent_id"])
        return self.bodies.get(intent["intent_id"])


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
