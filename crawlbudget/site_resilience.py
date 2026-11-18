"""Two-phase, non-anticipative mirror availability contracts for loopback mock sites.

The legacy :func:`preview_scenario_hardening` preview picks the cheapest
hardening node *per scenario with hindsight*.  Its two answers (``A`` for the
west outage, ``C`` for the east outage) cannot be bought together before the
outage is revealed and are retained only as an explicit trap.

The real service implemented here signs fingerprinted, two-phase contracts:

1. Before any outage is revealed, at most one (bounded to two) node is
   hardened and its ``harden_cost`` paid.  The hardened node survives every
   listed failure scenario.
2. After a scenario is revealed, at most one failed node may be repaired at
   its ``repair_cost``.  Repair changes only later points in time: the
   initial post-outage availability of each site is reported separately and
   can never be rewritten.

All inputs are synthetic.  Site identity and enablement come from the local
YAML config via :func:`crawlbudget.config.load_config`; mirror mappings that
reference unknown, disabled, or non-loopback sites are refused.
"""
from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass
from itertools import combinations
from pathlib import Path
from typing import Any

from .config import load_config
from .errors import (
    RES_CONTRACT_INVALID,
    RES_DISABLED_SITE,
    RES_DUPLICATE_ID,
    RES_EMPTY_SCENARIO,
    RES_LIMIT_EXCEEDED,
    RES_MODEL_INVALID,
    RES_NEGATIVE_COST,
    RES_UNKNOWN_NODE,
    RES_UNKNOWN_SITE,
    ConfigError,
    ConfigIssue,
    ResilienceError,
)
from .util import fingerprint

SCHEMA_VERSION = "mirror-availability/2.0"

# Bounded search space for the non-anticipative policy search.
MAX_NODES = 8
MAX_SITES = 5
MAX_SCENARIOS = 4
MAX_HARDEN = 2
MAX_REPAIR_PER_SCENARIO = 1

DEFAULT_CONFIG_PATH = Path("fixtures/demo_sites.yaml")
DEFAULT_FIXTURE_PATH = Path("fixtures/site_resilience/intake.json")


# ---------------------------------------------------------------------------
# Typed model
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class MirrorScenario:
    scenario_id: str
    failed_nodes: frozenset[str]


@dataclass(frozen=True)
class ContractObligation:
    contract_id: str
    allowed_initial_outage_sites: frozenset[str]
    outage_compensation: int


@dataclass(frozen=True)
class MirrorModel:
    sites: dict[str, tuple[str, ...]]
    harden_cost: dict[str, int]
    repair_cost: dict[str, int]
    scenarios: tuple[MirrorScenario, ...]
    contracts: tuple[ContractObligation, ...]
    max_harden_nodes: int
    node_ids: tuple[str, ...]
    site_ids: tuple[str, ...]
    mirror_fingerprint: str
    config_fingerprint: str
    config_path: str
    enabled_sites: tuple[str, ...]


# ---------------------------------------------------------------------------
# Validation and construction
# ---------------------------------------------------------------------------


def _issue(code: str, path: str, message: str) -> ConfigIssue:
    return ConfigIssue(code, path, message)


def _load_intake(fixture_path: Path) -> Any:
    # Duplicate JSON keys would let one scenario/node silently shadow another.
    def reject_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        seen: set[str] = set()
        duplicates: list[str] = []
        for key, _value in pairs:
            if key in seen and key not in duplicates:
                duplicates.append(key)
            seen.add(key)
        if duplicates:
            raise ValueError("duplicate JSON keys: " + ", ".join(sorted(duplicates)))
        return dict(pairs)

    try:
        return json.loads(fixture_path.read_text(encoding="utf-8"),
                          object_pairs_hook=reject_duplicates)
    except FileNotFoundError as exc:
        raise ResilienceError([_issue(
            RES_MODEL_INVALID, str(fixture_path), "fixture file not found")]) from exc
    except (json.JSONDecodeError, ValueError) as exc:
        raise ResilienceError([_issue(
            RES_MODEL_INVALID, str(fixture_path), f"invalid JSON: {exc}")]) from exc


