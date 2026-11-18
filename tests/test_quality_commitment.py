"""Commitment identity and exact hypergeometric bound mechanics."""

from __future__ import annotations

import json

import pytest

from crawlbudget.document_completeness import inspect_documents
from crawlbudget.quality_sampling import commit_quality_sample, load_frame
from crawlbudget.quality_stats import population_lower_bound
from crawlbudget.storage import Storage
from crawlbudget.pipeline import run_pipeline
from tests.fakes import ReplaySession
from tests.quality_support import (
    BASE, FULL, PUBLIC, Case, four_contracts, html, write_frame,
)


def test_post_hoc_seed_or_contract_swap_is_rejected(tmp_path):
    urls = [f"{BASE}s{index}.html" for index in range(4)]
    bodies = {url: html() for url in urls}
    case = Case(tmp_path, urls, bodies, four_contracts({"public_index": PUBLIC}),
                seed="b1", sample_size=3)
    with Storage(":memory:") as storage:
        run_pipeline(case.config, storage, "swap-case", reports_dir=tmp_path,
                     session=ReplaySession(case.routes), sleeper=lambda _: None,
                     quality_frame=case.frame, quality_contracts=case.contracts)
        changed = dict(case.frame, seed="b3")
        with pytest.raises(ValueError, match="frame or seed"):
            inspect_documents(storage, "swap-case", changed, case.intake_path)
        swapped = four_contracts({"public_index": {
            "required_fields": ["title"], "minimum_population_fraction": "3/4"}})
        with pytest.raises(ValueError, match="contracts do not match"):
            inspect_documents(storage, "swap-case", case.frame, case.intake_path,
                              contracts=swapped)
        with pytest.raises(ValueError, match="different quality sample commitment"):
            commit_quality_sample(storage, "swap-case", changed, case.contracts)
        with pytest.raises(ValueError, match="different quality sample commitment"):
            commit_quality_sample(storage, "swap-case", case.frame, swapped)


def test_commitment_must_precede_any_get(tmp_path):
    urls = [f"{BASE}p{index}.html" for index in range(4)]
    bodies = {url: html() for url in urls}
    case = Case(tmp_path, urls, bodies, four_contracts({"public_index": PUBLIC}),
                seed="b1", sample_size=3)
    with Storage(":memory:") as storage:
        run_pipeline(case.config, storage, "late-case", reports_dir=tmp_path,
                     session=ReplaySession(case.routes), sleeper=lambda _: None)
        with pytest.raises(ValueError, match="commitment is missing"):
            inspect_documents(storage, "late-case", case.frame, case.intake_path)
        with pytest.raises(ValueError, match="before any sampled GET"):
            commit_quality_sample(storage, "late-case", case.frame, case.contracts)


def test_reordering_frame_urls_or_contracts_is_identity_preserving(tmp_path):
    urls = [f"{BASE}o{index}.html" for index in range(4)]
    bodies = {url: html() for url in urls}
    contracts = four_contracts({"public_index": PUBLIC, "full_metadata": FULL})
    original = Case(tmp_path / "original", urls, bodies, contracts, seed="b1", sample_size=3)
    reordered_contracts = four_contracts({"full_metadata": FULL, "public_index": PUBLIC})
    reordered = Case(tmp_path / "reordered", list(reversed(urls)), bodies,
                     reordered_contracts, seed="b1", sample_size=3)
    first = original.run(tmp_path / "original")
    second = reordered.run(tmp_path / "reordered")
    assert first["commitment"]["frame_digest"] == second["commitment"]["frame_digest"]
    assert first["commitment"]["contracts_digest"] == second["commitment"]["contracts_digest"]
    assert first["commitment"]["commitment_digest"] == second["commitment"]["commitment_digest"]
    assert first["commitment"]["sample_urls"] == second["commitment"]["sample_urls"]
    assert first["decisions"] == second["decisions"]


def test_stat_bounds_are_exact_and_monotone():
    assert [population_lower_bound(6, 4, x) for x in range(5)] == [0, 1, 2, 3, 5]
    assert [population_lower_bound(4, 3, x) for x in range(4)] == [0, 1, 2, 3]
    assert population_lower_bound(30, 12, 12) == 26
    # N=8, n=4, x=4: lower bound 6/8 exactly equals the 3/4 minimum.
    assert population_lower_bound(8, 4, 4) == 6
    assert population_lower_bound(8, 4, 3) == 4
    for observed in range(5):
        bounds = [population_lower_bound(6, 4, value) for value in range(observed + 1)]
        assert bounds == sorted(bounds)


def test_larger_frame_meets_boundary_and_exposes_limits(tmp_path):
    urls = [f"{BASE}g{index}.html" for index in range(8)]
    bodies = {url: html() for url in urls}
    case = Case(tmp_path, urls, bodies, four_contracts({"full_metadata": FULL}),
                seed="b1", sample_size=4)
    result = case.run(tmp_path)
    full = result["contracts"]["full_metadata"]
    assert (full["qualified_count"], full["lower_bound"]) == (4, 6)
    assert full["lower_bound_fraction"]["text"] == "6/8"
    assert full["decision"] == "signed"
    with pytest.raises(ValueError, match="exceeds 30 URLs"):
        load_frame(write_frame(tmp_path / "big.json", [f"{BASE}u{i}.html" for i in range(31)],
                                sample_size=4), case.config.sites["lease-demo"])
    with pytest.raises(ValueError, match="exceeds 12"):
        load_frame(write_frame(tmp_path / "wide.json",
                                [f"{BASE}w{i}.html" for i in range(13)], sample_size=13),
                   case.config.sites["lease-demo"])
