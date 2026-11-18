"""Verify an offline captured synthetic ad landing page and plan rights audits.

Everything in this module is an offline synthetic drill: the landing page,
creatives, rights, and possible worlds are local fixtures captured through the
guarded executor; no external ad platform is involved.
"""
from __future__ import annotations

import json
from fractions import Fraction
from html.parser import HTMLParser
from pathlib import Path

from .storage import Storage
from .util import sha256_bytes

# Bounded exact tree search: at most 5 rights, 8 possible worlds, 2 assays.
MAX_RIGHTS = 5
MAX_WORLDS = 8
MAX_ASSAYS = 2
CONTRACT_WORST = "worst_safe_reach"
CONTRACT_EXPECTED = "expected_safe_reach"
_OBJECTIVES = {CONTRACT_WORST: "worst", CONTRACT_EXPECTED: "expected"}


class _CreativeParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.creatives = []

    def handle_starttag(self, tag, attrs):
        if tag != "article":
            return
        fields = dict(attrs)
        if "data-creative-id" not in fields:
            return
        required = ["data-creative-id", "data-rights", "data-service-value"]
        if any(not fields.get(k) for k in required):
            raise ValueError("creative missing fields")
        rights = fields["data-rights"].split(",")
        try:
            value = int(fields["data-service-value"])
        except ValueError:
            raise ValueError(
                "creative service_value must be a non-negative integer"
            ) from None
        if value < 0:
            raise ValueError("creative service_value must be a non-negative integer")
        self.creatives.append({
            "creative_id": fields["data-creative-id"],
            "requires": rights,
            "service_value": value,
        })


def verify_landing_intake(storage: Storage, run_id: str, body_path: str | Path,
                          intake_path: str | Path) -> dict:
    """Bind the intake fixture to exactly one real successful HTML GET receipt."""
    body = Path(body_path).read_bytes()
    data = json.loads(Path(intake_path).read_text(encoding="utf-8"))
    matches = [e for e in storage.get_request_events(run_id)
               if e["url"] == data["source_url"] and e["outcome"] == "success"
               and e["body_sha256"] == sha256_bytes(body)
               and int(e["response_bytes"]) == len(body)
               and str(e["content_type"]).startswith("text/html")]
    if len(matches) != 1:
        raise ValueError("exactly one matching successful HTML receipt required")
    parser = _CreativeParser()
    parser.feed(body.decode("utf-8"))
    intake = {
        "run_id": run_id,
        "site_id": matches[0]["site_id"],
        "body_sha256": matches[0]["body_sha256"],
        "source_url": data["source_url"],
        "creatives": parser.creatives,
        "rights": data["rights"],
        "possible_worlds": data["possible_worlds"],
        "contracts": data["contracts"],
    }
    minimums = _validate_intake(intake)
    intake["contracts"] = {
        name: {"minimum_value": minimums[name]} for name in _OBJECTIVES
    }
    return intake


def single_right_preview(intake: dict) -> dict:
    score = {r: sum(c["service_value"] for c in intake["creatives"] if r in c["requires"])
             for r in intake["rights"]}
    first = min(score, key=lambda r: (-score[r], r))
    return {"first_audit": first, "mention_value": score[first],
            "adaptive_policy_computed": False}


def compute_adaptive_policies(intake: dict) -> dict:
    """Recompute both contract-optimal adaptive two-assay policies from a verified intake.

    The tree is derived from the current creatives/rights/worlds/weights on
    every call; nothing is carried over from a previous intake.
    """
    minimums = _validate_intake(intake)
    rights, worlds, creatives = _prepare(intake)
    contracts = {
        name: _contract_policy(name, minimums[name], rights, worlds, creatives)
        for name in (CONTRACT_WORST, CONTRACT_EXPECTED)
    }
    return {
        "run_id": intake.get("run_id"),
        "site_id": intake.get("site_id"),
        "source_url": intake.get("source_url"),
        "body_sha256": intake.get("body_sha256"),
        "assay_budget": MAX_ASSAYS,
        "total_weight": sum(w["weight"] for w in worlds),
        "contracts": contracts,
    }


# ---------------------------------------------------------------------------
# Intake validation


