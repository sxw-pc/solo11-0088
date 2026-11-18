"""Pre-committed sampling, real receipts, and population certificates."""

from __future__ import annotations

import json

import pytest

from crawlbudget.cli import main
from crawlbudget.config import load_config
from crawlbudget.document_completeness import inspect_documents
from crawlbudget.pipeline import run_pipeline
from crawlbudget.quality_sampling import draw_frame, load_frame
from crawlbudget.storage import Storage
from crawlbudget.util import fixture_bytes
from tests.fakes import FakeResponse, ReplaySession
from tests.quality_support import (
    BASE, CONFIG, FRAME, FULL, INTAKE, PUBLIC, ROOT, Case, four_contracts, html,
)


def test_fixture_contracts_diverge_and_use_exact_bounds(tmp_path):
    config = load_config(CONFIG, project_root=ROOT)
    frame = load_frame(FRAME, config.sites["lease-demo"])
    manifest = json.loads(INTAKE.read_text(encoding="utf-8"))
    routes = {url: [FakeResponse(200, body=(ROOT / manifest["body_paths"][url]).read_bytes(),
                                 headers={"Content-Type": "text/html"})]
              for url in draw_frame(frame)}
    with Storage(":memory:") as storage:
        run_pipeline(config, storage, "metadata-case", reports_dir=tmp_path,
                     session=ReplaySession(routes), sleeper=lambda _: None,
                     quality_frame=frame, quality_contracts=manifest["contracts"])
        result = inspect_documents(storage, "metadata-case", frame, INTAKE)

    assert result["frame_size"] == 6 and result["sample_size"] == 4
    full, public = result["contracts"]["full_metadata"], result["contracts"]["public_index"]
    assert (full["qualified_count"], full["lower_bound"]) == (3, 3)
    assert full["lower_bound_fraction"]["text"] == "3/6"
    assert full["decision"] == "refused" and full["reason"] == "insufficient_evidence"
    assert (public["qualified_count"], public["lower_bound"]) == (4, 5)
    assert public["lower_bound_fraction"]["text"] == "5/6"
    assert public["decision"] == "signed"
    assert result["population_certificate"]["full_metadata"] is None
    assert result["population_certificate"]["public_index"] is not None
    assert full["observed_sample_fraction"]["is_population_estimate"] is False
    assert (full["N"], full["n"], full["x"]) == (6, 4, 3)
    assert (public["N"], public["n"], public["x"]) == (6, 4, 4)
    assert full["issued"] is False and public["issued"] is True
    assert full["tail_probabilities"] == {"0": "0/1", "1": "0/1", "2": "0/1",
                                          "3": "1/5", "4": "3/5", "5": "1/1", "6": "1/1"}
    assert result["x"] == {"full_metadata": 3, "public_index": 4}
    assert result["decision_reasons"] == {"full_metadata": "insufficient_evidence",
                                          "public_index": "lower_bound_meets_minimum"}
    assert result["certificates"] == result["contracts"]
    # Candidate tails: K=4 is not rejected for x=3, and K=4 is rejected for x=4.
    tails = {row["population_successes"]: row["tail_probability"]["text"]
             for row in full["candidate_bounds"]}
    assert tails == {0: "0/1", 1: "0/1", 2: "0/1", 3: "1/5", 4: "3/5", 5: "1/1", 6: "1/1"}
    assert public["candidate_bounds"][4]["rejected_at_alpha"] is True
    assert public["candidate_bounds"][5]["tail_probability"]["text"] == "1/3"
    # Certificate enumerates pre-execution fingerprints, receipts, and field states.
    commitment = result["commitment"]
    assert len(commitment["frame_urls"]) == 6
    assert len(commitment["sample_urls"]) == 4
    assert commitment["commitment_digest"] and commitment["version_digest"]
    assert result["run"]["config_fingerprint"]
    e_row = next(row for row in result["rows"] if row["url"].endswith("e.html"))
    assert e_row["fields"]["full_metadata"]["states"]["license"]["state"] == "missing"
    assert e_row["fields"]["public_index"]["states"]["title"]["state"] == "present"
    assert e_row["receipt_status"] == "valid"
    assert e_row["receipt"]["body_sha256"] == e_row["body_sha256"]


