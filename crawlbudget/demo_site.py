"""Loopback-only synthetic sites used by the demo and integration tests."""

from __future__ import annotations

import json
import socket
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml
from flask import Flask, Response, request
from werkzeug.serving import make_server

from .errors import DEMO_PORT_IN_USE, DemoError
from .models import ConfigBundle, SiteConfig
from .receipt import (
    DISPATCH_HEADER,
    RECEIPT_QUERY_MARK,
    RECEIPT_RECOVER_MARK,
    sign_receipt,
)
from .util import fixture_bytes, read_fixture_text, sha256_bytes


def load_page_manifest(path: str | Path) -> dict[str, dict[str, dict[str, Any]]]:
    source = Path(path)
    if not source.exists():
        raise DemoError("page manifest not found", details={"file": source.name})
    try:
        raw = yaml.safe_load(read_fixture_text(source))
    except (yaml.YAMLError, UnicodeDecodeError) as exc:
        raise DemoError("page manifest is invalid", details={"error": str(exc)}) from exc
    if not isinstance(raw, dict):
        raise DemoError("page manifest must be a mapping")
    manifest: dict[str, dict[str, dict[str, Any]]] = {}
    for site_id, pages in raw.items():
        if not isinstance(site_id, str) or not isinstance(pages, dict):
            raise DemoError("page manifest site entries must be mappings")
        # Per-site test injection policy lives beside the page table.
        site_pages = {key: value for key, value in pages.items()
                      if isinstance(value, dict) and not key.startswith("__")}
        manifest[site_id] = site_pages
    return manifest


def _site_policy(raw_manifest: dict[str, Any], site_id: str) -> dict[str, Any]:
    section = raw_manifest.get(site_id, {}) if isinstance(raw_manifest, dict) else {}
    policy = section.get("__policy__", {}) if isinstance(section, dict) else {}
    return policy if isinstance(policy, dict) else {}


