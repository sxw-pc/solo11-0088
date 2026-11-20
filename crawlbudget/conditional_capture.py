"""Bounded capture of loopback conditional GET evidence.

This records response facts. It deliberately makes no cache reuse decision.
The module only talks to registered loopback sites and only stores 200 bytes
or 304 validator metadata; a 304 receipt never carries a body. Receipts are
persisted idempotently so repeated offline runs do not duplicate facts.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import requests

from .models import SiteConfig
from .scope import scope_url
from .storage import Storage
from .util import canonical_json, iso_z, parse_utc, sha256_bytes, write_body_file

ALLOWED_REQUEST_HEADERS = frozenset({"Accept-Language", "If-None-Match"})


@dataclass(frozen=True)
class CaptureReceipt:
    url: str
    request_headers: tuple[tuple[str, str], ...]
    status: int
    etag: str
    vary: str
    body_sha256: str | None
    body_path: str | None


def capture_conditional(
    site: SiteConfig,
    url: str,
    headers: dict[str, str],
    output_dir: Path,
    *,
    session: requests.Session | None = None,
) -> CaptureReceipt:
    """Capture 200 bytes or 304 metadata without reusing an old representation."""
    if not scope_url(url, site).allowed:
        raise ValueError("URL is outside the registered loopback site")
    if set(headers) - ALLOWED_REQUEST_HEADERS:
        raise ValueError("unsupported conditional request header")
    own_session = session is None
    client = session or requests.Session()
    if own_session:
        client.trust_env = False
    try:
        response = client.get(
            url, headers=headers, allow_redirects=False,
            stream=True, timeout=site.timeout_ms / 1000.0,
        )
        try:
            status = int(response.status_code)
            if status not in {200, 304}:
                raise ValueError(f"capture status {status} is not a representation receipt")
            body = b"".join(response.iter_content(chunk_size=4096)) if status == 200 else b""
            if len(body) > site.max_response_bytes:
                raise ValueError("conditional response body exceeds site limit")
            digest = sha256_bytes(body) if status == 200 else None
            body_path = None
            if digest is not None:
                digest, body_path = write_body_file(output_dir, body)
            return CaptureReceipt(
                url=url, request_headers=tuple(sorted(headers.items())),
                status=status, etag=str(response.headers.get("ETag", "")),
                vary=str(response.headers.get("Vary", "")),
                body_sha256=digest, body_path=body_path,
            )
        finally:
            response.close()
    finally:
        if own_session:
            client.close()


def insert_capture_receipt(
    storage: Storage, run_id: str, site_id: str, receipt: CaptureReceipt,
    recorded_at: str,
) -> bool:
    """Persist one receipt; return False when the identical receipt exists."""
    cur = storage.conn.execute(
        """INSERT OR IGNORE INTO capture_receipts
           (run_id, site_id, url, request_headers_json, status, etag, vary,
            body_sha256, body_path, recorded_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (run_id, site_id, receipt.url,
         canonical_json(list(receipt.request_headers)), receipt.status,
         receipt.etag, receipt.vary, receipt.body_sha256, receipt.body_path,
         iso_z(parse_utc(recorded_at))),
    )
    storage.conn.commit()
    return bool(cur.rowcount)


def capture_and_record(
    site: SiteConfig, url: str, headers: dict[str, str], output_dir: Path,
    storage: Storage, run_id: str, recorded_at: str,
    *, session: requests.Session | None = None,
) -> CaptureReceipt:
    """Capture a receipt and persist its facts in the ledger, idempotently."""
    receipt = capture_conditional(site, url, headers, output_dir, session=session)
    insert_capture_receipt(
        storage, run_id, site.site_id, receipt, iso_z(parse_utc(recorded_at)))
    return receipt
