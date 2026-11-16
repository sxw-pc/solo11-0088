from dataclasses import replace
from pathlib import Path

from crawlbudget.errors import (
    ROBOTS_INVALID_CRAWL_DELAY, ROBOTS_MALFORMED_LINE, ROBOTS_MISSING,
    ROBOTS_RULE_WITHOUT_AGENT, ROBOTS_UNKNOWN_DIRECTIVE,
)
from crawlbudget.robots import load_robots, parse_robots


def test_alpha_rules_evidence_and_missing_robots_conservatism(demo_config):
    policy = load_robots(demo_config.sites["mock-alpha"])
    assert policy.available
    assert policy.crawl_delay_ms == 10
    allow = policy.evaluate("/mock-alpha/private/public-note.html")
    assert allow.allowed and allow.rule is not None and allow.rule.pattern.endswith("$")
    assert allow.evidence()["rule_id"] == "R002"
    denied = policy.evaluate("/mock-alpha/private/secret.html")
    assert not denied.allowed and denied.evidence()["rule_id"] == "R001"
    plain = policy.evaluate("/mock-alpha/index.html")
    assert plain.allowed and plain.rule is None and plain.reason == "allowed_no_rule"
    missing_site = replace(
        demo_config.sites["mock-alpha"], robots_file=Path("/missing") / "robots.txt"
    )
    missing = load_robots(missing_site)
    assert not missing.available
    assert missing.evaluate("/mock-alpha/index.html").allowed is False
    assert missing.warnings[0]["code"] == ROBOTS_MISSING


def test_wildcards_anchors_and_crawl_delay_rounding():
    text = """
User-agent: *
Disallow: /private/*
Allow: /private/open/
Allow: /private/public$
Crawl-delay: 0.25
"""
    policy = parse_robots(text, "s")
    assert policy.matched_user_agent == "*"
    assert policy.crawl_delay_ms == 250
    assert policy.evaluate("/private/open/page").allowed
    assert policy.evaluate("/private/public").allowed
    assert not policy.evaluate("/private/public/extra").allowed
    assert not policy.evaluate("/private/other").allowed
    assert policy.evaluate("/elsewhere").allowed
    rounded = parse_robots("User-agent: crawlbudget\nCrawl-delay: 0.0105\nAllow: /\n", "s")
    assert rounded.crawl_delay_ms == 11


def test_rule_precedence_and_group_merging():
    equal = parse_robots("User-agent: crawlbudget\nDisallow: /same/\nAllow: /same/\n", "s")
    match = equal.evaluate("/same/page")
    assert match.allowed and match.rule is not None and match.rule.directive == "allow"
    text = "User-agent: *\nDisallow: /\n\nUser-agent: crawlbudget\nAllow: /wanted/\n"
    assert parse_robots(text, "s").evaluate("/wanted/page").allowed
    other = parse_robots(text.replace("crawlbudget", "other"), "s")
    assert not other.evaluate("/wanted/page").allowed
    merged = parse_robots(
        "User-agent: crawlbudget\nDisallow: /one/\n\n"
        "User-agent: crawlbudget\nDisallow: /two/\n",
        "s",
    )
    assert len(merged.rules) == 2
    assert not merged.evaluate("/one/x").allowed
    assert not merged.evaluate("/two/x").allowed


def test_malformed_empty_and_unmatched_input_is_tolerated():
    malformed = parse_robots(
        "this line is broken\nDisallow: /orphan\nUser-agent: crawlbudget\n"
        "Disallow: relative-path\nAllow: /ok/\nCrawl-delay: not-a-number\n"
        "Sitemap: /sitemap.xml\n",
        "s",
    )
    assert {warning["code"] for warning in malformed.warnings} >= {
        ROBOTS_MALFORMED_LINE, ROBOTS_RULE_WITHOUT_AGENT,
        ROBOTS_INVALID_CRAWL_DELAY, ROBOTS_UNKNOWN_DIRECTIVE,
    }
    assert malformed.evaluate("/ok/page").allowed
    empty = parse_robots("User-agent: *\nDisallow:\n", "s")
    assert empty.rules == () and empty.evaluate("/anything").allowed
    unmatched = parse_robots("User-agent: someone-else\nDisallow: /\n", "s")
    assert unmatched.warnings[0]["code"] == "ROBOTS_NO_MATCHING_GROUP"
    assert unmatched.evaluate("/page").allowed