def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _validate_mirror_model(data: Any, config_bundle: Any) -> MirrorModel:
    issues: list[ConfigIssue] = []
    if not isinstance(data, dict):
        raise ResilienceError([_issue(
            RES_MODEL_INVALID, "$", "top level must be a mapping")])

    for key in ("sites", "harden_cost", "repair_cost", "scenarios", "contracts"):
        if key not in data:
            issues.append(_issue(RES_MODEL_INVALID, key, "field is required"))
    if issues:
        raise ResilienceError(issues)

    enabled = {site_id for site_id, site in config_bundle.sites.items() if site.enabled}
    known_sites = set(config_bundle.sites)

    raw_sites = data["sites"]
    if not isinstance(raw_sites, dict) or not raw_sites:
        issues.append(_issue(RES_MODEL_INVALID, "sites",
                             "must be a non-empty mapping of site_id to node list"))
        raw_sites = {}
    if len(raw_sites) > MAX_SITES:
        issues.append(_issue(RES_LIMIT_EXCEEDED, "sites",
                             f"at most {MAX_SITES} sites are supported"))

    # Node universe is defined by the harden-cost table; repair costs and
    # scenario failures must reference exactly those nodes.
    raw_harden = data["harden_cost"]
    raw_repair = data["repair_cost"]
    if not isinstance(raw_harden, dict) or not raw_harden:
        issues.append(_issue(RES_MODEL_INVALID, "harden_cost",
                             "must be a non-empty mapping of node_id to cost"))
        raw_harden = {}
    if not isinstance(raw_repair, dict) or not raw_repair:
        issues.append(_issue(RES_MODEL_INVALID, "repair_cost",
                             "must be a non-empty mapping of node_id to cost"))
        raw_repair = {}
    if len(raw_harden) > MAX_NODES:
        issues.append(_issue(RES_LIMIT_EXCEEDED, "harden_cost",
                             f"at most {MAX_NODES} nodes are supported"))

    node_ids: list[str] = []
    harden_cost: dict[str, int] = {}
    for node, cost in raw_harden.items():
        if not isinstance(node, str) or not node.strip():
            issues.append(_issue(RES_MODEL_INVALID, "harden_cost",
                                 "node ids must be non-empty strings"))
            continue
        if node in harden_cost:  # pragma: no cover - dict keys already unique
            issues.append(_issue(RES_DUPLICATE_ID, f"harden_cost.{node}",
                                 "duplicate node id"))
            continue
        if not _is_int(cost) or cost < 0:
            issues.append(_issue(RES_NEGATIVE_COST, f"harden_cost.{node}",
                                 "cost must be a non-negative integer"))
            continue
        node_ids.append(node)
        harden_cost[node] = cost
    node_set = set(node_ids)

    repair_cost: dict[str, int] = {}
    for node, cost in raw_repair.items():
        if node not in node_set:
            issues.append(_issue(RES_UNKNOWN_NODE, f"repair_cost.{node}",
                                 "repair node is absent from harden_cost"))
            continue
        if not _is_int(cost) or cost < 0:
            issues.append(_issue(RES_NEGATIVE_COST, f"repair_cost.{node}",
                                 "cost must be a non-negative integer"))
            continue
        repair_cost[node] = cost
    for node in node_set:
        if node not in repair_cost:
            issues.append(_issue(RES_MODEL_INVALID, f"repair_cost.{node}",
                                 "every hardened-candidate node needs a repair cost"))

    # Mirror mapping: every site must exist, be enabled, and list known,
    # unique replica nodes.  No external or disabled site may enter a contract.
    sites: dict[str, tuple[str, ...]] = {}
    for site_id, replicas in raw_sites.items():
        path = f"sites.{site_id}"
        if not isinstance(site_id, str) or not site_id.strip():
            issues.append(_issue(RES_MODEL_INVALID, "sites",
                                 "site ids must be non-empty strings"))
            continue
        if site_id not in known_sites:
            issues.append(_issue(RES_UNKNOWN_SITE, path,
                                 "site is not registered in the local site config"))
            continue
        if site_id not in enabled:
            issues.append(_issue(RES_DISABLED_SITE, path,
                                 "site is disabled in the local site config"))
            continue
        if not isinstance(replicas, list) or not replicas:
            issues.append(_issue(RES_MODEL_INVALID, path,
                                 "must be a non-empty list of mirror node ids"))
            continue
        local_nodes: list[str] = []
        seen_nodes: set[str] = set()
        for node in replicas:
            if not isinstance(node, str) or not node.strip():
                issues.append(_issue(RES_MODEL_INVALID, path,
                                     "node ids must be non-empty strings"))
                continue
            if node in seen_nodes:
                issues.append(_issue(RES_DUPLICATE_ID, f"{path}.{node}",
                                     "duplicate mirror node for site"))
                continue
            seen_nodes.add(node)
            if node not in node_set:
                issues.append(_issue(RES_UNKNOWN_NODE, f"{path}.{node}",
                                     "mirror node is absent from harden_cost"))
                continue
            local_nodes.append(node)
        if local_nodes:
            sites[site_id] = tuple(local_nodes)

    # Failure scenarios.
    raw_scenarios = data["scenarios"]
    if not isinstance(raw_scenarios, list) or not raw_scenarios:
        issues.append(_issue(RES_EMPTY_SCENARIO, "scenarios",
                             "at least one failure scenario is required"))
        raw_scenarios = []
    if len(raw_scenarios) > MAX_SCENARIOS:
        issues.append(_issue(RES_LIMIT_EXCEEDED, "scenarios",
                             f"at most {MAX_SCENARIOS} scenarios are supported"))
    scenarios: list[MirrorScenario] = []
    seen_scenario_ids: set[str] = set()
    for index, item in enumerate(raw_scenarios):
        path = f"scenarios[{index}]"
        if not isinstance(item, dict):
            issues.append(_issue(RES_MODEL_INVALID, path,
                                 "scenario entry must be a mapping"))
            continue
        scenario_id = item.get("scenario_id")
        if not isinstance(scenario_id, str) or not scenario_id.strip():
            issues.append(_issue(RES_MODEL_INVALID, f"{path}.scenario_id",
                                 "scenario_id must be a non-empty string"))
            continue
        if scenario_id in seen_scenario_ids:
            issues.append(_issue(RES_DUPLICATE_ID, f"{path}.scenario_id",
                                 f"duplicate scenario id {scenario_id}"))
            continue
        failed = item.get("failed_nodes")
        if not isinstance(failed, list) or not failed:
            issues.append(_issue(RES_EMPTY_SCENARIO, f"{path}.failed_nodes",
                                 "each scenario must fail at least one node"))
            continue
        members: set[str] = set()
        bad = False
        for node in failed:
            if not isinstance(node, str) or not node.strip():
                issues.append(_issue(RES_MODEL_INVALID, f"{path}.failed_nodes",
                                     "node ids must be non-empty strings"))
                bad = True
                continue
            if node in members:
                issues.append(_issue(RES_DUPLICATE_ID, f"{path}.failed_nodes.{node}",
                                     "duplicate failed node in scenario"))
                bad = True
                continue
            members.add(node)
            if node not in node_set:
                issues.append(_issue(RES_UNKNOWN_NODE, f"{path}.failed_nodes.{node}",
                                     "failed node is absent from harden_cost"))
                bad = True
        if bad:
            continue
        seen_scenario_ids.add(scenario_id)
        scenarios.append(MirrorScenario(scenario_id, frozenset(members)))

    # Service contracts.
    raw_contracts = data["contracts"]
    if not isinstance(raw_contracts, dict) or not raw_contracts:
        issues.append(_issue(RES_CONTRACT_INVALID, "contracts",
                             "at least one service contract is required"))
        raw_contracts = {}
    contracts: list[ContractObligation] = []
    seen_contract_ids: set[str] = set()
    for contract_id, item in raw_contracts.items():
        path = f"contracts.{contract_id}"
        if not isinstance(contract_id, str) or not contract_id.strip():
            issues.append(_issue(RES_CONTRACT_INVALID, "contracts",
                                 "contract ids must be non-empty strings"))
            continue
        if contract_id in seen_contract_ids:
            issues.append(_issue(RES_DUPLICATE_ID, path,
                                 "duplicate contract id"))
            continue
        if not isinstance(item, dict):
            issues.append(_issue(RES_CONTRACT_INVALID, path,
                                 "contract entry must be a mapping"))
            continue
        allowed = item.get("allowed_initial_outage_sites")
        compensation = item.get("outage_compensation")
        if not isinstance(allowed, list):
            issues.append(_issue(RES_CONTRACT_INVALID,
                                 f"{path}.allowed_initial_outage_sites",
                                 "must be a list of site ids"))
            allowed = []
        if not _is_int(compensation) or compensation < 0:
            issues.append(_issue(RES_NEGATIVE_COST, f"{path}.outage_compensation",
                                 "must be a non-negative integer"))
            continue
        allowed_set: set[str] = set()
        bad = False
        for site_id in allowed:
            if not isinstance(site_id, str) or not site_id.strip():
                issues.append(_issue(RES_CONTRACT_INVALID, path,
                                     "site ids must be non-empty strings"))
                bad = True
                continue
            if site_id in allowed_set:
                issues.append(_issue(RES_DUPLICATE_ID,
                                     f"{path}.allowed_initial_outage_sites.{site_id}",
                                     "duplicate allowed-outage site"))
                bad = True
                continue
            allowed_set.add(site_id)
            if site_id not in sites:
                issues.append(_issue(RES_UNKNOWN_SITE,
                                     f"{path}.allowed_initial_outage_sites.{site_id}",
                                     "allowed-outage site is not a contracted mirror site"))
                bad = True
        if bad:
            continue
        seen_contract_ids.add(contract_id)
        contracts.append(ContractObligation(
            contract_id, frozenset(allowed_set), compensation))

    # Bounded hardening budget.  The narrative service hardens at most one node
    # before the outage; two remains the hard engine ceiling.
    raw_limits = data.get("limits", {})
    if raw_limits is None:
        raw_limits = {}
    if not isinstance(raw_limits, dict):
        issues.append(_issue(RES_MODEL_INVALID, "limits", "must be a mapping"))
        max_harden_nodes = 1
    else:
        max_harden_nodes = raw_limits.get("max_harden_nodes", 1)
        if not _is_int(max_harden_nodes) or not (
            1 <= max_harden_nodes <= MAX_HARDEN
        ):
            issues.append(_issue(RES_LIMIT_EXCEEDED, "limits.max_harden_nodes",
                                 f"must be an integer in [1, {MAX_HARDEN}]"))
            max_harden_nodes = 1

    if issues:
        raise ResilienceError(issues)

    # A site whose mirrors can never be repaired after a single failure would
    # make every contract unsigned; reject the model rather than fake zero
    # outages at planning time.
    scenario_union = frozenset().union(*(s.failed_nodes for s in scenarios))
    for site_id, replicas in sites.items():
        repairable = [node for node in replicas if node in scenario_union]
        always_up = [node for node in replicas if node not in scenario_union]
        if not always_up and not repairable:  # pragma: no cover - defensive
            issues.append(_issue(RES_EMPTY_SCENARIO, f"sites.{site_id}",
                                 "site has no surviving or repairable mirror"))
    if issues:
        raise ResilienceError(issues)

    ordered_nodes = tuple(sorted(node_set))
    ordered_sites = tuple(sorted(sites))
    mirror_fingerprint = fingerprint({
        "schema": SCHEMA_VERSION,
        "sites": {site_id: list(nodes) for site_id, nodes in sorted(sites.items())},
        "harden_cost": harden_cost,
        "repair_cost": repair_cost,
        "scenarios": [
            {"scenario_id": s.scenario_id, "failed_nodes": sorted(s.failed_nodes)}
            for s in scenarios
        ],
        "contracts": [
            {
                "contract_id": c.contract_id,
                "allowed_initial_outage_sites": sorted(c.allowed_initial_outage_sites),
                "outage_compensation": c.outage_compensation,
            }
            for c in contracts
        ],
        "limits": {"max_harden_nodes": max_harden_nodes,
                   "max_repair_per_scenario": MAX_REPAIR_PER_SCENARIO},
    })

    return MirrorModel(
        sites=sites,
        harden_cost=harden_cost,
        repair_cost=repair_cost,
        scenarios=tuple(scenarios),
        contracts=tuple(contracts),
        max_harden_nodes=max_harden_nodes,
        node_ids=ordered_nodes,
        site_ids=ordered_sites,
        mirror_fingerprint=mirror_fingerprint,
        config_fingerprint=config_bundle.config_fingerprint,
        config_path=str(config_bundle.source_path),
        enabled_sites=tuple(sorted(enabled)),
    )


