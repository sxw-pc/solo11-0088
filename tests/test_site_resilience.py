"""Tests for two-phase, non-anticipative mirror availability contracts.

The first two tests pin the legacy per-scenario preview trap: with hindsight it
recommends A for west and C for east, which cannot be one pre-outage purchase.
Everything afterwards exercises the fingerprinted contract service.
"""
from __future__ import annotations

import copy
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

from crawlbudget.config import load_config
from crawlbudget.errors import (
    RES_DISABLED_SITE,
    RES_DUPLICATE_ID,
    RES_EMPTY_SCENARIO,
    RES_NEGATIVE_COST,
    RES_UNKNOWN_NODE,
    RES_UNKNOWN_SITE,
    ConfigError,
    ResilienceError,
)
from crawlbudget.site_resilience import (
    MAX_HARDEN,
    MAX_REPAIR_PER_SCENARIO,
    load_mirror_model,
    plan_contract,
    plan_resilience,
    preview_scenario_hardening,
)

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "fixtures"
INTAKE = FIXTURES / "site_resilience" / "intake.json"
DEMO_CONFIG = FIXTURES / "demo_sites.yaml"


def _load_intake() -> dict:
    return json.loads(INTAKE.read_text(encoding="utf-8"))


def _write_config(tmp_path: Path, mutator=None) -> Path:
    """Copy the loopback demo config into tmp_path with absolute fixture refs."""
    raw = yaml.safe_load(DEMO_CONFIG.read_text(encoding="utf-8"))
    for site in raw["sites"]:
        site["robots_file"] = str((FIXTURES / site["robots_file"]).resolve())
    for job in raw["jobs"]:
        job["seed_file"] = str((FIXTURES / job["seed_file"]).resolve())
    if mutator is not None:
        mutator(raw)
    target = tmp_path / "demo_sites.yaml"
    target.write_text(yaml.safe_dump(raw, sort_keys=False), encoding="utf-8")
    return target


def _write_intake(tmp_path: Path, data: dict) -> Path:
    target = tmp_path / "intake.json"
    target.write_text(json.dumps(data), encoding="utf-8")
    return target


def _plan(data: dict, tmp_path: Path, config_mutator=None) -> dict:
    config = _write_config(tmp_path, config_mutator)
    fixture = _write_intake(tmp_path, data)
    return plan_resilience(fixture, config, project_root=ROOT)


def _signed(result: dict, contract_id: str) -> dict:
    contract = next(c for c in result["contracts"] if c["contract_id"] == contract_id)
    assert contract["status"] == "signed", contract
    return contract


def _scenario(contract: dict, scenario_id: str) -> dict:
    return next(s for s in contract["scenarios"] if s["scenario_id"] == scenario_id)


# ---------------------------------------------------------------------------
# Legacy preview trap and fixture/config identity
# ---------------------------------------------------------------------------


def test_replica_sites_match_loopback_demo_sites():
    data = _load_intake()
    config = yaml.safe_load(DEMO_CONFIG.read_text(encoding="utf-8"))
    assert set(data["sites"]) == {s["site_id"] for s in config["sites"]}
    assert all(s["base_url"].startswith("http://127.0.0.1:") for s in config["sites"])


def test_preview_uses_two_incompatible_pre_outage_choices():
    data = _load_intake()
    # The hindsight preview is allowed to contradict itself across scenarios.
    assert preview_scenario_hardening(data) == {
        "west-outage": "A",
        "east-outage": "C",
    }
    assert data["harden_cost"]["B"] > data["harden_cost"]["A"]


def test_contract_service_does_not_buy_the_two_preview_choices(tmp_path):
    result = _plan(_load_intake(), tmp_path)
    for contract in result["contracts"]:
        assert contract["status"] == "signed"
        # Exactly one shared, pre-outage hardening decision per contract.
        assert len(contract["hardened_nodes"]) == 1
        # Neither contract is allowed to harden both A and C, the preview trap.
        assert set(contract["hardened_nodes"]) != {"A", "C"}
        for scenario in contract["scenarios"]:
            assert len(scenario["repair"]["repaired_nodes"]) <= MAX_REPAIR_PER_SCENARIO


