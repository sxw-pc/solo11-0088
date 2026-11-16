"""Declarative YAML loading and aggregate configuration validation."""

from __future__ import annotations

from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import yaml

from .errors import (
    CFG_DUPLICATE_JOB,
    CFG_DUPLICATE_SITE,
    CFG_INVALID_URL,
    CFG_INVALID_VALUE,
    CFG_INVALID_YAML,
    CFG_MISSING_FIELD,
    CFG_NON_LOOPBACK_TARGET,
    CFG_NON_POSITIVE_LIMIT,
    CFG_SEED_FILE_MISSING,
    CFG_UNKNOWN_SITE,
    ConfigError,
    ConfigIssue,
)
from .models import ConfigBundle, JobConfig, SiteConfig
from .util import fingerprint, is_loopback_host, normalize_path, read_fixture_text

REQUIRED_SITE_FIELDS = (
    "site_id",
    "base_url",
    "robots_file",
    "daily_quota",
    "max_requests_per_run",
    "max_rps",
    "burst",
    "crawl_delay_ms",
    "max_response_bytes",
)
REQUIRED_JOB_FIELDS = ("job_id", "site_id", "priority", "seed_file", "enabled")


def load_config(path: str | Path, project_root: str | Path | None = None) -> ConfigBundle:
    source = Path(path).resolve()
    root = Path(project_root).resolve() if project_root else Path.cwd().resolve()
    if not source.exists():
        raise ConfigError([ConfigIssue(CFG_INVALID_VALUE, source.name, "config file not found")])
    try:
        raw = yaml.safe_load(read_fixture_text(source))
    except (yaml.YAMLError, UnicodeDecodeError) as exc:
        raise ConfigError([ConfigIssue(CFG_INVALID_YAML, source.name, str(exc))]) from exc
    if not isinstance(raw, dict):
        raise ConfigError([ConfigIssue(CFG_INVALID_YAML, "$", "top level must be a mapping")])
    return _validate(raw, source, root)


def _validate(raw: dict[str, Any], source: Path, root: Path) -> ConfigBundle:
    errors: list[ConfigIssue] = []
    raw_sites = _list_field(raw, "sites", errors)
    raw_jobs = _list_field(raw, "jobs", errors)

    sites: dict[str, SiteConfig] = {}
    seen_sites: set[str] = set()
    for index, item in enumerate(raw_sites):
        prefix = f"sites[{index}]"
        site = _parse_site(item, index, prefix, source, root, errors)
        if site is None:
            continue
        if site.site_id in seen_sites:
            errors.append(ConfigIssue(
                CFG_DUPLICATE_SITE, f"{prefix}.site_id", f"duplicate {site.site_id}"
            ))
            continue
        seen_sites.add(site.site_id)
        sites[site.site_id] = site

    jobs: list[JobConfig] = []
    seen_jobs: set[str] = set()
    for index, item in enumerate(raw_jobs):
        prefix = f"jobs[{index}]"
        job = _parse_job(item, index, prefix, source, root, sites, errors)
        if job is None:
            continue
        if job.job_id in seen_jobs:
            errors.append(ConfigIssue(
                CFG_DUPLICATE_JOB, f"{prefix}.job_id", f"duplicate {job.job_id}"
            ))
            continue
        seen_jobs.add(job.job_id)
        jobs.append(job)

    if errors:
        raise ConfigError(errors)

    site_fields = REQUIRED_SITE_FIELDS + ("max_retries", "timeout_ms", "enabled")
    config_fp = fingerprint({
        "sites": [{key: item.get(key) for key in site_fields} for item in raw_sites],
        "jobs": [{key: item.get(key) for key in REQUIRED_JOB_FIELDS} for item in raw_jobs],
    })
    seeds_fp = fingerprint({job.job_id: _file_fingerprint(job.seed_file) for job in jobs})
    robots_fp = fingerprint({
        site_id: _file_fingerprint(site.robots_file) for site_id, site in sites.items()
    })
    return ConfigBundle(
        sites=sites, jobs=tuple(jobs), config_fingerprint=config_fp,
        seeds_fingerprint=seeds_fp, robots_fingerprint=robots_fp, source_path=source,
    )


