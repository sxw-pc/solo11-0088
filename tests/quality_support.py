"""Hermetic fixtures and configs for quality-certificate tests."""

from __future__ import annotations

import json
from pathlib import Path

from crawlbudget.config import load_config
from crawlbudget.quality_sampling import draw_frame
from crawlbudget.storage import Storage
from crawlbudget.pipeline import run_pipeline
from crawlbudget.document_completeness import inspect_documents
from tests.fakes import FakeResponse, ReplaySession

ROOT = Path(__file__).resolve().parents[1]
INTAKE = ROOT / "fixtures/document_completeness/intake.json"
FRAME = ROOT / "fixtures/quality/frame.json"
CONFIG = ROOT / "fixtures/quality_sampling.yaml"
BASE = "http://127.0.0.1:5021/lease-demo/"
FULL = {"required_fields": ["title", "owner", "updated", "license"],
        "minimum_population_fraction": "3/4"}
PUBLIC = {"required_fields": ["title", "owner", "updated"],
          "minimum_population_fraction": "3/4"}


def html(*, license="CC0", duplicate=None, empty=None) -> bytes:
    fields = {"title": "Doc", "owner": "Library", "updated": "2025-11-16"}
    if license is not None:
        fields["license"] = license
    if empty:
        fields[empty] = ""
    metas = "".join(f'<meta name="{k}" content="{v}">' for k, v in fields.items())
    if duplicate:
        metas += f'<meta name="{duplicate}" content="second">'
    return f"<!doctype html><html><head>{metas}</head><body>x</body></html>".encode()


class Case:
    def __init__(self, tmp_path, urls, bodies, contracts, seed, sample_size, retries=1):
        self.urls, self.bodies, self.contracts = urls, bodies, contracts
        self.frame = {"site_id": "lease-demo", "seed": seed,
                      "sample_size": sample_size, "urls": urls}
        self.selected = draw_frame(self.frame)
        self.body_paths = {}
        body_dir = tmp_path / "bodies"
        body_dir.mkdir(parents=True)
        for index, url in enumerate(urls):
            if url not in bodies:
                continue
            path = body_dir / f"body-{index}.html"
            path.write_bytes(bodies[url])
            self.body_paths[url] = str(path)
        self.frame_path = tmp_path / "frame.json"
        self.frame_path.write_text(json.dumps(self.frame), encoding="utf-8")
        self.intake_path = tmp_path / "intake.json"
        self.intake_path.write_text(json.dumps({
            "frame_path": str(self.frame_path), "body_paths": self.body_paths,
            "contracts": contracts,
        }), encoding="utf-8")
        seed_file = tmp_path / "seed.txt"
        seed_file.write_text("\n".join(self.selected) + "\n", encoding="utf-8")
        robots = tmp_path / "robots.txt"
        robots.write_text("User-agent: crawlbudget\nAllow: /lease-demo/\n", encoding="utf-8")
        self.config = load_config(self._write_config(tmp_path, seed_file, robots, retries),
                                  project_root=tmp_path)
        self.routes = {url: [FakeResponse(200, body=bodies[url],
                                          headers={"Content-Type": "text/html"})]
                       for url in self.selected if url in bodies}

    @staticmethod
    def _write_config(tmp_path, seed_file, robots, retries):
        path = tmp_path / "config.yaml"
        path.write_text(f"""
sites:
  - site_id: lease-demo
    base_url: {BASE}
    robots_file: {robots}
    daily_quota: 20
    max_requests_per_run: 20
    max_rps: 20
    burst: 2
    crawl_delay_ms: 0
    max_response_bytes: 4096
    max_retries: {retries}
    enabled: true
jobs:
  - job_id: quality
    site_id: lease-demo
    priority: 1
    seed_file: {seed_file}
    enabled: true
""", encoding="utf-8")
        return path

    def run(self, tmp_path, session=None, run_id="case"):
        with Storage(":memory:") as storage:
            run_pipeline(self.config, storage, run_id, reports_dir=tmp_path,
                         session=session or ReplaySession(self.routes),
                         sleeper=lambda _: None, quality_frame=self.frame,
                         quality_contracts=self.contracts)
            return inspect_documents(storage, run_id, self.frame, self.intake_path)


def four_contracts(contracts):
    return {name: {"required_fields": value["required_fields"],
                   "minimum_population_fraction": value["minimum_population_fraction"]}
            for name, value in contracts.items()}


def write_frame(path: Path, urls: list[str], sample_size: int) -> Path:
    path.write_text(json.dumps({"site_id": "lease-demo", "seed": "b1",
                                "sample_size": sample_size, "urls": urls}),
                    encoding="utf-8")
    return path
