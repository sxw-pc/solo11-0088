from pathlib import Path
from fractions import Fraction
import json

import pytest

from crawlbudget.cli import main as cli_main
from crawlbudget.landing_rights_intake import (
    compute_adaptive_policies,
    single_right_preview,
    verify_landing_intake,
)
from crawlbudget.pipeline import run_demo
from crawlbudget.storage import Storage

ROOT = Path(__file__).resolve().parents[1]
FIX = ROOT / "fixtures/landing_rights"
BODY = FIX / "mock-landing_landing.html"
INTAKE = FIX / "intake.json"
RUN = "landing-run"
ALL_WORLDS = ["w1", "w2", "w3", "w4", "w5", "w6", "w7"]
ALL_CREATIVES = ["K1", "K2", "K3", "K4", "K5", "K6"]


def _capture(tmp_path, *, pages_dir=FIX, manifest=FIX / "manifest.yaml", run_id=RUN):
    db = tmp_path / "landing.sqlite3"
    result = run_demo(FIX / "site.yaml", project_root=ROOT, db_path=db,
                      reports_dir=tmp_path / "reports", pages_dir=pages_dir,
                      manifest_path=manifest, run_id=run_id)
    assert result.status == "completed"
    return db


def _mutated_intake_path(tmp_path, mutate):
    data = json.loads(INTAKE.read_text(encoding="utf-8"))
    mutate(data)
    path = tmp_path / "intake.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


def _capture_with_page(tmp_path, html_text):
    pages = tmp_path / "pages"
    pages.mkdir()
    body = pages / "mock-landing_landing.html"
    body.write_text(html_text, encoding="utf-8", newline="\n")
    manifest = pages / "manifest.yaml"
    manifest.write_text((FIX / "manifest.yaml").read_text(encoding="utf-8"),
                        encoding="utf-8", newline="\n")
    db = _capture(tmp_path, pages_dir=pages, manifest=manifest)
    return db, body


def _leaf_map(contract):
    return {
        tuple((step["assay"], step["result"]) for step in leaf["path"]): leaf
        for leaf in contract["leaves"]
    }


def _check_leaves_partition_worlds(policies):
    for contract in policies["contracts"].values():
        seen = [w for leaf in contract["leaves"] for w in leaf["worlds"]]
        assert sorted(seen) == ALL_WORLDS
        for leaf in contract["leaves"]:
            assert leaf["worlds"], "every reachable branch must stay non-empty"


@pytest.fixture(scope="module")
def captured_db(tmp_path_factory):
    tmp = tmp_path_factory.mktemp("landing-capture")
    return _capture(tmp)


@pytest.fixture(scope="module")
def captured_intake(captured_db):
    with Storage(captured_db) as storage:
        return verify_landing_intake(storage, RUN, BODY, INTAKE)


def test_real_capture_yields_creative_rights_preview(tmp_path):
    db = tmp_path / "landing.sqlite3"
    result = run_demo(FIX / "site.yaml", project_root=ROOT, db_path=db,
                      reports_dir=tmp_path / "reports", pages_dir=FIX,
                      manifest_path=FIX / "manifest.yaml", run_id=RUN)
    assert result.status == "completed"
    with Storage(db) as storage:
        intake = verify_landing_intake(storage, RUN, BODY, INTAKE)
    assert intake["site_id"] == "mock-landing"
    assert len(intake["creatives"]) == 6
    assert len(intake["possible_worlds"]) == 7
    assert sum(w["prior_weight"] for w in intake["possible_worlds"]) == 57
    assert single_right_preview(intake) == {"first_audit": "A", "mention_value": 15,
                                            "adaptive_policy_computed": False}