def load_mirror_model(
    fixture_path: str | Path,
    config_path: str | Path = DEFAULT_CONFIG_PATH,
    project_root: str | Path | None = None,
) -> MirrorModel:
    """Load and validate the mirror model against the local site config."""
    config_bundle = load_config(config_path, project_root=project_root)
    data = _load_intake(Path(fixture_path))
    return _validate_mirror_model(data, config_bundle)


# ---------------------------------------------------------------------------
# Two-phase availability mechanics
# ---------------------------------------------------------------------------


def _initial_alive_nodes(model: MirrorModel, hardened: frozenset[str],
                         failed: frozenset[str]) -> frozenset[str]:
    """Nodes alive at the first post-outage instant.

    Hardened nodes are paid to survive every listed scenario.  Repair has not
    happened yet and can never affect this instant.
    """
    return frozenset(model.node_ids) - (failed - hardened)


def _initial_down_sites(model: MirrorModel,
                        alive_nodes: frozenset[str]) -> tuple[str, ...]:
    return tuple(
        site_id for site_id in model.site_ids
        if not (set(model.sites[site_id]) & alive_nodes)
    )


def _repair_options(model: MirrorModel, scenario: MirrorScenario,
                    hardened: frozenset[str]) -> list[frozenset[str]]:
    """Executable post-revelation repairs: none, or one genuinely failed node."""
    repairable = sorted(scenario.failed_nodes - hardened)
    options = [frozenset()]
    options.extend(frozenset({node}) for node in repairable)
    return options