def test_threshold_boundary_is_lower_bound_fraction(tmp_path):
    urls = [f"{BASE}x{index}.html" for index in range(4)]
    good = {url: html() for url in urls}
    case = Case(tmp_path, urls, good, four_contracts({"full_metadata": FULL}),
                seed="b1", sample_size=3)
    assert len(case.selected) == 3
    result = case.run(tmp_path)
    full = result["contracts"]["full_metadata"]
    assert (full["qualified_count"], full["lower_bound"], full["decision"]) == (3, 3, "signed")
    assert full["lower_bound_fraction"]["text"] == "3/4"

    # One missing license: x=2, lower=2, below the 3/4 threshold.
    mixed = dict(good)
    mixed[case.selected[0]] = html(license=None)
    case = Case(tmp_path / "mixed", urls, mixed, four_contracts({"full_metadata": FULL}),
                seed="b1", sample_size=3)
    result = case.run(tmp_path / "mixed")
    full = result["contracts"]["full_metadata"]
    assert (full["qualified_count"], full["lower_bound"], full["decision"]) == (2, 2, "refused")


def test_sample_identity_and_event_changes_move_the_certificate(tmp_path):
    urls = [f"{BASE}y{index}.html" for index in range(4)]
    bodies = {url: html() for url in urls}
    bodies[urls[0]] = html(license=None)
    contracts = four_contracts({"full_metadata": FULL})
    # Seed b67 draws y1,y2,y3 (all qualified); b3 draws y3,y1,y0 (x=2).
    first = Case(tmp_path / "one", urls, bodies, contracts, seed="b67", sample_size=3)
    second = Case(tmp_path / "two", urls, bodies, contracts, seed="b3", sample_size=3)
    signed = first.run(tmp_path / "one")
    refused = second.run(tmp_path / "two")
    assert signed["contracts"]["full_metadata"]["decision"] == "signed"
    assert refused["contracts"]["full_metadata"]["decision"] == "refused"
    assert set(signed["commitment"]["sample_urls"]) != set(refused["commitment"]["sample_urls"])

    # Same committed sample, one failed receipt: eligibility, not seed, changes.
    failed = Case(tmp_path / "three", urls, bodies, contracts, seed="b67", sample_size=3)
    broken = dict(failed.routes)
    target = failed.selected[0]
    broken[target] = [FakeResponse(500), FakeResponse(500)]
    result = failed.run(tmp_path / "three", session=ReplaySession(broken))
    full = result["contracts"]["full_metadata"]
    assert full["qualified_count"] == 2 and full["decision"] == "refused"
    row = next(row for row in result["rows"] if row["url"] == target)
    assert row["attempts"] == 2 and row["receipt_status"] == "missing"
    assert row["unqualified_reason"] == "no_success_receipt"


@pytest.mark.parametrize("mutation,reason", [
    (lambda session, url: session.__setitem__(
        url, [FakeResponse(200, body=b"changed", headers={"Content-Type": "text/html"})]),
     "success_receipt_does_not_match_capture"),
    (lambda session, url: session.__setitem__(
        url, [FakeResponse(200, body=html(), headers={"Content-Type": "text/plain"})]),
     "success_receipt_does_not_match_capture"),
])
def test_bad_receipt_is_unqualified_and_not_replaced(tmp_path, mutation, reason):
    urls = [f"{BASE}z{index}.html" for index in range(4)]
    bodies = {url: html() for url in urls}
    case = Case(tmp_path, urls, bodies, four_contracts({"public_index": PUBLIC}),
                seed="b1", sample_size=3)
    routes = dict(case.routes)
    mutation(routes, case.selected[0])
    result = case.run(tmp_path, session=ReplaySession(routes))
    public = result["contracts"]["public_index"]
    assert public["qualified_count"] == 2 and public["decision"] == "refused"
    row = next(row for row in result["rows"] if row["url"] == case.selected[0])
    assert row["unqualified_reason"] == reason
    assert result["sample_size"] == 3  # failed URL is judged, never redrawn


def test_retry_is_one_document_and_eventual_success_qualifies(tmp_path):
    urls = [f"{BASE}r{index}.html" for index in range(4)]
    bodies = {url: html() for url in urls}
    case = Case(tmp_path, urls, bodies, four_contracts({"public_index": PUBLIC}),
                seed="b1", sample_size=3, retries=1)
    routes = dict(case.routes)
    flaky = case.selected[0]
    routes[flaky] = [FakeResponse(500), FakeResponse(200, body=bodies[flaky],
                                                   headers={"Content-Type": "text/html"})]
    result = case.run(tmp_path, session=ReplaySession(routes))
    row = next(row for row in result["rows"] if row["url"] == flaky)
    assert row["attempts"] == 2 and row["receipt_status"] == "valid"
    assert result["sample_size"] == 3 and len(result["rows"]) == 3
    assert result["contracts"]["public_index"]["qualified_count"] == 3
    assert result["contracts"]["public_index"]["decision"] == "signed"