def _list_field(raw: dict[str, Any], key: str, errors: list[ConfigIssue]) -> list[Any]:
    value = raw.get(key)
    if not isinstance(value, list):
        errors.append(ConfigIssue(CFG_MISSING_FIELD, key, "must be a non-empty list"))
        return []
    if not value:
        errors.append(ConfigIssue(CFG_MISSING_FIELD, key, f"at least one {key[:-1]} is required"))
    return value


def _parse_site(
    item: Any, index: int, prefix: str, source: Path, root: Path,
    errors: list[ConfigIssue],
) -> SiteConfig | None:
    if not isinstance(item, dict):
        errors.append(ConfigIssue(CFG_INVALID_VALUE, prefix, "site entry must be a mapping"))
        return None
    for key in REQUIRED_SITE_FIELDS:
        if key not in item:
            errors.append(ConfigIssue(CFG_MISSING_FIELD, f"{prefix}.{key}", "field is required"))
    site_id = _required_string(item, "site_id", prefix, errors)
    base_url = _required_string(item, "base_url", prefix, errors)
    robots_ref = _required_string(item, "robots_file", prefix, errors)
    values: dict[str, Any] = {}
    for key in ("daily_quota", "max_requests_per_run", "burst", "max_response_bytes"):
        values[key] = _required_int(item, key, prefix, errors)
        if values[key] is not None and values[key] <= 0:
            errors.append(ConfigIssue(CFG_NON_POSITIVE_LIMIT, f"{prefix}.{key}", "must be > 0"))
            values[key] = None
    max_rps = item.get("max_rps")
    if "max_rps" in item:
        if isinstance(max_rps, bool) or not isinstance(max_rps, (int, float)) or max_rps <= 0:
            errors.append(ConfigIssue(CFG_NON_POSITIVE_LIMIT, f"{prefix}.max_rps", "must be > 0"))
            max_rps = None
    crawl = _required_int(item, "crawl_delay_ms", prefix, errors)
    if crawl is not None and crawl < 0:
        errors.append(ConfigIssue(CFG_INVALID_VALUE, f"{prefix}.crawl_delay_ms", "must be >= 0"))
        crawl = None
    max_retries = item.get("max_retries", 0)
    if isinstance(max_retries, bool) or not isinstance(max_retries, int) or max_retries < 0:
        errors.append(ConfigIssue(CFG_INVALID_VALUE, f"{prefix}.max_retries", "must be >= 0"))
        max_retries = None
    timeout = item.get("timeout_ms", 2000)
    if isinstance(timeout, bool) or not isinstance(timeout, int) or timeout <= 0:
        errors.append(ConfigIssue(CFG_NON_POSITIVE_LIMIT, f"{prefix}.timeout_ms", "must be > 0"))
        timeout = None
    enabled = item.get("enabled", True)
    if not isinstance(enabled, bool):
        errors.append(ConfigIssue(CFG_INVALID_VALUE, f"{prefix}.enabled", "must be boolean"))
        enabled = None
    base_valid = _validate_base_url(base_url, f"{prefix}.base_url", errors) if base_url else None
    robots_path = _resolve_path(robots_ref, source, root) if robots_ref else None
    required = (site_id, base_url, robots_ref, *values.values(), max_rps, crawl,
                max_retries, timeout, enabled, robots_path)
    if None in required or base_valid is None:
        return None
    return SiteConfig(
        site_id=site_id, base_url=base_url, robots_file=robots_path,
        daily_quota=values["daily_quota"], max_requests_per_run=values["max_requests_per_run"],
        max_rps=float(max_rps), burst=values["burst"], crawl_delay_ms=crawl,
        max_response_bytes=values["max_response_bytes"], max_retries=max_retries,
        timeout_ms=timeout, enabled=enabled, robots_ref=robots_ref,
    )


