"""Read synthetic lead tokens from one verified captured form page.

The intake module deliberately does two separate things:

* :func:`verify_lead_intake` binds parsed forms to a *real* successful HTML
  receipt in the crawl ledger (URL, body SHA-256, byte length and content
  type).  No attribution may be produced from a failed GET or a body whose
  bytes do not match that receipt.
* :func:`partition_lead_identities` / :func:`analyze_lead_identities` turn the
  verified forms into a complete, exact identity partition for one or both
  synthetic sale contracts.  Every form belongs to exactly one entity group;
  groups never cross tenants, never contain a ``cannot_link`` pair, and every
  multi-record group is connected by same-token evidence edges.

The search space is tiny by construction (the fixture has eight forms), so the
optimal partition is found by exhaustive enumeration over set partitions.  The
result therefore carries both the optimum and an independently reviewable
upper bound.
"""

from __future__ import annotations

import json
from html.parser import HTMLParser
from pathlib import Path

from .storage import Storage
from .util import sha256_bytes

CONSENT_VALUES = ("yes", "no", "unknown")

# (evidence kind, per-lead token attribute, contract weight attribute)
EVIDENCE_KINDS = (
    ("email", "email_token", "email_evidence"),
    ("phone", "phone_token", "phone_evidence"),
    ("account", "account_token", "account_evidence"),
)

# Exact set-partition search is Bell(n).  The synthetic forms page is capped at
# eight records, so the search stays bounded (Bell(8) = 4140 partitions).
MAX_EXACT_LEADS = 8

_REQUIRED_LEAD_FIELDS = (
    "data-lead-id",
    "data-tenant",
    "data-email-token",
    "data-phone-token",
    "data-account-token",
    "data-consent",
)


class _LeadParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.leads = []

    def handle_starttag(self, tag, attrs):
        if tag != "article":
            return
        row = dict(attrs)
        if "data-lead-id" not in row:
            return
        if any(not row.get(key) for key in _REQUIRED_LEAD_FIELDS):
            raise ValueError("lead form missing fields")
        self.leads.append(
            {
                "lead_id": row["data-lead-id"],
                "tenant": row["data-tenant"],
                "email_token": row["data-email-token"],
                "phone_token": row["data-phone-token"],
                "account_token": row["data-account-token"],
                "consent": row["data-consent"],
                "sale_id": row.get("data-sale-id", ""),
            }
        )


def _non_empty_text(value, label):
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{label} must be a non-empty string")
    return value


def _non_negative_int(value, label):
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{label} must be a non-negative integer")
    return value


def _validate_lead(row, index):
    if not isinstance(row, dict):
        raise ValueError(f"lead[{index}] must be an object")
    lead_id = _non_empty_text(row.get("lead_id"), f"lead[{index}].lead_id")
    tenant = _non_empty_text(row.get("tenant"), f"lead {lead_id} tenant")
    tokens = {}
    for _kind, token_field, _weight_field in EVIDENCE_KINDS:
        tokens[token_field] = _non_empty_text(
            row.get(token_field), f"lead {lead_id} {token_field}"
        )
    consent = row.get("consent")
    if consent not in CONSENT_VALUES:
        raise ValueError(
            f"lead {lead_id} consent must be one of yes/no/unknown"
        )
    sale_id = row.get("sale_id", "")
    if not isinstance(sale_id, str):
        raise ValueError(f"lead {lead_id} sale_id must be a string")
    if sale_id and not sale_id.strip():
        raise ValueError(f"lead {lead_id} sale_id must not be blank")
    return {
        "lead_id": lead_id,
        "tenant": tenant,
        "email_token": tokens["email_token"],
        "phone_token": tokens["phone_token"],
        "account_token": tokens["account_token"],
        "consent": consent,
        "sale_id": sale_id,
    }


def _validate_contracts(raw):
    if not isinstance(raw, dict) or not raw:
        raise ValueError("contracts must be a non-empty object")
    contracts = {}
    for name, spec in raw.items():
        _non_empty_text(name, "contract name")
        if not isinstance(spec, dict):
            raise ValueError(f"contract {name} must be an object")
        weights = {}
        for _kind, _token_field, weight_field in EVIDENCE_KINDS:
            weights[weight_field] = _non_negative_int(
                spec.get(weight_field), f"contract {name} {weight_field}"
            )
        unit = _non_empty_text(spec.get("unit"), f"contract {name} unit")
        contracts[name] = {**weights, "unit": unit}
    return contracts