def test_output_carries_bound_config_fingerprint(tmp_path):
    config = _write_config(tmp_path)
    fixture = _write_intake(tmp_path, _load_intake())
    bundle = load_config(config, project_root=ROOT)
    result = plan_resilience(fixture, config, project_root=ROOT)
    assert result["site_config"]["config_fingerprint"] == bundle.config_fingerprint
    assert result["site_config"]["contracted_sites"] == [
        "mock-alpha", "mock-beta", "mock-gamma",
    ]
    # Fingerprints bind the whole signed certificate together.
    assert len(result["mirror_model_fingerprint"]) == 64
    assert len(result["contract_fingerprint"]) == 64
    assert result["schema_version"] == "mirror-availability/2.0"


# ---------------------------------------------------------------------------
# continuous_sites: must harden B at cost 6
# ---------------------------------------------------------------------------


def test_continuous_contract_requires_hardening_b_at_six(tmp_path):
    result = _plan(_load_intake(), tmp_path)
    contract = _signed(result, "continuous_sites")
    assert contract["hardened_nodes"] == ["B"]
    assert contract["harden_cost"] == 6
    assert contract["worst_scenario_cost"] == 0
    assert contract["total_cost"] == 6
    for scenario in contract["scenarios"]:
        assert scenario["initial_phase"]["down_sites"] == []
        assert scenario["post_repair_phase"]["down_sites"] == []
        assert scenario["repair"]["repaired_nodes"] == []
        assert scenario["compensation"] == 0
        assert all(scenario["initial_phase"]["site_availability"].values())


def test_continuous_contract_rejects_cheaper_single_node_choices(tmp_path):
    # The optimality ledger must show B as the *only* feasible decision:
    # A alone loses beta in the east outage, C alone loses alpha in the west.
    result = _plan(_load_intake(), tmp_path)
    contract = _signed(result, "continuous_sites")
    ledger = contract["optimality_evidence"]["ledger"]
    assert [row["hardened_nodes"] for row in ledger] == [["B"]]
    assert contract["optimality_evidence"]["feasible_decisions"] == 1
    assert contract["optimality_evidence"]["harden_decisions_evaluated"] >= 1


def test_hardening_a_or_c_fails_continuous_sla(tmp_path):
    # Drive the evaluator directly to prove why the preview choices fail.
    fixture = _write_intake(tmp_path, _load_intake())
    config = _write_config(tmp_path)
    model = load_mirror_model(fixture, config, project_root=ROOT)
    obligation = next(c for c in model.contracts if c.contract_id == "continuous_sites")
    scenario_east = next(s for s in model.scenarios if s.scenario_id == "east-outage")
    scenario_west = next(s for s in model.scenarios if s.scenario_id == "west-outage")
    # A alone: east outage (B,C down) leaves beta with no live replica.
    assert plan_contract(model, obligation)["hardened_nodes"] == ["B"]
    from crawlbudget.site_resilience import _evaluate_scenario
    assert _evaluate_scenario(model, obligation, scenario_east, frozenset({"A"})) is None
    # C alone: west outage (A,B down) leaves alpha with no live replica.
    assert _evaluate_scenario(model, obligation, scenario_west, frozenset({"C"})) is None


# ---------------------------------------------------------------------------
# beta_repair_window: harden A at cost 2, total worst-case 5
# ---------------------------------------------------------------------------


def test_beta_window_contract_hardens_a_and_pays_five(tmp_path):
    result = _plan(_load_intake(), tmp_path)
    contract = _signed(result, "beta_repair_window")
    assert contract["hardened_nodes"] == ["A"]
    assert contract["harden_cost"] == 2
    assert contract["total_cost"] == 5
    west = _scenario(contract, "west-outage")
    east = _scenario(contract, "east-outage")
    # West outage: A survives hardening, all three sites stay up, no spend.
    assert west["initial_phase"]["down_sites"] == []
    assert west["repair"]["repaired_nodes"] == []
    assert west["compensation"] == 0
    assert west["scenario_total"] == 0
    # East outage: only beta may break initially, compensation 2, one repair.
    assert east["initial_phase"]["down_sites"] == ["mock-beta"]
    assert east["compensation"] == 2
    assert east["repair"]["repair_node"] in {"B", "C"}
    assert east["repair"]["repair_cost"] == 1
    assert east["scenario_total"] == 3
    assert contract["worst_scenario_cost"] == 3