@pytest.mark.parametrize("kwargs,reason", [
    ({"duplicate": "license"}, "duplicate"),
    ({"empty": "license"}, "empty"),
])
def test_duplicate_or_empty_field_is_unqualified(tmp_path, kwargs, reason):
    urls = [f"{BASE}f{index}.html" for index in range(4)]
    bodies = {url: html() for url in urls}
    affected = urls[1]
    bodies[affected] = html(**kwargs)
    case = Case(tmp_path, urls, bodies, four_contracts({"full_metadata": FULL}),
                seed="b1", sample_size=3)
    assert affected in case.selected
    result = case.run(tmp_path)
    row = next(row for row in result["rows"] if row["url"] == affected)
    assert row["fields"]["full_metadata"]["states"]["license"]["state"] == reason
    assert row["qualified"]["full_metadata"] is False


def test_no_usable_sample_refuses_without_claiming_population(tmp_path):
    urls = [f"{BASE}n{index}.html" for index in range(4)]
    bodies = {url: html() for url in urls}
    case = Case(tmp_path, urls, bodies, four_contracts({"public_index": PUBLIC}),
                seed="b1", sample_size=3, retries=0)
    routes = {url: [FakeResponse(500)] for url in case.selected}
    result = case.run(tmp_path, session=ReplaySession(routes))
    public = result["contracts"]["public_index"]
    assert public["qualified_count"] == 0 and public["lower_bound"] == 0
    assert public["decision"] == "refused" and public["reason"] == "no_qualified_sample"
    assert result["population_certificate"]["public_index"] is None
    assert len(public["candidate_bounds"]) == 5


def test_license_change_flips_full_metadata_decision(tmp_path):
    config = load_config(CONFIG, project_root=ROOT)
    frame = load_frame(FRAME, config.sites["lease-demo"])
    manifest = json.loads(INTAKE.read_text(encoding="utf-8"))
    modified = {}
    for url, reference in manifest["body_paths"].items():
        data = (ROOT / reference).read_bytes()
        if url.endswith("e.html"):
            data = data.replace(b"</head>", b'<meta name="license" content="CC0"></head>')
            assert b'name="license"' in data
        modified[url] = data
    body_dir = tmp_path / "bodies"
    body_dir.mkdir()
    paths = {}
    for index, (url, data) in enumerate(sorted(modified.items())):
        (body_dir / f"{index}.html").write_bytes(data)
        paths[url] = str(body_dir / f"{index}.html")
    intake_path = tmp_path / "intake.json"
    intake_path.write_text(json.dumps({
        "frame_path": str(FRAME), "body_paths": paths,
        "contracts": manifest["contracts"],
    }), encoding="utf-8")
    routes = {url: [FakeResponse(200, body=data, headers={"Content-Type": "text/html"})]
              for url, data in modified.items()}
    with Storage(":memory:") as storage:
        run_pipeline(config, storage, "license-case", reports_dir=tmp_path,
                     session=ReplaySession(routes), sleeper=lambda _: None,
                     quality_frame=frame, quality_contracts=manifest["contracts"])
        result = inspect_documents(storage, "license-case", frame, intake_path)
    full = result["contracts"]["full_metadata"]
    assert (full["qualified_count"], full["lower_bound"], full["decision"]) == (4, 5, "signed")