def _validate_cannot_link(raw, lead_ids):
    if not isinstance(raw, list):
        raise ValueError("cannot_link must be a list")
    pairs = set()
    for index, pair in enumerate(raw):
        if not isinstance(pair, (list, tuple)) or len(pair) != 2:
            raise ValueError(f"cannot_link[{index}] must contain two lead IDs")
        left, right = pair
        if not isinstance(left, str) or not isinstance(right, str):
            raise ValueError(f"cannot_link[{index}] lead IDs must be strings")
        if left == right:
            raise ValueError(f"cannot_link[{index}] must not self-reference")
        if left not in lead_ids or right not in lead_ids:
            raise ValueError(f"cannot_link[{index}] references an unknown lead")
        pairs.add(_pair_key(left, right))
    return [list(pair) for pair in sorted(pairs)]


def _validate_intake(data):
    """Validate and normalise the parts of an intake document we act on."""
    if not isinstance(data, dict):
        raise ValueError("lead intake must be an object")
    for field in ("run_id", "body_sha256"):
        value = data.get(field)
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"lead intake must be bound to a non-empty {field}")
    raw_leads = data.get("leads")
    if not isinstance(raw_leads, list) or not raw_leads:
        raise ValueError("lead intake must contain at least one lead")
    if len(raw_leads) > MAX_EXACT_LEADS:
        raise ValueError(
            f"at most {MAX_EXACT_LEADS} lead forms are supported by exact search"
        )
    leads = [_validate_lead(row, index) for index, row in enumerate(raw_leads)]
    lead_ids = [row["lead_id"] for row in leads]
    if len(lead_ids) != len(set(lead_ids)):
        raise ValueError("lead IDs must be unique")
    sale_ids = [row["sale_id"] for row in leads if row["sale_id"]]
    if len(sale_ids) != len(set(sale_ids)):
        raise ValueError("sale IDs must be unique")
    cannot_link = _validate_cannot_link(data.get("cannot_link"), set(lead_ids))
    contracts = _validate_contracts(data.get("contracts"))
    # Sorting by lead ID makes every downstream result independent of HTML /
    # caller ordering.
    leads.sort(key=lambda row: row["lead_id"])
    return {"leads": leads, "cannot_link": cannot_link, "contracts": contracts}


def verify_lead_intake(
    storage: Storage,
    run_id: str,
    body_path: str | Path,
    intake_path: str | Path,
) -> dict:
    """Parse forms only when backed by one real successful HTML GET receipt."""
    body = Path(body_path).read_bytes()
    config = json.loads(Path(intake_path).read_text(encoding="utf-8"))
    if not isinstance(config, dict):
        raise ValueError("lead intake config must be an object")
    source_url = config.get("source_url")
    if not isinstance(source_url, str) or not source_url:
        raise ValueError("lead intake config source_url must be a non-empty string")

    matches = [
        event
        for event in storage.get_request_events(run_id)
        if event["url"] == source_url
        and event["outcome"] == "success"
        and event["status_code"] is not None
        and 200 <= int(event["status_code"]) <= 299
        and event["body_sha256"] == sha256_bytes(body)
        and int(event["response_bytes"]) == len(body)
        and str(event["content_type"]).startswith("text/html")
    ]
    if len(matches) != 1:
        raise ValueError("exactly one matching successful HTML receipt required")

    parser = _LeadParser()
    parser.feed(body.decode("utf-8"))
    parsed = _validate_intake(
        {
            "run_id": run_id,
            "body_sha256": matches[0]["body_sha256"],
            "leads": parser.leads,
            "cannot_link": config.get("cannot_link"),
            "contracts": config.get("contracts"),
        }
    )
    return {
        "run_id": run_id,
        "body_sha256": matches[0]["body_sha256"],
        "site_id": matches[0]["site_id"],
        "leads": parsed["leads"],
        "cannot_link": parsed["cannot_link"],
        "contracts": parsed["contracts"],
    }