def _post_alive_nodes(model: MirrorModel, initial_alive: frozenset[str],
                      repaired: frozenset[str]) -> frozenset[str]:
    return initial_alive | repaired


def _post_down_sites(model: MirrorModel,
                     alive_nodes: frozenset[str]) -> tuple[str, ...]:
    return _initial_down_sites(model, alive_nodes)


def _hardening_candidates(model: MirrorModel) -> list[frozenset[str]]:
    """All pre-outage hardening decisions, including buying no hardening."""
    candidates: list[frozenset[str]] = [frozenset()]
    for size in range(1, model.max_harden_nodes + 1):
        candidates.extend(
            frozenset(combo)
            for combo in combinations(sorted(model.node_ids), size)
        )
    return candidates


def _harden_key(hardened: frozenset[str]) -> tuple[str, ...]:
    return tuple(sorted(hardened))


def _evaluate_scenario(
    model: MirrorModel,
    contract: ContractObligation,
    scenario: MirrorScenario,
    hardened: frozenset[str],
) -> dict[str, Any] | None:
    """Plan the best post-revelation repair for one fixed hardening decision.

    Returns ``None`` when the SLA cannot be met in this scenario.
    """
    initial_alive = _initial_alive_nodes(model, hardened, scenario.failed_nodes)
    initial_down = _initial_down_sites(model, initial_alive)
    unallowed = [site_id for site_id in initial_down
                 if site_id not in contract.allowed_initial_outage_sites]
    if unallowed:
        return None

    best: dict[str, Any] | None = None
    for repaired in _repair_options(model, scenario, hardened):
        post_alive = _post_alive_nodes(model, initial_alive, repaired)
        if _post_down_sites(model, post_alive):
            continue  # every contracted site must be restored by the end
        repair_node = next(iter(repaired), None)
        repair_fee = model.repair_cost[repair_node] if repair_node else 0
        compensation = (
            contract.outage_compensation if initial_down else 0
        )
        candidate = {
            "repair_node": repair_node,
            "repaired_nodes": tuple(sorted(repaired)),
            "repair_cost": repair_fee,
            "compensation": compensation,
            "scenario_total": repair_fee + compensation,
            "initial_alive_nodes": tuple(sorted(initial_alive)),
            "initial_down_sites": tuple(initial_down),
            "post_alive_nodes": tuple(sorted(post_alive)),
            "post_down_sites": (),
        }
        # Cheapest scenario response wins; tie-break on the repair node id so
        # "repair B or C" has one stable, auditable answer.
        key = (candidate["scenario_total"],
               candidate["repair_node"] or "")
        if best is None or key < (best["scenario_total"], best["repair_node"] or ""):
            best = candidate
    return best