def test_beta_initial_outage_is_not_erased_by_repair(tmp_path):
    result = _plan(_load_intake(), tmp_path)
    contract = _signed(result, "beta_repair_window")
    east = _scenario(contract, "east-outage")
    # The later repair restores service but the initial outage stays on record.
    assert east["post_repair_phase"]["down_sites"] == []
    assert east["post_repair_phase"]["initial_outage_preserved"] == ["mock-beta"]
    assert east["initial_phase"]["site_availability"]["mock-beta"] is False
    assert east["post_repair_phase"]["site_availability"]["mock-beta"] is True


def test_alpha_and_gamma_never_interrupt_in_beta_window(tmp_path):
    result = _plan(_load_intake(), tmp_path)
    contract = _signed(result, "beta_repair_window")
    for scenario in contract["scenarios"]:
        for phase in ("initial_phase", "post_repair_phase"):
            availability = scenario[phase]["site_availability"]
            assert availability["mock-alpha"] is True
            assert availability["mock-gamma"] is True
        assert "mock-alpha" not in scenario["initial_phase"]["down_sites"]
        assert "mock-gamma" not in scenario["initial_phase"]["down_sites"]


def test_beta_window_ledger_proves_five_beats_six(tmp_path):
    result = _plan(_load_intake(), tmp_path)
    contract = _signed(result, "beta_repair_window")
    ledger = {
        tuple(row["hardened_nodes"]): row["total_cost"]
        for row in contract["optimality_evidence"]["ledger"]
    }
    assert ledger[("A",)] == 5
    assert ledger[("B",)] == 6
    # Stable ordering: cheapest first, deterministic node ids.
    rows = contract["optimality_evidence"]["ledger"]
    assert [row["hardened_nodes"] for row in rows] == [["A"], ["B"]]


# ---------------------------------------------------------------------------
# Repair authority: post-revelation only, failed nodes only, at most one
# ---------------------------------------------------------------------------


def test_repair_only_targets_failed_nodes_and_never_hardened(tmp_path):
    result = _plan(_load_intake(), tmp_path)
    for contract in result["contracts"]:
        if contract["status"] != "signed":
            continue
        hardened = set(contract["hardened_nodes"])
        for scenario in contract["scenarios"]:
            repaired = set(scenario["repair"]["repaired_nodes"])
            failed = set(scenario["failed_nodes"])
            assert repaired <= failed
            assert not (repaired & hardened)
            assert len(repaired) <= MAX_REPAIR_PER_SCENARIO
            # Repair can only add nodes: it never removes initial survivors.
            initial = set(scenario["initial_phase"]["alive_nodes"])
            later = set(scenario["post_repair_phase"]["alive_nodes"])
            assert initial <= later


def test_unrepairable_broken_site_rejects_contract(tmp_path):
    # One scenario fails every replica of a site and hardening budget is one;
    # a no-outage contract cannot be signed instead of being faked as zero.
    data = _load_intake()
    data["scenarios"].append({
        "scenario_id": "total-outage",
        "failed_nodes": ["A", "B", "C"],
    })
    result = _plan(data, tmp_path)
    contract = next(c for c in result["contracts"]
                    if c["contract_id"] == "continuous_sites")
    assert contract["status"] == "rejected"
    assert contract["rejection_reasons"]
    assert any("total-outage" in reason for reason in contract["rejection_reasons"])


# ---------------------------------------------------------------------------
# Local config causality: unknown / disabled / external sites
# ---------------------------------------------------------------------------


def test_mirror_unknown_site_is_refused(tmp_path):
    data = _load_intake()
    data["sites"]["mock-delta"] = ["A"]
    with pytest.raises(ResilienceError) as exc:
        _plan(data, tmp_path)
    codes = {issue.code for issue in exc.value.issues}
    assert RES_UNKNOWN_SITE in codes


