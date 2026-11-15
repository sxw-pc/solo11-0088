"""URL normalization and loopback/site-scope validation."""

from __future__ import annotations

from urllib.parse import urlsplit

from .errors import (
    SCOPE_INVALID_PATH,
    SCOPE_INVALID_URL,
    SCOPE_NON_LOOPBACK,
    SCOPE_OUT_OF_SITE,
    SCOPE_PORT_MISMATCH,
    SCOPE_SCHEME,
    SCOPE_SENSITIVE_QUERY,
    SCOPE_USERINFO,
)
from .models import ScopeOutcome, SiteConfig
from .util import (
    canonical_url_parts,
    has_dot_segments,
    is_loopback_host,
    normalize_path,
    query_pairs,
    sensitive_query_names,
    sha256_text,
)


def external_location(target: str) -> bool:
    """Return True when a redirect target leaves the local machine."""
    if not target:
        return False
    parsed = urlsplit(target)
    return parsed.scheme not in {"http", "https"} or not is_loopback_host(parsed.hostname or "")


def scope_url(raw_url: str, site: SiteConfig) -> ScopeOutcome:
    """Validate and canonicalize one seed URL without making any network call."""
    original = raw_url
    text = raw_url.strip()
    if not text or any(char.isspace() for char in text):
        return _blocked(original, SCOPE_INVALID_URL, "URL is empty or contains whitespace")
    try:
        parsed = urlsplit(text)
        port = parsed.port
    except ValueError:
        return _blocked(original, SCOPE_INVALID_URL, "URL is malformed")
    if parsed.scheme.lower() != "http":
        return _blocked(original, SCOPE_SCHEME, "only http is permitted")
    if parsed.username is not None or parsed.password is not None:
        return _blocked(original, SCOPE_USERINFO, "userinfo is not permitted in seed URLs")
    host = (parsed.hostname or "").lower()
    if not host:
        return _blocked(original, SCOPE_INVALID_URL, "URL has no host")
    if not is_loopback_host(host):
        return _blocked(original, SCOPE_NON_LOOPBACK, "host is not 127.0.0.1 or ::1")
    if port is None:
        return _blocked(original, SCOPE_PORT_MISMATCH, "URL must include the registered port")
    if host != site.host or port != site.port:
        return _blocked(original, SCOPE_PORT_MISMATCH, "host or port differs from site registration")
    raw_path = parsed.path or "/"
    if has_dot_segments(raw_path):
        return _blocked(original, SCOPE_INVALID_PATH, "dot segments are not permitted")
    path = normalize_path(raw_path)
    if not path.startswith(site.base_path):
        return _blocked(original, SCOPE_OUT_OF_SITE, "path is outside the registered site prefix")
    sensitive = sensitive_query_names(parsed.query)
    if sensitive:
        return _blocked(
            original,
            SCOPE_SENSITIVE_QUERY,
            "sensitive query parameter names are not permitted: " + ", ".join(sensitive),
        )
    pairs = query_pairs(parsed.query)
    normalized = canonical_url_parts(
        scheme="http", host=host, port=port, path=path, query_pairs=pairs
    )
    return ScopeOutcome(
        allowed=True,
        original_url=original,
        normalized_url=normalized,
        url_hash=sha256_text(normalized),
        reason=None,
        detail="in scope",
    )


def require_in_scope(raw_url: str, site: SiteConfig) -> str:
    result = scope_url(raw_url, site)
    if not result.allowed:
        raise ValueError(f"{result.reason}: {result.detail}")
    return result.normalized_url


def _blocked(original: str, reason: str, detail: str) -> ScopeOutcome:
    return ScopeOutcome(
        allowed=False,
        original_url=original,
        normalized_url="",
        url_hash=sha256_text("invalid:" + reason + ":" + original.strip()),
        reason=reason,
        detail=detail,
    )