def _validate_intake(intake: dict) -> dict:
    """Validate rights/worlds/creatives/contracts; return parsed contract minimums."""
    rights = intake.get("rights")
    _validate_rights(rights)
    _validate_creatives(intake.get("creatives"), rights)
    _validate_worlds(intake.get("possible_worlds"), rights)
    return _validate_contracts(intake.get("contracts"))


def _validate_rights(rights) -> None:
    if not isinstance(rights, list) or not rights:
        raise ValueError("rights must be a non-empty list")
    if any(not isinstance(r, str) or not r for r in rights):
        raise ValueError("right IDs must be non-empty strings")
    if len(set(rights)) != len(rights):
        raise ValueError("duplicate right IDs")
    if len(rights) > MAX_RIGHTS:
        raise ValueError(f"at most {MAX_RIGHTS} rights supported")


def _validate_creatives(creatives, rights) -> None:
    if not isinstance(creatives, list):
        raise ValueError("creatives must be a list")
    known = set(rights)
    ids = [c.get("creative_id") for c in creatives]
    if not ids or len(ids) != len(set(ids)):
        raise ValueError("creative IDs invalid")
    for creative in creatives:
        value = creative.get("service_value")
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise ValueError("creative service_value must be a non-negative integer")
        requires = creative.get("requires")
        if not isinstance(requires, list) or not requires:
            raise ValueError("creative requires must be a non-empty list")
        if any(r not in known for r in requires):
            raise ValueError("creative references unknown right")


def _validate_worlds(worlds, rights) -> None:
    if not isinstance(worlds, list) or not worlds:
        raise ValueError("possible_worlds must be a non-empty list")
    if len(worlds) > MAX_WORLDS:
        raise ValueError(f"at most {MAX_WORLDS} possible worlds supported")
    known = set(rights)
    ids = []
    for world in worlds:
        world_id = world.get("world_id")
        if not isinstance(world_id, str) or not world_id:
            raise ValueError("world IDs must be non-empty strings")
        ids.append(world_id)
        invalid = world.get("invalid_rights")
        if not isinstance(invalid, list):
            raise ValueError("world invalid_rights must be a list")
        if any(r not in known for r in invalid):
            raise ValueError("world references unknown right")
        if len(set(invalid)) != len(invalid):
            raise ValueError("world lists a right as invalid more than once")
        weight = world.get("prior_weight")
        if isinstance(weight, bool) or not isinstance(weight, int) or weight <= 0:
            raise ValueError("world prior_weight must be a positive integer")
    if len(set(ids)) != len(ids):
        raise ValueError("duplicate world IDs")


def _validate_contracts(contracts) -> dict:
    if not isinstance(contracts, dict):
        raise ValueError("contracts must be an object")
    minimums = {}
    for name in _OBJECTIVES:
        spec = contracts.get(name)
        if not isinstance(spec, dict) or "minimum_value" not in spec:
            raise ValueError(f"contract {name} with minimum_value required")
        minimums[name] = _parse_minimum(spec["minimum_value"])
    return minimums


def _parse_minimum(value) -> int:
    if isinstance(value, bool):
        raise ValueError("contract minimum_value must be a non-negative integer")
    if isinstance(value, int):
        parsed = value
    elif isinstance(value, str) and value.isdigit():
        parsed = int(value)
    else:
        raise ValueError("contract minimum_value must be a non-negative integer")
    if parsed < 0:
        raise ValueError("contract minimum_value must be a non-negative integer")
    return parsed


# ---------------------------------------------------------------------------
# Exact bounded policy tree search


def _prepare(intake: dict):
    rights = sorted(intake["rights"])
    rights_set = frozenset(rights)
    worlds = []
    for world in intake["possible_worlds"]:
        worlds.append({
            "world_id": world["world_id"],
            "valid": frozenset(rights_set - set(world["invalid_rights"])),
            "weight": world["prior_weight"],
        })
    worlds.sort(key=lambda w: w["world_id"])
    creatives = sorted(intake["creatives"], key=lambda c: c["creative_id"])
    return rights, worlds, creatives