def test_changed_body_or_failed_receipt_cannot_supply_creatives(tmp_path):
    db = tmp_path / "landing.sqlite3"
    run_demo(FIX / "site.yaml", project_root=ROOT, db_path=db,
             reports_dir=tmp_path / "reports", pages_dir=FIX,
             manifest_path=FIX / "manifest.yaml", run_id=RUN)
    changed = tmp_path / "changed.html"
    changed.write_bytes(BODY.read_bytes() + b" ")
    with Storage(db) as storage:
        with pytest.raises(ValueError, match="matching successful HTML"):
            verify_landing_intake(storage, RUN, changed, INTAKE)


def test_unknown_run_has_no_matching_receipt(captured_db):
    with Storage(captured_db) as storage:
        with pytest.raises(ValueError, match="matching successful HTML"):
            verify_landing_intake(storage, "no-such-run", BODY, INTAKE)


def test_failed_get_leaves_no_matching_receipt(tmp_path):
    pages = tmp_path / "pages"
    pages.mkdir()
    manifest = pages / "manifest.yaml"
    manifest.write_text((FIX / "manifest.yaml").read_text(encoding="utf-8"),
                        encoding="utf-8", newline="\n")
    db = tmp_path / "landing.sqlite3"
    run_demo(FIX / "site.yaml", project_root=ROOT, db_path=db,
             reports_dir=tmp_path / "reports", pages_dir=pages,
             manifest_path=manifest, run_id=RUN)
    with Storage(db) as storage:
        events = storage.get_request_events(RUN)
        assert events and all(e["outcome"] != "success" for e in events)
        with pytest.raises(ValueError, match="matching successful HTML"):
            verify_landing_intake(storage, RUN, BODY, INTAKE)


def test_preview_first_audit_loses_to_adaptive_policy(captured_intake):
    preview = single_right_preview(captured_intake)
    assert preview["first_audit"] == "A"
    assert preview["adaptive_policy_computed"] is False
    policies = compute_adaptive_policies(captured_intake)
    worst = policies["contracts"]["worst_safe_reach"]
    assert worst["first_audit"] == "E"
    assert worst["first_audit"] != preview["first_audit"]
    # The preview's pick caps the worst-case guarantee at 6, below the contract.
    assert worst["first_audit_bounds"]["A"] == 6
    assert worst["first_audit_bounds"]["A"] < worst["minimum_value"]


def test_worst_safe_reach_tree_matches_contract(captured_intake):
    policies = compute_adaptive_policies(captured_intake)
    worst = policies["contracts"]["worst_safe_reach"]
    assert worst["objective"] == "worst"
    assert worst["first_audit"] == "E"
    assert worst["second_audits"] == {"valid": "B", "invalid": "A"}
    assert worst["value"] == 7
    assert worst["minimum_value"] == 7
    assert worst["meets_minimum"] is True
    assert worst["decision"] == "pass"
    assert "meets contract minimum 7" in worst["reason"]
    assert worst["first_audit_bounds"] == {"A": 6, "B": 6, "C": 6, "D": 6, "E": 7}
    leaves = _leaf_map(worst)
    assert set(leaves) == {
        (("E", "valid"), ("B", "valid")),
        (("E", "valid"), ("B", "invalid")),
        (("E", "invalid"), ("A", "valid")),
        (("E", "invalid"), ("A", "invalid")),
    }
    assert leaves[(("E", "valid"), ("B", "valid"))]["worlds"] == ["w4", "w5", "w6"]
    assert leaves[(("E", "valid"), ("B", "valid"))]["safe_creatives"] == ["K2", "K5"]
    assert leaves[(("E", "valid"), ("B", "valid"))]["leaf_value"] == 9
    assert leaves[(("E", "valid"), ("B", "invalid"))]["worlds"] == ["w2"]
    assert leaves[(("E", "valid"), ("B", "invalid"))]["safe_creatives"] == ["K4", "K5"]
    assert leaves[(("E", "valid"), ("B", "invalid"))]["leaf_value"] == 7
    assert leaves[(("E", "invalid"), ("A", "valid"))]["worlds"] == ["w1", "w3"]
    assert leaves[(("E", "invalid"), ("A", "valid"))]["safe_creatives"] == ["K1"]
    assert leaves[(("E", "invalid"), ("A", "valid"))]["leaf_value"] == 7
    assert leaves[(("E", "invalid"), ("A", "invalid"))]["worlds"] == ["w7"]
    assert leaves[(("E", "invalid"), ("A", "invalid"))]["safe_creatives"] == ["K2", "K3"]
    assert leaves[(("E", "invalid"), ("A", "invalid"))]["leaf_value"] == 11
    assert min(leaf["leaf_value"] for leaf in worst["leaves"]) == 7
    _check_leaves_partition_worlds(policies)


