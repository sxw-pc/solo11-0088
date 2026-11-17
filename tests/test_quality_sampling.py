"""A deterministic quality sample and its observed retry paths."""

from pathlib import Path

import pytest

from crawlbudget.config import load_config
from crawlbudget.pipeline import run_pipeline
from crawlbudget.quality_sampling import audit_sample, draw_frame, load_frame
from crawlbudget.storage import Storage
from tests.fakes import FakeResponse, ReplaySession


ROOT = Path(__file__).resolve().parent.parent
FRAME = ROOT / "fixtures/quality/frame.json"


def test_sample_draw_and_observation_keep_retry_as_one_target(tmp_path):
    config = load_config(ROOT / "fixtures/quality_sampling.yaml", project_root=ROOT)
    frame = load_frame(FRAME, config.sites["lease-demo"])
    selected = draw_frame(frame)
    seeds = (ROOT / "fixtures/seeds/quality-sample.txt").read_text(encoding="utf-8").splitlines()
    assert selected == seeds
    routes = {url: [FakeResponse(200, body=b"quality")] for url in selected}
    flaky = next(url for url in selected if url.endswith("e.html"))
    routes[flaky] = [FakeResponse(500), FakeResponse(200, body=b"quality")]
    with Storage(":memory:") as storage:
        run_pipeline(config, storage, "sample-case", reports_dir=tmp_path,
                     session=ReplaySession(routes), sleeper=lambda _: None)
        result = audit_sample(storage, "sample-case", frame)
    assert result["frame_size"] == 6
    assert result["sample_size"] == 4
    assert sum(row["first_success"] for row in result["observations"]) == 3
    assert sum(row["eventual_success"] for row in result["observations"]) == 4
    assert next(row for row in result["observations"] if row["url"] == flaky)["attempts"] == 2


def test_frame_rejects_external_and_duplicate_urls(tmp_path):
    config = load_config(ROOT / "fixtures/quality_sampling.yaml", project_root=ROOT)
    source = FRAME.read_text(encoding="utf-8")
    duplicate = tmp_path / "duplicate.json"
    duplicate.write_text(source.replace("/f.html", "/e.html"), encoding="utf-8")
    with pytest.raises(ValueError, match="duplicate URLs"):
        load_frame(duplicate, config.sites["lease-demo"])
    external = tmp_path / "external.json"
    external.write_text(source.replace("127.0.0.1:5021", "example.invalid:5021"), encoding="utf-8")
    with pytest.raises(ValueError, match="out-of-scope"):
        load_frame(external, config.sites["lease-demo"])