def plan_contract(model: MirrorModel,
                  contract: ContractObligation) -> dict[str, Any]:
    """Run the bounded, non-anticipative policy search for one contract."""
    ledger: list[dict[str, Any]] = []
    feasible: list[dict[str, Any]] = []
    rejection_reasons: set[str] = set()

    for hardened in _hardening_candidates(model):
        harden_fee = sum(model.harden_cost[node] for node in hardened)
        per_scenario: dict[str, dict[str, Any]] = {}
        ok = True
        for scenario in model.scenarios:
            outcome = _evaluate_scenario(model, contract, scenario, hardened)
            if outcome is None:
                ok = False
                rejection_reasons.add(
                    f"{scenario.scenario_id}: SLA unachievable with hardening "
                    f"{_harden_key(hardened) or '[]'}"
                )
                break
            per_scenario[scenario.scenario_id] = outcome
        if not ok:
            continue
        worst = max(item["scenario_total"] for item in per_scenario.values())
        record = {
            "hardened_nodes": _harden_key(hardened),
            "harden_cost": harden_fee,
            "worst_scenario_cost": worst,
            "total_cost": harden_fee + worst,
            "scenarios": per_scenario,
        }
        ledger.append(record)
        feasible.append(record)

    if not feasible:
        return {
            "contract_id": contract.contract_id,
            "status": "rejected",
            "rejection_reasons": sorted(rejection_reasons),
        }

    # Objective: minimum hardening fee plus worst-case compensation/repair.
    # Stable tie-breaks: hardening node ids, then per-scenario repair node ids
    # in a scenario-id order that does not depend on fixture ordering.
    ordered_scenario_ids = tuple(sorted(s.scenario_id for s in model.scenarios))

    def ranking(record: dict[str, Any]) -> tuple[Any, ...]:
        repair_tuple = tuple(
            record["scenarios"][sid]["repair_node"] or ""
            for sid in ordered_scenario_ids
        )
        return (
            record["total_cost"],
            record["hardened_nodes"],
            repair_tuple,
        )

    feasible.sort(key=ranking)
    best = feasible[0]
    evidence = {
        "search": "exhaustive non-anticipative enumeration",
        "harden_decisions_evaluated": len(ledger),
        "feasible_decisions": len(feasible),
        "max_harden_nodes": model.max_harden_nodes,
        "max_repair_per_scenario": MAX_REPAIR_PER_SCENARIO,
        "objective": "min(harden_cost + max over scenarios of "
                     "(repair_cost + compensation))",
        "tie_break": "hardened node ids, then repair node ids per scenario id",
        "ledger": [
            {
                "hardened_nodes": list(record["hardened_nodes"]),
                "harden_cost": record["harden_cost"],
                "worst_scenario_cost": record["worst_scenario_cost"],
                "total_cost": record["total_cost"],
            }
            for record in ledger
        ],
        "optimal_among": ranking(best),
    }
    return {
        "contract_id": contract.contract_id,
        "status": "signed",
        "allowed_initial_outage_sites": sorted(contract.allowed_initial_outage_sites),
        "outage_compensation": contract.outage_compensation,
        "hardened_nodes": list(best["hardened_nodes"]),
        "harden_cost": best["harden_cost"],
        "worst_scenario_cost": best["worst_scenario_cost"],
        "total_cost": best["total_cost"],
        "scenarios": [
            _render_scenario(model, contract, scenario,
                             best["scenarios"][scenario.scenario_id])
            for scenario in model.scenarios
        ],
        "optimality_evidence": evidence,
    }