def test_expected_safe_reach_tree_matches_contract(captured_intake):
    policies = compute_adaptive_policies(captured_intake)
    expected = policies["contracts"]["expected_safe_reach"]
    assert expected["objective"] == "expected"
    assert expected["first_audit"] == "D"
    assert expected["second_audits"] == {"valid": "B", "invalid": "C"}
    assert expected["value"] == {"numerator": 818, "denominator": 57}
    assert 818 / 57 >= expected["minimum_value"] == 14
    assert expected["meets_minimum"] is True
    assert expected["decision"] == "pass"
    assert "818/57" in expected["reason"]
    bounds = expected["first_audit_bounds"]
    assert bounds == {
        "A": {"numerator": 271, "denominator": 19},  # 813/57
        "B": {"numerator": 809, "denominator": 57},
        "C": {"numerator": 659, "denominator": 57},
        "D": {"numerator": 818, "denominator": 57},
        "E": {"numerator": 713, "denominator": 57},
    }
    assert max(
        Fraction(b["numerator"], b["denominator"]) for r, b in bounds.items() if r != "D"
    ) <= Fraction(813, 57)
    leaves = _leaf_map(expected)
    assert leaves[(("D", "valid"), ("B", "valid"))]["worlds"] == ["w6"]
    assert leaves[(("D", "valid"), ("B", "valid"))]["safe_creatives"] == ALL_CREATIVES
    assert leaves[(("D", "valid"), ("B", "valid"))]["leaf_value"] == 33
    assert leaves[(("D", "valid"), ("B", "invalid"))]["worlds"] == ["w1", "w2"]
    assert leaves[(("D", "valid"), ("B", "invalid"))]["safe_creatives"] == ["K4"]
    assert leaves[(("D", "valid"), ("B", "invalid"))]["leaf_value"] == 4
    assert leaves[(("D", "invalid"), ("C", "valid"))]["worlds"] == ["w4", "w7"]
    assert leaves[(("D", "invalid"), ("C", "valid"))]["safe_creatives"] == ["K2", "K3"]
    assert leaves[(("D", "invalid"), ("C", "valid"))]["leaf_value"] == 11
    assert leaves[(("D", "invalid"), ("C", "invalid"))]["worlds"] == ["w3", "w5"]
    assert leaves[(("D", "invalid"), ("C", "invalid"))]["safe_creatives"] == ["K2"]
    assert leaves[(("D", "invalid"), ("C", "invalid"))]["leaf_value"] == 6
    weights = {w["world_id"]: w["prior_weight"] for w in captured_intake["possible_worlds"]}
    numerator = sum(weights[w] * leaf["leaf_value"]
                    for leaf in expected["leaves"] for w in leaf["worlds"])
    assert numerator == 818


def test_w6_leaf_releases_differ_between_contracts(captured_intake):
    policies = compute_adaptive_policies(captured_intake)
    worst = policies["contracts"]["worst_safe_reach"]
    expected = policies["contracts"]["expected_safe_reach"]
    worst_w6 = next(leaf for leaf in worst["leaves"] if "w6" in leaf["worlds"])
    expected_w6 = next(leaf for leaf in expected["leaves"] if "w6" in leaf["worlds"])
    assert worst_w6["safe_creatives"] == ["K2", "K5"]
    assert worst_w6["leaf_value"] == 9
    assert expected_w6["worlds"] == ["w6"]
    assert expected_w6["safe_creatives"] == ALL_CREATIVES
    assert expected_w6["leaf_value"] == 33


