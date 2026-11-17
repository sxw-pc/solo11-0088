"""Bounded capture of loopback conditional GET evidence.

This records response facts. It deliberately makes no cache reuse decision.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import requests

from .models import SiteConfig
from .scope import scope_url
from .util import sha256_bytes


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
    allowed = {"Accept-Language", "If-None-Match"}
    if set(headers) - allowed:
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
                output_dir.mkdir(parents=True, exist_ok=True)
                target = output_dir / f"{digest}.body"
                if target.exists() and target.read_bytes() != body:
                    raise ValueError("captured body digest collision")
                target.write_bytes(body)
                body_path = str(target)
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
