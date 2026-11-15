"""Stable error codes and exception types for crawlbudget."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


# Configuration errors.
CFG_MISSING_FIELD = "CFG_MISSING_FIELD"
CFG_DUPLICATE_SITE = "CFG_DUPLICATE_SITE"
CFG_DUPLICATE_JOB = "CFG_DUPLICATE_JOB"
CFG_INVALID_URL = "CFG_INVALID_URL"
CFG_NON_LOOPBACK_TARGET = "CFG_NON_LOOPBACK_TARGET"
CFG_NON_POSITIVE_LIMIT = "CFG_NON_POSITIVE_LIMIT"
CFG_UNKNOWN_SITE = "CFG_UNKNOWN_SITE"
CFG_INVALID_VALUE = "CFG_INVALID_VALUE"
CFG_SEED_FILE_MISSING = "CFG_SEED_FILE_MISSING"
CFG_INVALID_YAML = "CFG_INVALID_YAML"

# Scope errors.
SCOPE_INVALID_URL = "scope_invalid_url"
SCOPE_SCHEME = "scope_scheme"
SCOPE_NON_LOOPBACK = "scope_non_loopback"
SCOPE_PORT_MISMATCH = "scope_port_mismatch"
SCOPE_USERINFO = "scope_userinfo"
SCOPE_INVALID_PATH = "scope_invalid_path"
SCOPE_OUT_OF_SITE = "scope_out_of_site"
SCOPE_SENSITIVE_QUERY = "scope_sensitive_query"

# Robots errors and warning codes.
ROBOTS_MISSING = "robots_missing"
ROBOTS_DISALLOW = "robots_disallow"
ROBOTS_MALFORMED_LINE = "ROBOTS_MALFORMED_LINE"
ROBOTS_UNKNOWN_DIRECTIVE = "ROBOTS_UNKNOWN_DIRECTIVE"
ROBOTS_INVALID_CRAWL_DELAY = "ROBOTS_INVALID_CRAWL_DELAY"
ROBOTS_RULE_WITHOUT_AGENT = "ROBOTS_RULE_WITHOUT_AGENT"
ROBOTS_NO_MATCHING_GROUP = "ROBOTS_NO_MATCHING_GROUP"

# Budget and planning errors.
BUDGET_QUOTA_EXCEEDED = "quota_exceeded"
BUDGET_RUN_LIMIT_EXCEEDED = "run_limit_exceeded"
BUDGET_SITE_DISABLED = "site_disabled"
BUDGET_JOB_DISABLED = "job_disabled"
BUDGET_GRANTED = "granted"

# Execution errors.
EXEC_TIMEOUT = "EXEC_TIMEOUT"
EXEC_TRANSPORT_ERROR = "EXEC_TRANSPORT_ERROR"
EXEC_REDIRECT_BLOCKED = "EXEC_REDIRECT_BLOCKED"
EXEC_BODY_TOO_LARGE = "EXEC_BODY_TOO_LARGE"
EXEC_PREFLIGHT_BLOCKED = "EXEC_PREFLIGHT_BLOCKED"

# Storage/report/CLI errors.
STORE_NOT_FOUND = "STORE_NOT_FOUND"
REPORT_MISSING = "REPORT_MISSING"
GOLDEN_MISMATCH = "GOLDEN_MISMATCH"
GOLDEN_INPUT_MISSING = "GOLDEN_INPUT_MISSING"
DEMO_PORT_IN_USE = "DEMO_PORT_IN_USE"
DEMO_START_FAILED = "DEMO_START_FAILED"
CLI_INVALID_ARGUMENT = "CLI_INVALID_ARGUMENT"
CLI_UNEXPECTED = "CLI_UNEXPECTED"


@dataclass(frozen=True)
class ConfigIssue:
    """One configuration validation problem."""

    code: str
    path: str
    message: str

    def as_dict(self) -> dict[str, str]:
        return {"code": self.code, "path": self.path, "message": self.message}


class CrawlBudgetError(Exception):
    """Base exception carrying a machine-readable code."""

    code = CLI_UNEXPECTED
    exit_code = 1

    def __init__(self, message: str, *, details: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.details = details or {}

    def as_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {"code": self.code, "message": self.message}
        if self.details:
            payload["details"] = self.details
        return payload


class ConfigError(CrawlBudgetError):
    code = "CFG_INVALID"
    exit_code = 2

    def __init__(self, issues: list[ConfigIssue]) -> None:
        self.issues = tuple(issues)
        message = "configuration validation failed"
        super().__init__(message, details={"issues": [i.as_dict() for i in issues]})

    def __str__(self) -> str:
        return "\n".join(
            f"{issue.code} {issue.path}: {issue.message}" for issue in self.issues
        )


class StorageError(CrawlBudgetError):
    code = STORE_NOT_FOUND
    exit_code = 1


class GoldenError(CrawlBudgetError):
    code = GOLDEN_MISMATCH
    exit_code = 3


class DemoError(CrawlBudgetError):
    code = DEMO_START_FAILED
    exit_code = 4


class CliError(CrawlBudgetError):
    code = CLI_INVALID_ARGUMENT
    exit_code = 2
