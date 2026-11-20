"""Loopback-only synthetic sites used by the demo and integration tests."""

from __future__ import annotations

import socket
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

import yaml
from flask import Flask, Response, jsonify, request
from werkzeug.serving import make_server

from .errors import DEMO_PORT_IN_USE, DemoError
from .models import ConfigBundle, SiteConfig
from .receipts import (
    INTENT_HEADER,
    LEASE_HEADER,
    RECEIPT_API_PREFIX,
    RUN_HEADER,
    sign_absence,
    sign_receipt,
)
from .util import fixture_bytes, read_fixture_text, sha256_bytes

# Test injection point: receives the signed receipt (and may mutate, re-sign
# via crawlbudget.receipts.sign_receipt, corrupt, or drop it by returning
# None) before the mock site stores it.
ReceiptHook = Callable[[dict[str, Any]], dict[str, Any] | None]


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
        manifest[site_id] = {}
        for subpath, entry in pages.items():
            if not isinstance(subpath, str) or not isinstance(entry, dict):
                raise DemoError("page manifest path entries must be mappings")
            manifest[site_id][subpath.strip("/")] = entry
    return manifest


def create_site_app(
    site: SiteConfig,
    pages: dict[str, dict[str, Any]],
    pages_dir: str | Path,
    *,
    receipt_hook: ReceiptHook | None = None,
) -> Flask:
    app = Flask(f"crawlbudget_mock_{site.site_id}")
    app.config["CRAWLBUDGET_SITE"] = site.site_id
    fixture_root = Path(pages_dir)
    state: dict[str, int] = {}
    receipts: dict[str, dict[str, Any]] = {}
    bodies: dict[str, bytes] = {}
    app.config["CRAWLBUDGET_RECEIPTS"] = receipts
    app.config["CRAWLBUDGET_RECEIPT_BODIES"] = bodies
    lock = threading.Lock()
    base_path = site.base_path

    def record_receipt(key: str, response: Response) -> None:
        intent_id = request.headers.get(INTENT_HEADER, "")
        if not intent_id:
            return
        body = response.get_data()
        with lock:
            seq = len(receipts) + 1
        receipt = sign_receipt(site.site_id, {
            "intent_id": intent_id,
            "site_id": site.site_id,
            "run_id": request.headers.get(RUN_HEADER, ""),
            "lease_id": request.headers.get(LEASE_HEADER, ""),
            "path": key,
            "status": int(response.status_code),
            "body_sha256": sha256_bytes(body),
            "seq": seq,
        })
        if receipt_hook is not None:
            receipt = receipt_hook(receipt)
        if receipt is None:
            return
        with lock:
            receipts[intent_id] = receipt
            bodies[intent_id] = body

    @app.get(base_path)
    @app.get(base_path + "<path:subpath>")
    def serve(subpath: str = "") -> Response | tuple[Response, int]:
        key = subpath.strip("/")
        entry = pages.get(key)
        if entry is None:
            response = _html(404, f"mock path not found in {site.site_id}", site.site_id)
            record_receipt(key, response)
            return response
        status = int(entry.get("status", 200))
        redirect = entry.get("redirect")
        if redirect:
            response = Response(
                _page("Moved", f"synthetic redirect for {site.site_id}"),
                status=302,
                content_type="text/html; charset=utf-8",
                headers={"Location": str(redirect), "X-Mock-Site": site.site_id},
            )
            record_receipt(key, response)
            return response
        fail_first = int(entry.get("fail_first", 0))
        if fail_first > 0:
            with lock:
                seen = state.get(key, 0) + 1
                state[key] = seen
            if seen <= fail_first:
                response = _html(500, "synthetic transient failure", site.site_id)
                record_receipt(key, response)
                return response
        if status >= 400 and "file" not in entry:
            response = _html(status, f"synthetic status {status}", site.site_id)
            record_receipt(key, response)
            return response
        filename = str(entry.get("file", ""))
        if not filename:
            response = _html(404, "mock page file is not configured", site.site_id)
            record_receipt(key, response)
            return response
        if Path(filename).name != filename:
            response = _html(500, "invalid mock page filename", site.site_id)
            record_receipt(key, response)
            return response
        path = fixture_root / filename
        if not path.exists():
            response = _html(500, "mock page fixture missing", site.site_id)
            record_receipt(key, response)
            return response
        response = Response(
            fixture_bytes(path),
            status=status,
            content_type="text/html; charset=utf-8",
        )
        response.headers["X-Mock-Site"] = site.site_id
        response.headers["Cache-Control"] = "no-store"
        record_receipt(key, response)
        return response

    @app.get(RECEIPT_API_PREFIX + "/<intent_id>")
    def receipt_lookup(intent_id: str) -> tuple[Any, int]:
        with lock:
            receipt = receipts.get(intent_id)
        if receipt is None:
            return jsonify(sign_absence(site.site_id, intent_id)), 404
        return jsonify(receipt), 200

    @app.get(RECEIPT_API_PREFIX + "/<intent_id>/body")
    def receipt_body(intent_id: str) -> Response | tuple[Any, int]:
        with lock:
            known = intent_id in receipts
            body = bodies.get(intent_id, b"")
        if not known:
            return jsonify(sign_absence(site.site_id, intent_id)), 404
        return Response(body, content_type="text/html; charset=utf-8")

    @app.get("/healthz")
    def healthz() -> Response:
        return Response(f"ok {site.site_id}\n", content_type="text/plain; charset=utf-8")

    return app


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
    receipts: dict[str, dict[str, Any]] = field(default_factory=dict)
    receipt_bodies: dict[str, bytes] = field(default_factory=dict)

    def stop(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)


def start_mock_server(
    site: SiteConfig,
    pages: dict[str, dict[str, Any]],
    pages_dir: str | Path,
    *,
    receipt_hook: ReceiptHook | None = None,
) -> RunningMockServer:
    app = create_site_app(site, pages, pages_dir, receipt_hook=receipt_hook)
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
        site_id=site.site_id, host=site.host, port=site.port, server=server,
        thread=thread,
        receipts=app.config["CRAWLBUDGET_RECEIPTS"],
        receipt_bodies=app.config["CRAWLBUDGET_RECEIPT_BODIES"],
    )


def start_mock_servers(
    config: ConfigBundle,
    pages_dir: str | Path,
    manifest_path: str | Path,
    *,
    receipt_hooks: dict[str, ReceiptHook] | None = None,
) -> list[RunningMockServer]:
    manifest = load_page_manifest(manifest_path)
    hooks = receipt_hooks or {}
    started: list[RunningMockServer] = []
    try:
        for site_id in sorted(config.sites):
            site = config.sites[site_id]
            started.append(
                start_mock_server(site, manifest.get(site_id, {}), pages_dir,
                                  receipt_hook=hooks.get(site_id))
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