def test_cli_commits_frame_before_get_and_reports_both_decisions(tmp_path, capsys):
    bodies = {}
    manifest = json.loads(INTAKE.read_text(encoding="utf-8"))
    for url, reference in manifest["body_paths"].items():
        # The mock site normalises fixture line endings, so capture the same bytes.
        bodies[url] = fixture_bytes(ROOT / reference)
    pages = tmp_path / "pages"
    pages.mkdir()
    for index, (url, data) in enumerate(sorted(bodies.items())):
        (pages / f"p{index}.html").write_bytes(data)
    manifest_path = tmp_path / "manifest.yaml"
    entries = "\n".join(
        f"  {url.rsplit('/', 1)[-1]}:\n    file: p{index}.html"
        for index, url in enumerate(sorted(bodies))
    )
    manifest_path.write_text(f"lease-demo:\n{entries}\n", encoding="utf-8")
    frame_path = tmp_path / "frame.json"
    # The population is all six frame URLs; only the drawn four have captures.
    frame_path.write_text(json.dumps({
        "site_id": "lease-demo", "seed": "quality-audit-1", "sample_size": 4,
        "urls": json.loads(FRAME.read_text(encoding="utf-8"))["urls"],
    }), encoding="utf-8")
    intake_path = tmp_path / "intake.json"
    intake_path.write_text(json.dumps({
        "frame_path": str(frame_path), "body_paths": {
            url: str(pages / f"p{index}.html")
            for index, url in enumerate(sorted(bodies))
        }, "contracts": manifest["contracts"],
    }), encoding="utf-8")
    config_path = tmp_path / "config.yaml"
    config_path.write_text(f"""
sites:
  - site_id: lease-demo
    base_url: {BASE}
    robots_file: {ROOT / 'fixtures/robots/lease-demo.txt'}
    daily_quota: 20
    max_requests_per_run: 20
    max_rps: 20
    burst: 2
    crawl_delay_ms: 0
    max_response_bytes: 4096
    max_retries: 0
    enabled: true
jobs:
  - job_id: quality
    site_id: lease-demo
    priority: 1
    seed_file: {ROOT / 'fixtures/seeds/quality-sample.txt'}
    enabled: true
""", encoding="utf-8")
    db = tmp_path / "ledger.sqlite3"
    common = ["--config", str(config_path), "--db", str(db),
              "--reports-dir", str(tmp_path / "reports"),
              "--pages-dir", str(pages), "--manifest", str(manifest_path),
              "--project-root", str(tmp_path)]
    assert main(["run", *common, "--run-id", "cli-quality",
                 "--quality-intake", str(intake_path)]) == 0
    capsys.readouterr()
    with Storage(db) as storage:
        commitment = storage.get_quality_commitment("cli-quality")
        assert commitment is not None
        assert storage.attempt_count("cli-quality") > 0
    code = main(["quality-audit", "--config", str(config_path), "--db", str(db),
                 "--project-root", str(tmp_path), "--run-id", "cli-quality",
                 "--intake", str(intake_path)])
    assert code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["decisions"] == {"full_metadata": "refused",
                                    "public_index": "signed"}
    assert payload["contracts"]["full_metadata"]["lower_bound_fraction"]["text"] == "3/6"
    assert payload["contracts"]["public_index"]["lower_bound_fraction"]["text"] == "5/6"


def test_missing_field_cannot_be_borrowed_from_another_page(tmp_path):
    urls = [f"{BASE}c{index}.html" for index in range(4)]
    bodies = {url: html() for url in urls}
    # The sampled e-equivalent lacks license; other pages still carry it.
    affected = urls[1]
    bodies[affected] = html(license=None)
    case = Case(tmp_path, urls, bodies, four_contracts({"full_metadata": FULL,
                                                        "public_index": PUBLIC}),
                seed="b1", sample_size=3)
    assert affected in case.selected
    result = case.run(tmp_path)
    row = next(row for row in result["rows"] if row["url"] == affected)
    assert row["qualified"]["full_metadata"] is False
    assert row["qualified"]["public_index"] is True
    assert row["fields"]["full_metadata"]["states"]["license"]["state"] == "missing"
    assert result["contracts"]["full_metadata"]["qualified_count"] == 2
    assert result["contracts"]["public_index"]["qualified_count"] == 3


def test_missing_capture_is_unqualified_and_not_replaced(tmp_path):
    urls = [f"{BASE}m{index}.html" for index in range(4)]
    bodies = {url: html() for url in urls}
    case = Case(tmp_path, urls, bodies, four_contracts({"public_index": PUBLIC}),
                seed="b1", sample_size=3)
    missing = case.selected[0]
    intake = json.loads(case.intake_path.read_text(encoding="utf-8"))
    del intake["body_paths"][missing]
    case.intake_path.write_text(json.dumps(intake), encoding="utf-8")
    result = case.run(tmp_path)
    row = next(row for row in result["rows"] if row["url"] == missing)
    assert row["unqualified_reason"] == "captured_body_missing"
    assert row["receipt_status"] == "invalid"
    assert result["contracts"]["public_index"]["qualified_count"] == 2
    assert result["sample_size"] == 3 and len(result["rows"]) == 3
