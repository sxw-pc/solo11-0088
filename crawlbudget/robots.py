"""Offline robots.txt parsing and deterministic path authorization."""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from pathlib import Path

from .errors import (
    ROBOTS_INVALID_CRAWL_DELAY,
    ROBOTS_MALFORMED_LINE,
    ROBOTS_MISSING,
    ROBOTS_NO_MATCHING_GROUP,
    ROBOTS_RULE_WITHOUT_AGENT,
    ROBOTS_UNKNOWN_DIRECTIVE,
)
from .models import RobotsPolicy, RobotsRule, SiteConfig
from .util import read_fixture_text

SUPPORTED_DIRECTIVES = {"user-agent", "allow", "disallow", "crawl-delay"}
DEFAULT_AGENT = "crawlbudget"


@dataclass
class _RawGroup:
    agents: list[str] = field(default_factory=list)
    rules: list[tuple[str, str, int]] = field(default_factory=list)
    crawl_delays: list[float] = field(default_factory=list)
    first_line: int = 0


def _warn(code: str, message: str, line: int) -> dict[str, object]:
    return {"code": code, "message": message, "line": line}


def _unavailable(site: SiteConfig, path: Path, problem: str) -> RobotsPolicy:
    return RobotsPolicy(
        site_id=site.site_id,
        rules=(),
        crawl_delay_ms=0,
        matched_user_agent="",
        warnings=(_warn(
            ROBOTS_MISSING,
            f"robots fixture {problem} for {site.site_id}; denying all paths",
            0,
        ),),
        available=False,
        unavailable_reason=f"robots fixture {problem}: {path.name}",
    )


def load_robots(site: SiteConfig, user_agent: str = DEFAULT_AGENT) -> RobotsPolicy:
    path = Path(site.robots_file)
    if not path.exists():
        return _unavailable(site, path, "missing")
    try:
        text = read_fixture_text(path)
    except (OSError, UnicodeDecodeError):
        return _unavailable(site, path, "unreadable")
    return parse_robots(text, site_id=site.site_id, user_agent=user_agent)


def parse_robots(text: str, site_id: str, user_agent: str = DEFAULT_AGENT) -> RobotsPolicy:
    groups, warnings = _parse_groups(text)
    selected = _select_groups(groups, user_agent)
    if not selected and groups:
        warnings.append(_warn(
            ROBOTS_NO_MATCHING_GROUP,
            f"no robots group matches user-agent {user_agent}",
            0,
        ))
    rules: list[RobotsRule] = []
    for group in selected:
        for directive, pattern, _line in group.rules:
            body = pattern[:-1] if pattern.endswith("$") else pattern
            rules.append(RobotsRule(
                rule_id=f"R{len(rules) + 1:03d}",
                directive=directive,
                pattern=pattern,
                user_agents=tuple(group.agents),
                group_id=f"G{groups.index(group) + 1:03d}",
                specificity=sum(1 for char in body if char != "*"),
            ))
    delays = [delay for group in selected for delay in group.crawl_delays]
    return RobotsPolicy(
        site_id=site_id,
        rules=tuple(rules),
        crawl_delay_ms=int(math.ceil(max(delays) * 1000)) if delays else 0,
        matched_user_agent=", ".join(sorted({a for g in selected for a in g.agents})),
        warnings=tuple(warnings),
        available=True,
    )


def _parse_groups(text: str) -> tuple[list[_RawGroup], list[dict[str, object]]]:
    groups: list[_RawGroup] = []
    warnings: list[dict[str, object]] = []
    current = _RawGroup()
    current_started = False

    def flush() -> None:
        nonlocal current, current_started
        if current.agents or current.rules or current.crawl_delays:
            groups.append(current)
        current = _RawGroup()
        current_started = False

    for number, raw_line in enumerate(text.splitlines(), start=1):
        line = raw_line.split("#", 1)[0].strip()
        if not line:
            if current_started:
                flush()
            continue
        if ":" not in line:
            warnings.append(_warn(ROBOTS_MALFORMED_LINE, "line has no colon; ignored", number))
            continue
        key, value = line.split(":", 1)
        directive, value = key.strip().lower(), value.strip()
        if directive == "user-agent":
            if current.rules or current.crawl_delays:
                flush()
            if not value:
                warnings.append(_warn(ROBOTS_MALFORMED_LINE, "empty user-agent; ignored", number))
                continue
            if not current.agents:
                current.first_line = number
            current.agents.append(value)
            current_started = True
        elif directive in {"allow", "disallow"}:
            if not current.agents:
                warnings.append(_warn(
                    ROBOTS_RULE_WITHOUT_AGENT,
                    f"{directive} before any user-agent; ignored",
                    number,
                ))
            elif value == "":
                current_started = True
            elif not value.startswith("/"):
                warnings.append(_warn(
                    ROBOTS_MALFORMED_LINE,
                    f"{directive} pattern must start with /; ignored",
                    number,
                ))
            else:
                current.rules.append((directive, value, number))
                current_started = True
        elif directive == "crawl-delay":
            if not current.agents:
                warnings.append(_warn(
                    ROBOTS_RULE_WITHOUT_AGENT,
                    "crawl-delay before any user-agent; ignored",
                    number,
                ))
                continue
            try:
                delay = float(value)
                if delay < 0 or not math.isfinite(delay):
                    raise ValueError
            except ValueError:
                warnings.append(_warn(
                    ROBOTS_INVALID_CRAWL_DELAY,
                    "crawl-delay must be a finite non-negative number",
                    number,
                ))
                continue
            current.crawl_delays.append(delay)
            current_started = True
        elif directive not in SUPPORTED_DIRECTIVES:
            warnings.append(_warn(
                ROBOTS_UNKNOWN_DIRECTIVE,
                f"unsupported directive {directive}; ignored",
                number,
            ))
    flush()
    return groups, warnings


def _select_groups(groups: list[_RawGroup], user_agent: str) -> list[_RawGroup]:
    wanted = user_agent.strip().lower()
    exact = [g for g in groups if any(a.strip().lower() == wanted for a in g.agents)]
    if exact:
        return exact
    return [g for g in groups if any(a.strip() == "*" for a in g.agents)]
