"""Shared fixtures for the crawlbudget test suite."""

import shutil
from pathlib import Path

import pytest
import yaml

from crawlbudget.config import load_config
from crawlbudget.models import RunRecord
from crawlbudget.planner import build_plan

ROOT = Path(__file__).resolve().parent.parent
FIXTURES = ROOT / "fixtures"


@pytest.fixture
def demo_config():
    return load_config(FIXTURES / "demo_sites.yaml", project_root=ROOT)


@pytest.fixture
def demo_plan(demo_config):
    return build_plan(demo_config, "test-run", "2026-01-15T00:00:00Z")


@pytest.fixture
def config_factory(tmp_path):
    """Create a valid config in tmp_path, optionally mutating the raw demo YAML."""

    def make(mutator=None, *, name: str = "config.yaml"):
        raw = yaml.safe_load((FIXTURES / "demo_sites.yaml").read_text(encoding="utf-8"))
        for site in raw["sites"]:  # absolute refs keep the copy valid outside fixtures/
            site["robots_file"] = str((FIXTURES / site["robots_file"]).resolve())
        for job in raw["jobs"]:
            job["seed_file"] = str((FIXTURES / job["seed_file"]).resolve())
        if mutator is not None:
            mutator(raw)
        target = tmp_path / name
        target.write_text(yaml.safe_dump(raw, sort_keys=False), encoding="utf-8", newline="\n")
        return load_config(target, project_root=ROOT), target, raw

    return make


@pytest.fixture
def copy_fixture_tree(tmp_path):
    def copy() -> Path:
        target = tmp_path / "fixtures"
        shutil.copytree(FIXTURES, target)
        return target

    return copy


def prepare_plan(storage, plan) -> None:
    """Persist a plan's run, leases, and planned requests for executor tests."""
    storage.create_run(RunRecord(
        run_id=plan.run_id, created_at=plan.virtual_start, started_at=plan.virtual_start,
        finished_at=plan.virtual_start, virtual_date="2026-01-15", dry_run=False,
        status="running", config_fingerprint="c" * 64, seeds_fingerprint="s" * 64,
        robots_fingerprint="r" * 64,
    ))
    for lease in plan.leases:
        storage.upsert_lease(lease)
    for request in plan.requests:
        storage.insert_planned(request)


def write_simple_site_config(tmp_path: Path, port: int) -> tuple[Path, Path, Path, Path]:
    """Write a hermetic one-site demo used by CLI integration tests."""
    pages = tmp_path / "pages"
    pages.mkdir()
    robots = tmp_path / "robots.txt"
    robots.write_text("User-agent: crawlbudget\nAllow: /mock-alpha/\n", encoding="utf-8")
    seed = tmp_path / "seed.txt"
    seed.write_text(
        f"http://127.0.0.1:{port}/mock-alpha/index.html\n"
        f"http://127.0.0.1:{port}/mock-alpha/missing.html\n",
        encoding="utf-8",
    )
    (pages / "index.html").write_text(
        "<!doctype html><html><body><h1>CLI fixture</h1></body></html>\n",
        encoding="utf-8", newline="\n",
    )
    manifest = pages / "manifest.yaml"
    manifest.write_text("mock-alpha:\n  index.html:\n    file: index.html\n",
                        encoding="utf-8", newline="\n")
    config = tmp_path / "demo.yaml"
    config.write_text(
        f"""
sites:
  - site_id: mock-alpha
    base_url: http://127.0.0.1:{port}/mock-alpha/
    robots_file: {robots.name}
    daily_quota: 3
    max_requests_per_run: 3
    max_rps: 50
    burst: 2
    crawl_delay_ms: 1
    max_response_bytes: 4096
    max_retries: 0
    enabled: true
jobs:
  - job_id: job-cli
    site_id: mock-alpha
    priority: 1
    seed_file: {seed.name}
    enabled: true
""",
        encoding="utf-8", newline="\n",
    )
    return config, seed, pages, manifest