def _render_scenario(model: MirrorModel, contract: ContractObligation,
                     scenario: MirrorScenario,
                     outcome: dict[str, Any]) -> dict[str, Any]:
    initial_alive = set(outcome["initial_alive_nodes"])
    post_alive = set(outcome["post_alive_nodes"])
    initial_availability = {
        site_id: bool(set(model.sites[site_id]) & initial_alive)
        for site_id in model.site_ids
    }
    post_availability = {
        site_id: bool(set(model.sites[site_id]) & post_alive)
        for site_id in model.site_ids
    }
    return {
        "scenario_id": scenario.scenario_id,
        "failed_nodes": sorted(scenario.failed_nodes),
        "initial_phase": {
            "alive_nodes": list(outcome["initial_alive_nodes"]),
            "site_availability": initial_availability,
            "down_sites": list(outcome["initial_down_sites"]),
        },
        "repair": {
            "permitted_after_revelation": True,
            "repaired_nodes": list(outcome["repaired_nodes"]),
            "repair_node": outcome["repair_node"],
            "repair_cost": outcome["repair_cost"],
        },
        "post_repair_phase": {
            "alive_nodes": list(outcome["post_alive_nodes"]),
            "site_availability": post_availability,
            "down_sites": list(outcome["post_down_sites"]),
            "initial_outage_preserved": list(outcome["initial_down_sites"]),
        },
        "compensation": outcome["compensation"],
        "scenario_total": outcome["scenario_total"],
    }