def preview_token_components(intake: dict) -> list[list[str]]:
    """Naively merge any shared token, without tenant or contradiction rules.

    This is intentionally *not* a valid identity partition.  It demonstrates
    why transitive token merging is unsafe: ``r1``/``r3`` are a declared
    cannot-link pair that share a token only through ``r2``, and ``r7``/``r8``
    share every token but belong to different tenants.
    """
    parent = {row["lead_id"]: row["lead_id"] for row in intake["leads"]}

    def root(x):
        while parent[x] != x:
            x = parent[x]
        return x

    for i, left in enumerate(intake["leads"]):
        for right in intake["leads"][i + 1:]:
            if (
                left["email_token"] == right["email_token"]
                or left["phone_token"] == right["phone_token"]
                or left["account_token"] == right["account_token"]
            ):
                parent[root(right["lead_id"])] = root(left["lead_id"])
    groups = {}
    for row in intake["leads"]:
        groups.setdefault(root(row["lead_id"]), []).append(row["lead_id"])
    return sorted([sorted(value) for value in groups.values()])


def _pair_key(left_id: str, right_id: str) -> tuple[str, str]:
    return (left_id, right_id) if left_id < right_id else (right_id, left_id)


def _pair_evidence(leads, weights):
    """Map ``(left_index, right_index)`` to shared kinds and their evidence.

    Indices are positions in the lead-ID-sorted list, so the key is already
    canonical (left < right).  Each shared token kind contributes its contract
    weight exactly once per pair.
    """
    evidence = {}
    for i in range(len(leads)):
        for j in range(i + 1, len(leads)):
            kinds = []
            detail = {}
            for kind, token_field, weight_field in EVIDENCE_KINDS:
                if leads[i][token_field] == leads[j][token_field]:
                    kinds.append(kind)
                    detail[kind] = weights[weight_field]
            if kinds:
                evidence[(i, j)] = {
                    "kinds": kinds,
                    "weights": detail,
                    "score": sum(detail.values()),
                }
    return evidence


def _consent_evidence(leads, indices):
    evidence = {"yes": [], "no": [], "unknown": []}
    for index in indices:
        evidence[leads[index]["consent"]].append(leads[index]["lead_id"])
    for values in evidence.values():
        values.sort()
    if evidence["no"]:
        state = "denied"
    elif evidence["yes"]:
        state = "granted"
    else:
        state = "unknown"
    return state, evidence


def _consent_reason(state, evidence):
    if state == "granted":
        return "affirmative consent from " + ", ".join(evidence["yes"])
    if state == "denied":
        return "consent vetoed by " + ", ".join(evidence["no"])
    return (
        "no affirmative consent; unknown only from "
        + (", ".join(evidence["unknown"]) or "no lead")
    )


def _group_score(group, evidence):
    total = 0
    for position, left in enumerate(group):
        for right in group[position + 1:]:
            detail = evidence.get((left, right))
            if detail is not None:
                total += detail["score"]
    return total


def _group_legal(group, leads, cannot_pairs, evidence):
    """Same tenant, no cannot_link pair, and evidence-connected when > 1."""
    tenant = leads[group[0]]["tenant"]
    if any(leads[index]["tenant"] != tenant for index in group):
        return False
    for position, left in enumerate(group):
        for right in group[position + 1:]:
            if _pair_key(leads[left]["lead_id"], leads[right]["lead_id"]) in cannot_pairs:
                return False
    if len(group) == 1:
        return True
    # Every record must be reachable from the group's anchor through
    # same-token evidence edges within the group; records that share nothing
    # with the rest of the group may not be merged "for free".
    reached = {group[0]}
    pending = [group[0]]
    while pending:
        current = pending.pop()
        for candidate in group:
            if candidate in reached:
                continue
            key = (current, candidate) if current < candidate else (candidate, current)
            if key in evidence:
                reached.add(candidate)
                pending.append(candidate)
    return len(reached) == len(group)


def _search_partition(leads, cannot_pairs, evidence):
    """Exhaustively enumerate every legal set partition.

    Enumerates all set partitions in canonical restricted-growth form (each
    partition exactly once), keeping only partitions whose groups are
    same-tenant, free of ``cannot_link`` pairs, and evidence-connected.  The
    optimum is the maximum possible total evidence; ties are broken by the
    sorted list of group IDs (a group's ID is its lexicographically smallest
    member lead ID), then by the sorted member lists.
    """
    n = len(leads)
    groups: list[list[int]] = []
    candidates = []

    def record():
        for group in groups:
            if not _group_legal(group, leads, cannot_pairs, evidence):
                return
        score = sum(_group_score(group, evidence) for group in groups)
        group_ids = tuple(sorted(leads[group[0]]["lead_id"] for group in groups))
        members = tuple(
            sorted(
                tuple(sorted(leads[index]["lead_id"] for index in group))
                for group in groups
            )
        )
        candidates.append((score, (group_ids, members), [list(group) for group in groups]))

    def recurse(index):
        if index == n:
            record()
            return
        for group in groups:
            group.append(index)
            recurse(index + 1)
            group.pop()
        groups.append([index])
        recurse(index + 1)
        groups.pop()

    recurse(0)

    best_score = max(candidate[0] for candidate in candidates)
    best = [candidate for candidate in candidates if candidate[0] == best_score]
    chosen = min(best, key=lambda candidate: candidate[1])
    return {
        "total_score": best_score,
        "groups": chosen[2],
        "partitions_evaluated": len(candidates),
        "optimal_partitions": len(best),
        "unique_optimum": len(best) == 1,
        "evidence": evidence,
        "cannot_pairs": cannot_pairs,
    }