def test_assay_results_filter_incompatible_worlds(captured_intake):
    intake = captured_intake
    invalid = {w["world_id"]: set(w["invalid_rights"])
               for w in intake["possible_worlds"]}
    policies = compute_adaptive_policies(intake)
    for contract in policies["contracts"].values():
        for leaf in contract["leaves"]:
            for step in leaf["path"]:
                for world_id in leaf["worlds"]:
                    is_invalid = step["assay"] in invalid[world_id]
                    assert is_invalid == (step["result"] == "invalid"), (
                        f"world {world_id} is incompatible with observation "
                        f"{step['assay']}={step['result']}"
                    )


def test_second_audit_adapts_to_first_result(captured_intake):
    policies = compute_adaptive_policies(captured_intake)
    worst = policies["contracts"]["worst_safe_reach"]
    assert worst["second_audits"]["valid"] != worst["second_audits"]["invalid"]
    expected = policies["contracts"]["expected_safe_reach"]
    assert expected["second_audits"]["valid"] != expected["second_audits"]["invalid"]
    # Neither contract retests the first-audit right on either branch.
    for contract in (worst, expected):
        assert contract["first_audit"] not in contract["second_audits"].values()


def test_modified_page_changes_tree_and_releasable_creatives(tmp_path):
    html = BODY.read_text(encoding="utf-8")
    html = html.replace('data-rights="E" data-service-value="3"',
                        'data-rights="E" data-service-value="9"')
    html = html.replace('data-rights="A,B" data-service-value="8"',
                        'data-rights="E" data-service-value="2"')
    db, body = _capture_with_page(tmp_path, html)
    with Storage(db) as storage:
        intake = verify_landing_intake(storage, RUN, body, INTAKE)
    creatives = {c["creative_id"]: c for c in intake["creatives"]}
    assert creatives["K5"]["service_value"] == 9
    assert creatives["K6"]["requires"] == ["E"]
    policies = compute_adaptive_policies(intake)
    worst = policies["contracts"]["worst_safe_reach"]
    # The richer page reshapes the worst-case tree away from the E-first plan.
    assert worst["first_audit"] == "A"
    assert worst["value"] == 11
    worst_w6 = next(leaf for leaf in worst["leaves"] if "w6" in leaf["worlds"])
    assert worst_w6["worlds"] == ["w3", "w6"]
    assert worst_w6["safe_creatives"] == ["K1", "K2"]
    assert worst_w6["leaf_value"] == 13
    expected = policies["contracts"]["expected_safe_reach"]
    assert expected["first_audit"] == "E"
    assert expected["value"] == {"numerator": 937, "denominator": 57}
    expected_w6 = next(leaf for leaf in expected["leaves"] if "w6" in leaf["worlds"])
    assert expected_w6["worlds"] == ["w6"]
    assert "K6" in expected_w6["safe_creatives"]
    numerator = sum(
        next(w["prior_weight"] for w in intake["possible_worlds"] if w["world_id"] == wid)
        * leaf["leaf_value"]
        for leaf in expected["leaves"] for wid in leaf["worlds"]
    )
    assert numerator == expected["value"]["numerator"]


def test_weight_changes_recompute_expected_but_not_worst(captured_intake):
    mutated = json.loads(json.dumps(captured_intake))
    for world in mutated["possible_worlds"]:
        if world["world_id"] == "w6":
            world["prior_weight"] = 1
    policies = compute_adaptive_policies(mutated)
    assert policies["total_weight"] == 43
    worst = policies["contracts"]["worst_safe_reach"]
    assert worst["first_audit"] == "E"
    assert worst["value"] == 7
    expected = policies["contracts"]["expected_safe_reach"]
    # Weights feed only the expected contract: the tree is re-optimized.
    assert expected["first_audit"] == "C"
    assert expected["value"] == {"numerator": 435, "denominator": 43}
    numerator = sum(
        (1 if wid == "w6" else next(w["prior_weight"]
                                    for w in captured_intake["possible_worlds"]
                                    if w["world_id"] == wid))
        * leaf["leaf_value"]
        for leaf in expected["leaves"] for wid in leaf["worlds"]
    )
    assert numerator == expected["value"]["numerator"]


