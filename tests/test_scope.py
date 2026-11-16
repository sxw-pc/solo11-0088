from dataclasses import replace

import pytest

from crawlbudget.errors import (
    SCOPE_INVALID_PATH, SCOPE_INVALID_URL, SCOPE_NON_LOOPBACK, SCOPE_OUT_OF_SITE,
    SCOPE_PORT_MISMATCH, SCOPE_SCHEME, SCOPE_SENSITIVE_QUERY, SCOPE_USERINFO,
)
from crawlbudget.scope import require_in_scope, scope_url


def test_normalization_equivalence_and_hash_stability(demo_config):
    site = demo_config.sites["mock-alpha"]
    result = scope_url("http://127.0.0.1:5021/mock-alpha/docs/page.html?b=2&a=1#section", site)
    assert result.allowed
    assert result.normalized_url == "http://127.0.0.1:5021/mock-alpha/docs/page.html?a=1&b=2"
    assert len(result.url_hash) == 64
    first = scope_url("http://127.0.0.1:5021/mock-alpha/x?b=2&a=1#one", site)
    second = scope_url("HTTP://127.0.0.1:5021/mock-alpha/x?a=1&b=2#two", site)
    assert first.allowed and second.allowed and first.url_hash == second.url_hash
    blocked_first = scope_url("http://example.invalid/a", site)
    assert blocked_first.url_hash == scope_url("http://example.invalid/a", site).url_hash


@pytest.mark.parametrize(
    "url, expected_reason",
    [
        ("http://example.invalid/x", SCOPE_NON_LOOPBACK),
        ("http://10.0.0.1/x", SCOPE_NON_LOOPBACK),
        ("http://127.0.0.2:5021/x", SCOPE_NON_LOOPBACK),
        ("https://127.0.0.1:5021/mock-alpha/x", SCOPE_SCHEME),
        ("ftp://127.0.0.1:5021/mock-alpha/x", SCOPE_SCHEME),
        ("http://user:pass@127.0.0.1:5021/mock-alpha/x", SCOPE_USERINFO),
        ("not a url", SCOPE_INVALID_URL),
        ("", SCOPE_INVALID_URL),
        ("http://127.0.0.1/mock-alpha/x", SCOPE_PORT_MISMATCH),
        ("http://127.0.0.1:9999/mock-alpha/x", SCOPE_PORT_MISMATCH),
        ("http://127.0.0.1:5021/mock-alpha-other/x", SCOPE_OUT_OF_SITE),
        ("http://127.0.0.1:5021/other/x", SCOPE_OUT_OF_SITE),
        ("http://127.0.0.1:5021/mock-alpha/docs/../secret", SCOPE_INVALID_PATH),
        ("http://127.0.0.1:5021/mock-alpha/docs/%2e%2e/secret", SCOPE_INVALID_PATH),
        ("http://127.0.0.1:5021/mock-alpha/x?token=placeholder", SCOPE_SENSITIVE_QUERY),
    ],
)
def test_blocked_url_reasons_and_sensitive_query_detail(demo_config, url, expected_reason):
    result = scope_url(url, demo_config.sites["mock-alpha"])
    assert not result.allowed
    assert result.reason == expected_reason
    if expected_reason == SCOPE_SENSITIVE_QUERY:
        assert "token" in result.detail


def test_ipv6_loopback_and_require_in_scope(demo_config):
    site = replace(demo_config.sites["mock-alpha"], base_url="http://[::1]:5021/mock-alpha/")
    result = scope_url("http://[::1]:5021/mock-alpha/index.html", site)
    assert result.allowed
    assert result.normalized_url == "http://[::1]:5021/mock-alpha/index.html"
    plain = demo_config.sites["mock-alpha"]
    assert require_in_scope("http://127.0.0.1:5021/mock-alpha/a", plain).endswith("/a")
    with pytest.raises(ValueError):
        require_in_scope("http://example.invalid/a", plain)