def _upper_bound(leads, evidence, cannot_pairs):
    """Reviewable upper bound: every allowed pair's evidence counted at most once.

    A group's score is the sum of evidence over *all* its internal pairs, so
    summing every legal pair in a tenant/evidence component can never be less
    than the optimum for that component.  Cross-tenant and cannot-link pairs
    are excluded because they can never be counted.
    """
    n = len(leads)
    parent = list(range(n))

    def root(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    allowed = []
    for (i, j), detail in evidence.items():
        if leads[i]["tenant"] != leads[j]["tenant"]:
            continue
        if _pair_key(leads[i]["lead_id"], leads[j]["lead_id"]) in cannot_pairs:
            continue
        allowed.append((i, j, detail))
        left, right = root(i), root(j)
        if left != right:
            parent[right] = left

    components = {}
    for index in range(n):
        components.setdefault(root(index), []).append(index)

    rows = []
    total = 0
    for indices in components.values():
        indices_set = set(indices)
        pairs = [
            {
                "left": leads[i]["lead_id"],
                "right": leads[j]["lead_id"],
                "kinds": list(detail["kinds"]),
                "score": detail["score"],
            }
            for i, j, detail in allowed
            if i in indices_set and j in indices_set
        ]
        bound = sum(pair["score"] for pair in pairs)
        total += bound
        rows.append(
            {
                "members": sorted(leads[index]["lead_id"] for index in indices),
                "bound": bound,
                "pairs": pairs,
            }
        )
    rows.sort(key=lambda row: row["members"])
    return total, rows


def _rejected_links(leads, evidence, cannot_pairs):
    rejected = []
    for i in range(len(leads)):
        for j in range(i + 1, len(leads)):
            left = leads[i]["lead_id"]
            right = leads[j]["lead_id"]
            reasons = []
            if leads[i]["tenant"] != leads[j]["tenant"]:
                reasons.append("tenant_mismatch")
            is_cannot_link = _pair_key(left, right) in cannot_pairs
            if is_cannot_link:
                reasons.append("cannot_link")
            if not reasons:
                continue
            detail = evidence.get((i, j))
            # A tenant split only needs reporting when there was actually a
            # token edge to reject.  cannot_link is always evidence on its own.
            if detail is None and not is_cannot_link:
                continue
            shared_kinds = list(detail["kinds"]) if detail else []
            shared_evidence = dict(detail["weights"]) if detail else {}
            if "tenant_mismatch" in reasons:
                rule = "same group may not cross tenants"
            else:
                rule = "same group may not contain a cannot_link pair"
            rejected.append(
                {
                    "left": left,
                    "right": right,
                    "reason": reasons[0],
                    "reasons": reasons,
                    "tenants": {left: leads[i]["tenant"], right: leads[j]["tenant"]},
                    "shared_kinds": shared_kinds,
                    "shared_evidence": shared_evidence,
                    "cannot_link_pair": [left, right] if is_cannot_link else None,
                    "evidence": {
                        "rule": rule,
                        "tenant": {left: leads[i]["tenant"], right: leads[j]["tenant"]},
                        "shared_kinds": shared_kinds,
                        "shared_evidence": shared_evidence,
                        "cannot_link_pair": [left, right] if is_cannot_link else None,
                    },
                }
            )
    rejected.sort(key=lambda row: (row["left"], row["right"]))
    return rejected


def partition_lead_identities(intake: dict, contract_name: str) -> dict:
    """Return the unique-score optimal identity partition for one contract.

    The result is bound to the intake's ``run_id`` / ``body_sha256`` and details
    every group's members, supporting evidence edges, consent state and sales,
    every rejected tenant/cannot-link connection, the optimal total, the
    exhaustive-search count and a reviewable upper bound.
    """
    validated = _validate_intake(intake)
    leads = validated["leads"]
    contracts = validated["contracts"]
    if contract_name not in contracts:
        raise ValueError(f"unknown contract {contract_name!r}")
    spec = contracts[contract_name]
    weights = {kind: spec[weight_field] for kind, _token, weight_field in EVIDENCE_KINDS}
    token_weights = {weight_field: spec[weight_field] for _k, _t, weight_field in EVIDENCE_KINDS}
    cannot_pairs = {
        _pair_key(left, right) for left, right in validated["cannot_link"]
    }

    hyperedges = _pair_evidence(leads, token_weights)
    search = _search_partition(leads, cannot_pairs, hyperedges)

    evidence = search["evidence"]
    groups = []
    sales = []
    for indices in search["groups"]:
        indices.sort()
        member_ids = sorted(leads[index]["lead_id"] for index in indices)
        group_id = member_ids[0]
        score = 0
        edges = []
        for position, left_index in enumerate(indices):
            for right_index in indices[position + 1:]:
                detail = evidence.get((left_index, right_index))
                if detail is None:
                    continue
                score += detail["score"]
                edges.append(
                    {
                        "left": leads[left_index]["lead_id"],
                        "right": leads[right_index]["lead_id"],
                        "kinds": list(detail["kinds"]),
                        "weights": dict(detail["weights"]),
                        "score": detail["score"],
                    }
                )
        state, consent = _consent_evidence(leads, indices)
        sale_ids = sorted(leads[index]["sale_id"] for index in indices if leads[index]["sale_id"])
        groups.append(
            {
                "group_id": group_id,
                "run_id": intake.get("run_id"),
                "body_sha256": intake.get("body_sha256"),
                "tenant": leads[indices[0]]["tenant"],
                "members": member_ids,
                "score": score,
                "evidence_edges": edges,
                "consent": state,
                "consent_evidence": consent,
                "sale_ids": sale_ids,
            }
        )
        for index in indices:
            sale_id = leads[index]["sale_id"]
            if not sale_id:
                continue
            billable = state == "granted"
            reason = _consent_reason(state, consent)
            sales.append(
                {
                    "sale_id": sale_id,
                    "lead_id": leads[index]["lead_id"],
                    "run_id": intake.get("run_id"),
                    "body_sha256": intake.get("body_sha256"),
                    "contract": contract_name,
                    "group_id": group_id,
                    "members": member_ids,
                    "consent": state,
                    "consent_evidence": consent,
                    "billable": billable,
                    "reason": (
                        "billable: group consent granted; " + reason
                        if billable
                        else "not billable: " + reason
                    ),
                }
            )
    groups.sort(key=lambda group: group["group_id"])
    sales.sort(key=lambda sale: sale["sale_id"])

    # Group scores must add up to the searched total; keep the assertion local
    # so a future change to group construction cannot silently drift.
    assert sum(group["score"] for group in groups) == search["total_score"]
    bound, bound_components = _upper_bound(leads, evidence, cannot_pairs)

    return {
        "run_id": intake.get("run_id"),
        "body_sha256": intake.get("body_sha256"),
        "site_id": intake.get("site_id"),
        "contract": contract_name,
        "unit": spec["unit"],
        "weights": weights,
        "groups": groups,
        "total_score": search["total_score"],
        "upper_bound": bound,
        "upper_bound_components": bound_components,
        "optimality": {
            "method": "exhaustive_set_partition_search",
            "partitions_evaluated": search["partitions_evaluated"],
            "optimal_partitions": search["optimal_partitions"],
            "unique_optimum": search["unique_optimum"],
            "tie_break": "sorted group_id list, then sorted member lists",
        },
        "rejected_links": _rejected_links(leads, evidence, cannot_pairs),
        "sales": sales,
        "sales_summary": {
            "billable": [sale["sale_id"] for sale in sales if sale["billable"]],
            "not_billable": [sale["sale_id"] for sale in sales if not sale["billable"]],
        },
    }


def analyze_lead_identities(intake: dict) -> dict:
    """Partition the verified intake under every contract in its config."""
    validated = _validate_intake(intake)
    contracts = {
        name: partition_lead_identities(intake, name)
        for name in sorted(validated["contracts"])
    }
    return {
        "run_id": intake.get("run_id"),
        "body_sha256": intake.get("body_sha256"),
        "site_id": intake.get("site_id"),
        "contracts": contracts,
        "sales": {name: report["sales"] for name, report in contracts.items()},
    }