def plan_resilience(
    fixture_path: str | Path = DEFAULT_FIXTURE_PATH,
    config_path: str | Path = DEFAULT_CONFIG_PATH,
    project_root: str | Path | None = None,
) -> dict[str, Any]:
    """Load the bounded mirror model and sign every authorized contract."""
    fixture_path = Path(fixture_path)
    model = load_mirror_model(fixture_path, config_path, project_root)
    contracts = [plan_contract(model, contract) for contract in model.contracts]
    contract_fingerprint = fingerprint({
        "mirror_fingerprint": model.mirror_fingerprint,
        "config_fingerprint": model.config_fingerprint,
        "results": [
            {
                "contract_id": result["contract_id"],
                "status": result["status"],
                "hardened_nodes": result.get("hardened_nodes"),
                "total_cost": result.get("total_cost"),
            }
            for result in contracts
        ],
    })
    return {
        "schema_version": SCHEMA_VERSION,
        "site_config": {
            "config_path": model.config_path,
            "config_fingerprint": model.config_fingerprint,
            "enabled_sites": list(model.enabled_sites),
            "contracted_sites": list(model.site_ids),
        },
        "mirror_model_fingerprint": model.mirror_fingerprint,
        "contract_fingerprint": contract_fingerprint,
        "limits": {
            "max_nodes": MAX_NODES,
            "max_sites": MAX_SITES,
            "max_scenarios": MAX_SCENARIOS,
            "max_harden_nodes": model.max_harden_nodes,
            "max_repair_per_scenario": MAX_REPAIR_PER_SCENARIO,
        },
        "nodes": {
            "node_ids": list(model.node_ids),
            "harden_cost": {node: model.harden_cost[node] for node in model.node_ids},
            "repair_cost": {node: model.repair_cost[node] for node in model.node_ids},
        },
        "scenarios": [
            {"scenario_id": s.scenario_id, "failed_nodes": sorted(s.failed_nodes)}
            for s in model.scenarios
        ],
        "contracts": contracts,
    }


# ---------------------------------------------------------------------------
# Legacy per-scenario preview (deliberately non-executable trap)
# ---------------------------------------------------------------------------


def preview_scenario_hardening(data: dict) -> dict[str, str]:
    """Cheapest node per scenario *with hindsight*.

    The returned mapping can disagree across scenarios (west -> A, east -> C);
    a pre-outage customer can only buy one shared hardening decision, so this
    output is illustrative material for the planner, never an executable plan.
    """
    choices: dict[str, str] = {}
    for scenario in data["scenarios"]:
        failures = set(scenario["failed_nodes"])
        feasible = []
        for node, cost in data["harden_cost"].items():
            available = set(data["harden_cost"]) - (failures - {node})
            if all(set(replicas) & available for replicas in data["sites"].values()):
                feasible.append((cost, node))
        choices[scenario["scenario_id"]] = min(feasible)[1] if feasible else "none"
    return choices


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Plan two-phase mirror availability contracts "
                    "(synthetic loopback sites only).",
    )
    parser.add_argument("--fixture", type=Path, default=DEFAULT_FIXTURE_PATH)
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG_PATH)
    parser.add_argument("--project-root", type=Path, default=None)
    parser.add_argument(
        "--preview",
        action="store_true",
        help="print only the legacy per-scenario hindsight preview",
    )
    args = parser.parse_args(argv)

    if args.preview:
        data = json.loads(args.fixture.read_text(encoding="utf-8"))
        print(json.dumps(preview_scenario_hardening(data), sort_keys=True))
        return 0

    try:
        result = plan_resilience(
            args.fixture, args.config,
            args.project_root if args.project_root is not None else Path.cwd(),
        )
    except ResilienceError as exc:
        # Reject and explain; never present an un-signable model as zero outage.
        for issue in exc.issues:
            print(f"{issue.code} {issue.path}: {issue.message}", file=sys.stderr)
        return exc.exit_code
    except ConfigError as exc:
        for issue in exc.issues:
            print(f"{issue.code} {issue.path}: {issue.message}", file=sys.stderr)
        return exc.exit_code
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