def test_mirror_disabled_site_is_refused(tmp_path):
    data = _load_intake()

    def disable_beta(raw):
        for site in raw["sites"]:
            if site["site_id"] == "mock-beta":
                site["enabled"] = False

    config = _write_config(tmp_path, disable_beta)
    fixture = _write_intake(tmp_path, data)
    with pytest.raises(ResilienceError) as exc:
        load_mirror_model(fixture, config, project_root=ROOT)
    codes = {issue.code for issue in exc.value.issues}
    # The disabled site is the causal defect; its contract reference then
    # cascades into an unknown-site issue, which is also correct.
    assert RES_DISABLED_SITE in codes
    disabled_issue = next(i for i in exc.value.issues if i.code == RES_DISABLED_SITE)
    assert "mock-beta" in disabled_issue.path


def test_non_loopback_config_is_refused_upstream(tmp_path):
    def go_external(raw):
        raw["sites"][0]["base_url"] = "http://example.com:80/mock-alpha/"

    config = _write_config(tmp_path, go_external)
    with pytest.raises(ConfigError):
        load_config(config, project_root=ROOT)


def test_config_change_causally_changes_contract_certificate(tmp_path):
    fixture = _write_intake(tmp_path, _load_intake())
    first_config = _write_config(tmp_path)
    first = plan_resilience(fixture, first_config, project_root=ROOT)

    def retune(raw):
        raw["sites"][0]["max_rps"] = 99

    second_config = _write_config(tmp_path, retune)
    second = plan_resilience(fixture, second_config, project_root=ROOT)
    # The mirror model itself is untouched...
    assert second["mirror_model_fingerprint"] == first["mirror_model_fingerprint"]
    # ...but the bound config identity and signed certificate must change.
    assert second["site_config"]["config_fingerprint"] != \
        first["site_config"]["config_fingerprint"]
    assert second["contract_fingerprint"] != first["contract_fingerprint"]
    # The plan content remains the same B/6, A/5 pair.
    assert {
        (c["contract_id"], tuple(c["hardened_nodes"]), c["total_cost"])
        for c in second["contracts"]
    } == {
        (c["contract_id"], tuple(c["hardened_nodes"]), c["total_cost"])
        for c in first["contracts"]
    }


# ---------------------------------------------------------------------------
# Structural rejection: negative costs, duplicate ids, empty scenarios
# ---------------------------------------------------------------------------


def _expect_rejection(data: dict, tmp_path, *codes):
    with pytest.raises(ResilienceError) as exc:
        _plan(data, tmp_path)
    observed = {issue.code for issue in exc.value.issues}
    for code in codes:
        assert code in observed, (code, observed)


def test_negative_harden_cost_is_refused(tmp_path):
    data = _load_intake()
    data["harden_cost"]["A"] = -1
    _expect_rejection(data, tmp_path, RES_NEGATIVE_COST)


def test_negative_repair_cost_is_refused(tmp_path):
    data = _load_intake()
    data["repair_cost"]["C"] = -2
    _expect_rejection(data, tmp_path, RES_NEGATIVE_COST)


def test_negative_compensation_is_refused(tmp_path):
    data = _load_intake()
    data["contracts"]["continuous_sites"]["outage_compensation"] = -1
    _expect_rejection(data, tmp_path, RES_NEGATIVE_COST)


def test_duplicate_scenario_id_is_refused(tmp_path):
    data = _load_intake()
    data["scenarios"][1]["scenario_id"] = "west-outage"
    _expect_rejection(data, tmp_path, RES_DUPLICATE_ID)


def test_duplicate_failed_node_is_refused(tmp_path):
    data = _load_intake()
    data["scenarios"][0]["failed_nodes"] = ["A", "A"]
    _expect_rejection(data, tmp_path, RES_DUPLICATE_ID)


def test_duplicate_mirror_node_is_refused(tmp_path):
    data = _load_intake()
    data["sites"]["mock-alpha"] = ["A", "A"]
    _expect_rejection(data, tmp_path, RES_DUPLICATE_ID)