def create_site_app(
    site: SiteConfig,
    pages: dict[str, dict[str, Any]],
    pages_dir: str | Path,
    *,
    policy: dict[str, Any] | None = None,
) -> Flask:
    app = Flask(f"crawlbudget_mock_{site.site_id}")
    app.config["CRAWLBUDGET_SITE"] = site.site_id
    fixture_root = Path(pages_dir)
    state: dict[str, int] = {}
    # dispatch_id -> signed receipt record, the site-side proof of acceptance.
    receipts: dict[str, dict[str, Any]] = {}
    lock = threading.Lock()
    base_path = site.base_path
    policy = dict(policy or {})

    def _page_bytes(entry: dict[str, Any]) -> tuple[bytes | None, int]:
        filename = str(entry.get("file", ""))
        if not filename or Path(filename).name != filename:
            return None, 500
        path = fixture_root / filename
        if not path.exists():
            return None, 500
        return fixture_bytes(path), 200

    def _record_receipt(dispatch_id: str, entry: dict[str, Any],
                        body: bytes, status: int) -> dict[str, Any]:
        record = {
            "dispatch_id": dispatch_id,
            "site_id": site.site_id,
            # The identity fields echoed back are taken from the signed id's
            # accompanying headers when present; the dispatch_id is the anchor.
            "run_id": request.headers.get("X-CB-Run-Id", ""),
            "job_id": request.headers.get("X-CB-Job-Id", ""),
            "url_hash": request.headers.get("X-CB-Url-Hash", ""),
            "url": request.url,
            "attempt": int(request.headers.get("X-CB-Attempt", "1") or "1"),
            "lease_id": request.headers.get("X-CB-Lease-Id", ""),
            "status_code": status,
            "body_sha256": sha256_bytes(body),
            "response_bytes": len(body),
            "received_at": datetime.now(timezone.utc)
            .replace(microsecond=0).isoformat().replace("+00:00", "Z"),
        }
        record["signature"] = sign_receipt(site.site_id, dispatch_id)
        with lock:
            receipts[dispatch_id] = record
        return record

    @app.get(base_path)
    @app.get(base_path + "<path:subpath>")
    def serve(subpath: str = "") -> Response | tuple[Response, int]:
        key = subpath.strip("/")
        # Receipt and recovery endpoints live under their own path segment so
        # they are never mistaken for a page key by the catch-all route.
        parts = key.split("/") if key else []
        if parts and parts[0] in {RECEIPT_QUERY_MARK, RECEIPT_RECOVER_MARK}:
            return _receipt_routes(parts[0])
        entry = pages.get(key)
        if entry is None:
            return _html(404, f"mock path not found in {site.site_id}", site.site_id)
        status = int(entry.get("status", 200))
        redirect = entry.get("redirect")
        if redirect:
            return Response(
                _page("Moved", f"synthetic redirect for {site.site_id}"),
                status=302,
                content_type="text/html; charset=utf-8",
                headers={"Location": str(redirect), "X-Mock-Site": site.site_id},
            )
        fail_first = int(entry.get("fail_first", 0))
        if fail_first > 0:
            with lock:
                seen = state.get(key, 0) + 1
                state[key] = seen
            if seen <= fail_first:
                return _html(500, "synthetic transient failure", site.site_id)
        if status >= 400 and "file" not in entry:
            return _html(status, f"synthetic status {status}", site.site_id)
        body, error_status = _page_bytes(entry)
        if body is None:
            return _html(error_status, "mock page fixture missing", site.site_id)

        dispatch_id = request.headers.get(DISPATCH_HEADER, "").strip()
        if dispatch_id:
            # Test injection: corrupt the signature the site would otherwise
            # issue, so clients must reject the claim as unverifiable.
            corrupt = policy.get("corrupt_receipt_for")
            if corrupt == key or corrupt == "*":
                record = _record_receipt(dispatch_id, entry, body, status)
                record["signature"] = "0" * 64
            else:
                _record_receipt(dispatch_id, entry, body, status)
            # Test injection: the site accepted and durably recorded the
            # collection, then the connection is torn down while streaming the
            # body. This models loss between site acceptance and local
            # persistence without inventing an HTTP status the client could
            # mistake for a normal, completed attempt.
            if policy.get("drop_response_for") in {key, "*"}:
                request.environ["crawlbudget.drop_connection"] = True
                response = Response(
                    _dropped_stream(), status=status,
                    content_type="text/html; charset=utf-8",
                )
                response.headers["X-Mock-Site"] = site.site_id
                response.headers["Cache-Control"] = "no-store"
                return response
            # Test injection: answer the GET but forget the receipt entirely,
            # giving a definite "never arrived" answer on the resume probe.
            if policy.get("omit_receipt_for") in {key, "*"}:
                with lock:
                    receipts.pop(dispatch_id, None)
        response = Response(
            body, status=status, content_type="text/html; charset=utf-8",
        )
        response.headers["X-Mock-Site"] = site.site_id
        response.headers["Cache-Control"] = "no-store"
        if dispatch_id:
            response.headers[DISPATCH_HEADER] = dispatch_id
        return response

    def _receipt_routes(mark: str) -> Response | tuple[Response, int]:
        dispatch_id = request.args.get("dispatch_id", "").strip()
        if not dispatch_id:
            return Response(
                json.dumps({"error": "dispatch_id required"}),
                status=400, content_type="application/json",
            )
        with lock:
            record = receipts.get(dispatch_id)
        if mark == RECEIPT_QUERY_MARK:
            if record is None:
                # A signed, explicit JSON 404 is the site's definite conclusion
                # that no collection arrived for this identity.
                return Response(
                    json.dumps({
                        "dispatch_id": dispatch_id,
                        "site_id": site.site_id,
                        "present": False,
                        "signature": sign_receipt(site.site_id, dispatch_id),
                    }),
                    status=404, content_type="application/json",
                )
            return Response(
                json.dumps(record), status=200,
                content_type="application/json",
            )
        # Recovery endpoint: re-serve the originally delivered body under the
        # same identity. It never creates a new collection receipt, so the
        # re-delivery contract adds no site quota consumption.
        if record is None:
            return Response(
                json.dumps({"error": "unknown dispatch identity"}),
                status=404, content_type="application/json",
            )
        target_path = record.get("url", "") or request.args.get("url", "")
        key = target_path.rstrip("/").rsplit("/", 1)[-1]
        page_entry = pages.get(key)
        if page_entry is None:
            return Response(
                json.dumps({"error": "recovery source page no longer configured"}),
                status=410, content_type="application/json",
            )
        body, error_status = _page_bytes(page_entry)
        if body is None:
            return _html(error_status, "mock page fixture missing", site.site_id)
        response = Response(
            body, status=200, content_type="text/html; charset=utf-8",
        )
        response.headers["X-Mock-Site"] = site.site_id
        response.headers["X-CB-Recovery-For"] = dispatch_id
        response.headers["Cache-Control"] = "no-store"
        return response

    @app.get("/healthz")
    def healthz() -> Response:
        return Response(f"ok {site.site_id}\n", content_type="text/plain; charset=utf-8")

    return _ConnectionDroppingMiddleware(app)