def _parse_job(
    item: Any, index: int, prefix: str, source: Path, root: Path,
    sites: dict[str, SiteConfig], errors: list[ConfigIssue],
) -> JobConfig | None:
    if not isinstance(item, dict):
        errors.append(ConfigIssue(CFG_INVALID_VALUE, prefix, "job entry must be a mapping"))
        return None
    for key in REQUIRED_JOB_FIELDS:
        if key not in item:
            errors.append(ConfigIssue(CFG_MISSING_FIELD, f"{prefix}.{key}", "field is required"))
    job_id = _required_string(item, "job_id", prefix, errors)
    site_id = _required_string(item, "site_id", prefix, errors)
    seed_ref = _required_string(item, "seed_file", prefix, errors)
    priority = _required_int(item, "priority", prefix, errors)
    if priority is not None and priority < 0:
        errors.append(ConfigIssue(CFG_INVALID_VALUE, f"{prefix}.priority", "must be >= 0"))
        priority = None
    enabled = item.get("enabled")
    if not isinstance(enabled, bool):
        if "enabled" in item:
            errors.append(ConfigIssue(CFG_INVALID_VALUE, f"{prefix}.enabled", "must be boolean"))
        enabled = None
    if site_id and site_id not in sites:
        errors.append(ConfigIssue(CFG_UNKNOWN_SITE, f"{prefix}.site_id", f"unknown {site_id}"))
    seed_path = _resolve_path(seed_ref, source, root) if seed_ref else None
    if seed_path is not None and not seed_path.exists():
        errors.append(
            ConfigIssue(CFG_SEED_FILE_MISSING, f"{prefix}.seed_file", "seed file not found")
        )
        seed_path = None
    if None in (job_id, site_id, priority, enabled, seed_path):
        return None
    return JobConfig(
        job_id=job_id, site_id=site_id, priority=priority,
        seed_file=seed_path, enabled=enabled,
    )


def _validate_base_url(base_url: str, path: str, errors: list[ConfigIssue]) -> str | None:
    try:
        parsed = urlsplit(base_url)
        port = parsed.port
    except ValueError:
        errors.append(ConfigIssue(CFG_INVALID_URL, path, "malformed URL or port"))
        return None
    if parsed.scheme != "http" or not parsed.hostname or port is None:
        errors.append(ConfigIssue(CFG_INVALID_URL, path, "base_url must be http with an explicit port"))
        return None
    if parsed.username or parsed.password:
        errors.append(ConfigIssue(CFG_INVALID_URL, path, "userinfo is not allowed"))
        return None
    if parsed.query or parsed.fragment:
        errors.append(ConfigIssue(CFG_INVALID_URL, path, "query and fragment are not allowed"))
        return None
    if not is_loopback_host(parsed.hostname):
        errors.append(ConfigIssue(CFG_NON_LOOPBACK_TARGET, path, "host must be 127.0.0.1 or ::1"))
        return None
    normalized = normalize_path(parsed.path)
    if normalized != parsed.path or not normalized.endswith("/"):
        errors.append(ConfigIssue(CFG_INVALID_URL, path, "base path must be normalized and end with /"))
        return None
    return base_url


def _required_string(item: dict[str, Any], key: str, prefix: str,
                     errors: list[ConfigIssue]) -> str | None:
    if key not in item:
        return None
    value = item[key]
    if not isinstance(value, str) or not value.strip():
        errors.append(ConfigIssue(CFG_INVALID_VALUE, f"{prefix}.{key}", "must be a non-empty string"))
        return None
    return value.strip()


def _required_int(item: dict[str, Any], key: str, prefix: str,
                  errors: list[ConfigIssue]) -> int | None:
    if key not in item:
        return None
    value = item[key]
    if isinstance(value, bool) or not isinstance(value, int):
        errors.append(ConfigIssue(CFG_INVALID_VALUE, f"{prefix}.{key}", "must be an integer"))
        return None
    return value


def _resolve_path(ref: str, source: Path, root: Path) -> Path:
    candidate = Path(ref)
    if candidate.is_absolute():
        return candidate
    options = (source.parent / candidate, root / candidate)
    for option in options:
        if option.exists():
            return option.resolve()
    return (source.parent / candidate).resolve()

def _file_fingerprint(path: Path) -> str:
    if not path.exists():
        return "MISSING"
    return fingerprint(read_fixture_text(path))