def test_world_changes_recompute_trees(captured_intake):
    mutated = json.loads(json.dumps(captured_intake))
    mutated["possible_worlds"] = [w for w in mutated["possible_worlds"]
                                  if w["world_id"] != "w6"]
    policies = compute_adaptive_policies(mutated)
    assert policies["total_weight"] == 42
    for contract in policies["contracts"].values():
        seen = [w for leaf in contract["leaves"] for w in leaf["worlds"]]
        assert sorted(seen) == ["w1", "w2", "w3", "w4", "w5", "w7"]
    expected = policies["contracts"]["expected_safe_reach"]
    assert expected["first_audit"] == "B"
    assert expected["value"] == {"numerator": 419, "denominator": 42}
    assert expected["decision"] == "reject"  # 419/42 < 14


@pytest.mark.parametrize("mutate,match", [
    (lambda d: d["rights"].append("A"), "duplicate right"),
    (lambda d: d["rights"].append("F"), "at most 5 rights"),
    (lambda d: d["possible_worlds"][0]["invalid_rights"].append("Z"), "unknown right"),
    (lambda d: d["possible_worlds"][0]["invalid_rights"].append("B"), "more than once"),
    (lambda d: d["possible_worlds"][0].update(prior_weight=0), "positive integer"),
    (lambda d: d["possible_worlds"][0].update(prior_weight=-2), "positive integer"),
    (lambda d: d["possible_worlds"][0].update(prior_weight="5"), "positive integer"),
    (lambda d: d["possible_worlds"][0].update(prior_weight=True), "positive integer"),
    (lambda d: d["possible_worlds"][1].update(world_id="w1"), "duplicate world"),
    (lambda d: d["possible_worlds"].extend([
        {"world_id": "w8", "invalid_rights": [], "prior_weight": 1},
        {"world_id": "w9", "invalid_rights": [], "prior_weight": 1},
    ]), "at most 8 possible worlds"),
    (lambda d: d["contracts"].pop("expected_safe_reach"), "expected_safe_reach"),
    (lambda d: d["contracts"]["expected_safe_reach"].update(minimum_value="x"),
     "minimum_value"),
    (lambda d: d["contracts"]["worst_safe_reach"].update(minimum_value=-1),
     "minimum_value"),
])
def test_intake_validation_rejects_bad_payloads(captured_db, tmp_path, mutate, match):
    path = _mutated_intake_path(tmp_path, mutate)
    with Storage(captured_db) as storage:
        with pytest.raises(ValueError, match=match):
            verify_landing_intake(storage, RUN, BODY, path)


@pytest.mark.parametrize("old,new,match", [
    ('data-service-value="3"', 'data-service-value="-3"', "service_value"),
    ('data-service-value="4"', 'data-service-value="four"', "service_value"),
    ('data-creative-id="K2"', 'data-creative-id="K1"', "creative IDs invalid"),
    ('data-rights="C"', 'data-rights="Z"', "unknown right"),
])
def test_page_creative_validation(tmp_path, old, new, match):
    html = BODY.read_text(encoding="utf-8").replace(old, new)
    db, body = _capture_with_page(tmp_path, html)
    with Storage(db) as storage:
        with pytest.raises(ValueError, match=match):
            verify_landing_intake(storage, RUN, body, INTAKE)