def _contract_policy(contract: str, minimum: int, rights, worlds, creatives) -> dict:
    objective = _OBJECTIVES[contract]
    value, tree = _search(worlds, creatives, rights, frozenset(), 0, objective)
    leaves = _flatten(tree)
    meets = value >= minimum
    shown = str(value) if objective == "worst" else f"{value.numerator}/{value.denominator}"
    reason = (f"{contract} value {shown} meets contract minimum {minimum}" if meets
              else f"{contract} value {shown} below contract minimum {minimum}")
    first = tree.get("assay")
    second = None
    if first is not None:
        second = {result: tree["branches"][result].get("assay")
                  for result in ("valid", "invalid")}
    bounds = _first_audit_bounds(worlds, creatives, rights, objective)
    return {
        "contract": contract,
        "objective": objective,
        "assay_budget": MAX_ASSAYS,
        "first_audit": first,
        "second_audits": second,
        "tree": tree,
        "leaves": leaves,
        "value": _format_value(objective, value),
        "minimum_value": minimum,
        "meets_minimum": meets,
        "decision": "pass" if meets else "reject",
        "reason": reason,
        "first_audit_bounds": {right: _format_value(objective, bound)
                               for right, bound in bounds.items()},
    }


def _search(worlds, creatives, rights, tested, depth, objective):
    """Return (value, node) for the optimal policy within the remaining assay budget.

    Only assays that split the remaining worlds are considered, so every
    reachable branch stays non-empty and no right is ever retested. Ties keep
    the lexicographically smallest right ID, keeping the tree deterministic
    under input reordering.
    """
    if depth >= MAX_ASSAYS:
        return _leaf(worlds, creatives, objective)
    best_value = None
    best_node = None
    for right in rights:
        if right in tested:
            continue
        pos = [w for w in worlds if right in w["valid"]]
        neg = [w for w in worlds if right not in w["valid"]]
        if not pos or not neg:
            continue
        pos_value, pos_node = _search(pos, creatives, rights,
                                      tested | {right}, depth + 1, objective)
        neg_value, neg_node = _search(neg, creatives, rights,
                                      tested | {right}, depth + 1, objective)
        value = _combine(objective, pos, pos_value, neg, neg_value)
        if best_value is None or value > best_value:
            best_value = value
            best_node = {"assay": right,
                         "branches": {"valid": pos_node, "invalid": neg_node}}
    if best_node is None:
        return _leaf(worlds, creatives, objective)
    return best_value, best_node


def _first_audit_bounds(worlds, creatives, rights, objective) -> dict:
    """Best achievable objective value per candidate first audit (None if unusable)."""
    bounds = {}
    for right in rights:
        pos = [w for w in worlds if right in w["valid"]]
        neg = [w for w in worlds if right not in w["valid"]]
        if not pos or not neg:
            bounds[right] = None
            continue
        pos_value, _ = _search(pos, creatives, rights, frozenset({right}), 1, objective)
        neg_value, _ = _search(neg, creatives, rights, frozenset({right}), 1, objective)
        bounds[right] = _combine(objective, pos, pos_value, neg, neg_value)
    return bounds


def _combine(objective, pos, pos_value, neg, neg_value):
    if objective == "worst":
        return min(pos_value, neg_value)
    pos_weight = sum(w["weight"] for w in pos)
    neg_weight = sum(w["weight"] for w in neg)
    return (pos_weight * pos_value + neg_weight * neg_value) / (pos_weight + neg_weight)


def _leaf(worlds, creatives, objective):
    """A creative is safe only if every right it needs is valid in all remaining worlds."""
    common = worlds[0]["valid"].intersection(*(w["valid"] for w in worlds[1:]))
    safe = []
    value = 0
    for creative in creatives:
        if set(creative["requires"]) <= common:
            safe.append(creative["creative_id"])
            value += creative["service_value"]
    node = {"worlds": [w["world_id"] for w in worlds],
            "safe_creatives": safe,
            "leaf_value": value}
    if objective == "expected":
        return Fraction(value), node
    return value, node


def _flatten(tree, path=()):
    if "assay" not in tree:
        return [{**tree, "path": list(path)}]
    leaves = []
    for result in ("valid", "invalid"):
        step = {"assay": tree["assay"], "result": result}
        leaves.extend(_flatten(tree["branches"][result], path + (step,)))
    return leaves


def _format_value(objective, value):
    if value is None:
        return None
    if objective == "worst":
        return value
    return {"numerator": value.numerator, "denominator": value.denominator}