def test_duplicate_json_keys_are_refused(tmp_path):
    config = _write_config(tmp_path)
    fixture = tmp_path / "dup.json"
    fixture.write_text(
        '{"sites": {}, "harden_cost": {"A": 1}, "harden_cost": {"B": 2}, '
        '"repair_cost": {"A": 1, "B": 1}, "scenarios": [], "contracts": {}}',
        encoding="utf-8",
    )
    with pytest.raises(ResilienceError):
        load_mirror_model(fixture, config, project_root=ROOT)


def test_unknown_node_in_scenario_is_refused(tmp_path):
    data = _load_intake()
    data["scenarios"][0]["failed_nodes"] = ["Z"]
    _expect_rejection(data, tmp_path, RES_UNKNOWN_NODE)


def test_unknown_node_in_mirror_mapping_is_refused(tmp_path):
    data = _load_intake()
    data["sites"]["mock-alpha"] = ["A", "Z"]
    _expect_rejection(data, tmp_path, RES_UNKNOWN_NODE)


def test_empty_scenario_list_is_refused(tmp_path):
    data = _load_intake()
    data["scenarios"] = []
    _expect_rejection(data, tmp_path, RES_EMPTY_SCENARIO)


def test_scenario_without_failed_nodes_is_refused(tmp_path):
    data = _load_intake()
    data["scenarios"][0]["failed_nodes"] = []
    _expect_rejection(data, tmp_path, RES_EMPTY_SCENARIO)


def test_hardening_budget_above_engine_ceiling_is_refused(tmp_path):
    data = _load_intake()
    data["limits"] = {"max_harden_nodes": MAX_HARDEN + 1}
    _expect_rejection(data, tmp_path)


# ---------------------------------------------------------------------------
# Determinism: scenario reordering must not change the signed plan
# ---------------------------------------------------------------------------


def test_reordering_scenarios_keeps_the_same_plan(tmp_path):
    base = _load_intake()
    reordered = copy.deepcopy(base)
    reordered["scenarios"].reverse()
    first = _plan(base, tmp_path)
    second = _plan(reordered, tmp_path)

    def signature(result):
        return {
            contract["contract_id"]: (
                tuple(contract["hardened_nodes"]),
                contract["total_cost"],
                tuple(sorted(
                    (s["scenario_id"], s["repair"]["repair_node"] or "",
                     s["compensation"])
                    for s in contract["scenarios"]
                )),
            )
            for contract in result["contracts"]
        }

    assert signature(second) == signature(first)
    # Output scenario order follows the fixture, but the fingerprinted decision
    # is independent of that order.
    assert [s["scenario_id"] for s in second["scenarios"]] == list(
        reversed([s["scenario_id"] for s in first["scenarios"]])
    )


def test_repair_tie_breaks_stably_on_node_id(tmp_path):
    # East outage leaves both B and C failed; the cheaper-total tie is resolved
    # by node id so the choice is reproducible.
    result = _plan(_load_intake(), tmp_path)
    contract = _signed(result, "beta_repair_window")
    east = _scenario(contract, "east-outage")
    assert east["repair"]["repair_node"] == "B"


# ---------------------------------------------------------------------------
# CLI smoke test
# ---------------------------------------------------------------------------


def test_cli_emits_signed_contracts_and_preview_trap():
    output = subprocess.run(
        [
            sys.executable, "-B", "-m", "crawlbudget.site_resilience",
            "--fixture", str(INTAKE),
            "--config", str(DEMO_CONFIG),
            "--project-root", str(ROOT),
        ],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
        env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
    )
    payload = json.loads(output.stdout)
    by_id = {c["contract_id"]: c for c in payload["contracts"]}
    assert by_id["continuous_sites"]["hardened_nodes"] == ["B"]
    assert by_id["continuous_sites"]["total_cost"] == 6
    assert by_id["beta_repair_window"]["hardened_nodes"] == ["A"]
    assert by_id["beta_repair_window"]["total_cost"] == 5

    preview = subprocess.run(
        [
            sys.executable, "-B", "-m", "crawlbudget.site_resilience",
            "--fixture", str(INTAKE),
            "--preview",
        ],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
        env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
    )
    assert json.loads(preview.stdout) == {
        "west-outage": "A", "east-outage": "C",
    }