def _tie_intake():
    return {
        "rights": ["P", "Q"],
        "creatives": [
            {"creative_id": "c1", "requires": ["P"], "service_value": 5},
            {"creative_id": "c2", "requires": ["Q"], "service_value": 5},
        ],
        "possible_worlds": [
            {"world_id": "w1", "invalid_rights": ["P"], "prior_weight": 1},
            {"world_id": "w2", "invalid_rights": ["Q"], "prior_weight": 1},
            {"world_id": "w3", "invalid_rights": [], "prior_weight": 1},
        ],
        "contracts": {
            "worst_safe_reach": {"minimum_value": 0},
            "expected_safe_reach": {"minimum_value": 0},
        },
    }


def test_tied_first_audits_break_by_right_id():
    policies = compute_adaptive_policies(_tie_intake())
    worst = policies["contracts"]["worst_safe_reach"]
    assert worst["first_audit_bounds"] == {"P": 5, "Q": 5}
    assert worst["first_audit"] == "P"
    expected = policies["contracts"]["expected_safe_reach"]
    assert expected["first_audit_bounds"] == {
        "P": {"numerator": 20, "denominator": 3},
        "Q": {"numerator": 20, "denominator": 3},
    }
    assert expected["first_audit"] == "P"


def test_input_reordering_yields_identical_policies(captured_intake):
    baseline = compute_adaptive_policies(captured_intake)
    reordered = json.loads(json.dumps(captured_intake))
    reordered["rights"] = list(reversed(reordered["rights"]))
    reordered["possible_worlds"] = list(reversed(reordered["possible_worlds"]))
    for world in reordered["possible_worlds"]:
        world["invalid_rights"] = list(reversed(world["invalid_rights"]))
    reordered["creatives"] = list(reversed(reordered["creatives"]))
    for creative in reordered["creatives"]:
        creative["requires"] = list(reversed(creative["requires"]))
    shuffled = compute_adaptive_policies(reordered)
    assert json.dumps(shuffled, sort_keys=True) == json.dumps(baseline, sort_keys=True)


def test_no_safe_creative_yields_zero_and_rejection():
    intake = {
        "rights": ["A", "B"],
        "creatives": [
            {"creative_id": "c1", "requires": ["A", "B"], "service_value": 9},
        ],
        "possible_worlds": [
            {"world_id": "w1", "invalid_rights": ["A"], "prior_weight": 2},
            {"world_id": "w2", "invalid_rights": ["B"], "prior_weight": 3},
        ],
        "contracts": {
            "worst_safe_reach": {"minimum_value": 1},
            "expected_safe_reach": {"minimum_value": 1},
        },
    }
    policies = compute_adaptive_policies(intake)
    for name, contract in policies["contracts"].items():
        assert all(leaf["safe_creatives"] == [] for leaf in contract["leaves"])
        assert all(leaf["leaf_value"] == 0 for leaf in contract["leaves"])
        assert contract["meets_minimum"] is False
        assert contract["decision"] == "reject"
        assert "below contract minimum 1" in contract["reason"]
    assert policies["contracts"]["worst_safe_reach"]["value"] == 0
    assert policies["contracts"]["expected_safe_reach"]["value"] == {
        "numerator": 0, "denominator": 1,
    }


def test_cli_landing_rights(tmp_path, capsys):
    code = cli_main([
        "landing-rights",
        "--project-root", str(ROOT),
        "--db", str(tmp_path / "landing.sqlite3"),
        "--reports-dir", str(tmp_path / "reports"),
    ])
    assert code == 0
    out = json.loads(capsys.readouterr().out)
    assert out["assay_budget"] == 2
    assert out["total_weight"] == 57
    worst = out["contracts"]["worst_safe_reach"]
    assert worst["first_audit"] == "E"
    assert worst["second_audits"] == {"valid": "B", "invalid": "A"}
    assert worst["value"] == 7
    assert worst["decision"] == "pass"
    expected = out["contracts"]["expected_safe_reach"]
    assert expected["first_audit"] == "D"
    assert expected["second_audits"] == {"valid": "B", "invalid": "C"}
    assert expected["value"] == {"numerator": 818, "denominator": 57}
    assert expected["decision"] == "pass"
