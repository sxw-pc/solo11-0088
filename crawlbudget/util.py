"""Small deterministic helpers shared by the crawlbudget modules."""

from __future__ import annotations

import hashlib
import json
import posixpath
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable
from urllib.parse import parse_qsl, urlencode, urlunsplit

UTC = timezone.utc
LOOPBACK_HOSTS = frozenset({"127.0.0.1", "::1"})
SENSITIVE_QUERY_NAMES = frozenset(
    {"token", "api_key", "apikey", "key", "secret", "password", "auth", "session"}
)
SCHEMA_VERSION = "1.0"


def parse_utc(value: str) -> datetime:
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    parsed = datetime.fromisoformat(text)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def iso_z(value: datetime) -> str:
    return value.astimezone(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def virtual_date(value: datetime) -> str:
    return value.astimezone(UTC).date().isoformat()


def lease_expiry(start: datetime) -> str:
    next_day = start.astimezone(UTC).date() + timedelta(days=1)
    return f"{next_day.isoformat()}T00:00:00Z"


def canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_text(text: str) -> str:
    return sha256_bytes(text.encode("utf-8"))


def fingerprint(value: Any) -> str:
    return sha256_text(canonical_json(value))


def read_fixture_text(path: Path) -> str:
    """Decode repository fixtures with platform-independent line endings."""
    data = path.read_bytes().decode("utf-8-sig")
    return data.replace("\r\n", "\n").replace("\r", "\n")


def fixture_bytes(path: Path) -> bytes:
    return read_fixture_text(path).encode("utf-8")


def normalize_path(path: str) -> str:
    if not path:
        return "/"
    if not path.startswith("/"):
        path = "/" + path
    trailing = path.endswith("/")
    normalized = posixpath.normpath(path)
    if not normalized.startswith("/"):
        normalized = "/" + normalized
    if trailing and not normalized.endswith("/"):
        normalized += "/"
    return normalized


def has_dot_segments(path: str) -> bool:
    decoded = path.replace("%2e", ".").replace("%2E", ".")
    return any(part in {".", ".."} for part in decoded.split("/"))


def is_loopback_host(host: str) -> bool:
    return host.lower() in LOOPBACK_HOSTS


def canonical_url_parts(
    *, scheme: str, host: str, port: int, path: str, query_pairs: Iterable[tuple[str, str]]
) -> str:
    query = urlencode(sorted(query_pairs), doseq=True)
    host_text = f"[{host}]" if ":" in host and not host.startswith("[") else host
    return urlunsplit((scheme, f"{host_text}:{port}", path, query, ""))


def query_pairs(raw_query: str) -> list[tuple[str, str]]:
    return sorted(parse_qsl(raw_query, keep_blank_values=True))


def sensitive_query_names(raw_query: str) -> list[str]:
    found = {
        name.lower()
        for name, _value in parse_qsl(raw_query, keep_blank_values=True)
        if name.lower() in SENSITIVE_QUERY_NAMES
    }
    return sorted(found)


def dedupe_stable(items: Iterable[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for item in items:
        if item not in seen:
            seen.add(item)
            result.append(item)
    return result


def elapsed_ms(started: float) -> int:
    return max(0, int(round((time.monotonic() - started) * 1000)))