class _ConnectionDroppingMiddleware:
    """Close the raw socket for requests flagged as post-acceptance losses."""

    def __init__(self, wsgi_app) -> None:
        self.wsgi_app = wsgi_app

    def __call__(self, environ, start_response):
        def _closing_start_response(status, headers, exc_info=None):
            write = start_response(status, headers, exc_info)
            if environ.get("crawlbudget.drop_connection"):
                raw_socket = environ.get("werkzeug.socket")
                if raw_socket is not None:
                    try:
                        raw_socket.shutdown(socket.SHUT_RDWR)
                    except OSError:
                        pass
            return write

        return self.wsgi_app(environ, _closing_start_response)


def _dropped_stream():
    # The headers are flushed first; the middleware then drops the socket, so
    # iterating this body raises a transport error on the client side.
    yield b""
    return  # pragma: no cover - the socket is already torn down
    yield b""


def _html(status: int, message: str, site_id: str) -> Response:
    return Response(
        _page(str(status), message), status=status,
        content_type="text/html; charset=utf-8", headers={"X-Mock-Site": site_id},
    )


def _page(title: str, message: str) -> str:
    return (
        f'<!doctype html><html lang="en"><head><meta charset="utf-8"><title>{title}</title>'
        f"</head><body><h1>{title}</h1><p>{message}</p>"
        "<p>crawlbudget synthetic fixture; no external resources.</p></body></html>"
    )


@dataclass
class RunningMockServer:
    site_id: str
    host: str
    port: int
    server: Any
    thread: threading.Thread

    def stop(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)


def start_mock_server(
    site: SiteConfig,
    pages: dict[str, dict[str, Any]],
    pages_dir: str | Path,
    *,
    policy: dict[str, Any] | None = None,
) -> RunningMockServer:
    app = create_site_app(site, pages, pages_dir, policy=policy)
    try:
        server = make_server(site.host, site.port, app, threaded=True)
    except OSError as exc:
        raise DemoError(
            "cannot bind mock site",
            details={"site_id": site.site_id, "host": site.host, "port": site.port,
                     "code": DEMO_PORT_IN_USE, "error": str(exc)},
        ) from exc
    thread = threading.Thread(
        target=server.serve_forever, name=f"mock-{site.site_id}", daemon=True
    )
    thread.start()
    _wait_for_server(site.host, site.port)
    return RunningMockServer(
        site_id=site.site_id, host=site.host, port=site.port, server=server, thread=thread
    )


def start_mock_servers(
    config: ConfigBundle,
    pages_dir: str | Path,
    manifest_path: str | Path,
    *,
    policies: dict[str, dict[str, Any]] | None = None,
) -> list[RunningMockServer]:
    raw_manifest = yaml.safe_load(read_fixture_text(manifest_path))
    manifest = load_page_manifest(manifest_path)
    policies = policies or {}
    started: list[RunningMockServer] = []
    try:
        for site_id in sorted(config.sites):
            site = config.sites[site_id]
            policy = policies.get(site_id) or _site_policy(raw_manifest, site_id)
            started.append(
                start_mock_server(
                    site, manifest.get(site_id, {}), pages_dir, policy=policy
                )
            )
    except Exception:
        stop_mock_servers(started)
        raise
    return started


def stop_mock_servers(servers: list[RunningMockServer]) -> None:
    for server in reversed(servers):
        try:
            server.stop()
        except Exception:
            pass


def _wait_for_server(host: str, port: int, timeout: float = 2.0) -> None:
    probe = socket.socket(socket.AF_INET6 if ":" in host else socket.AF_INET, socket.SOCK_STREAM)
    probe.settimeout(timeout)
    try:
        probe.connect((host, port))
    finally:
        probe.close()
