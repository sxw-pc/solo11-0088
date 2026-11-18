"""Offline tests for verified synthetic lead identity partitioning and billing.

Every test is isolated: partition tests build intake documents in memory, and
receipt tests start the loopback mock site from a temporary pages directory, so
nothing leaves 127.0.0.1 and no fixture file is mutated.
"""

from __future__ import annotations

import copy
import json
import re
from pathlib import Path

import pytest

from crawlbudget.lead_identity_intake import (
    analyze_lead_identities,
    partition_lead_identities,
    preview_token_components,
    verify_lead_intake,
)
from crawlbudget.pipeline import run_demo
from crawlbudget.storage import Storage
from crawlbudget.util import sha256_bytes

ROOT = Path(__file__).resolve().parents[1]
FIX = ROOT / "fixtures/lead_identity"

CONTRACTS = {
    "person_identity": {
        "email_evidence": 5,
        "phone_evidence": 1,
        "account_evidence": 1,
        "unit": "individual",
    },
    "household_identity": {
        "email_evidence": 2,
        "phone_evidence": 6,
        "account_evidence": 7,
        "unit": "household",
    },
}
CANNOT_LINK = [["r1", "r3"], ["r4", "r6"], ["r5", "r7"]]
PERSON_GROUPS = [["r1", "r2"], ["r3"], ["r4", "r5"], ["r6", "r7"], ["r8"]]
HOUSEHOLD_GROUPS = [["r1"], ["r2", "r3"], ["r4", "r5"], ["r6", "r7"], ["r8"]]

_ARTICLE = re.compile(r"<article ([^>]*)></article>")
_ATTR = re.compile(r'data-([a-z-]+)="([^"]*)"')


def parse_forms(html: str) -> list[dict]:
    """Parse fixture articles without relying on the module's own parser."""
    leads = []
    for match in _ARTICLE.finditer(html):
        row = dict(_ATTR.findall(match.group(1)))
        leads.append(
            {
                "lead_id": row["lead-id"],
                "tenant": row["tenant"],
                "email_token": row["email-token"],
                "phone_token": row["phone-token"],
                "account_token": row["account-token"],
                "consent": row["consent"],
                "sale_id": row.get("sale-id", ""),
            }
        )
    return leads


def fixture_leads() -> list[dict]:
    body = (FIX / "mock-leads_forms.html").read_text(encoding="utf-8")
    return parse_forms(body)


def build_intake(
    leads: list[dict],
    *,
    cannot_link: list[list[str]] | None = None,
    contracts: dict | None = None,
    run_id: str = "test-run",
    body_sha256: str = "0" * 64,
) -> dict:
    return {
        "run_id": run_id,
        "body_sha256": body_sha256,
        "site_id": "mock-leads",
        "leads": copy.deepcopy(leads),
        "cannot_link": copy.deepcopy(CANNOT_LINK if cannot_link is None else cannot_link),
        "contracts": copy.deepcopy(CONTRACTS if contracts is None else contracts),
    }


def group_members(report: dict) -> list[list[str]]:
    return [group["members"] for group in report["groups"]]


def page_run(tmp_path: Path, page_bytes: bytes, *, run_id: str = "lead-run") -> tuple[Path, Path]:
    """Run the real mock server + executor over a temporary forms page."""
    pages = tmp_path / "pages"
    pages.mkdir(parents=True)
    page = pages / "forms.html"
    page.write_bytes(page_bytes)
    manifest = pages / "manifest.yaml"
    manifest.write_text(
        "mock-leads:\n  forms.html:\n    file: forms.html\n", encoding="utf-8"
    )
    db = tmp_path / "leads.sqlite3"
    result = run_demo(
        FIX / "site.yaml",
        project_root=ROOT,
        db_path=db,
        reports_dir=tmp_path / "reports",
        pages_dir=pages,
        manifest_path=manifest,
        run_id=run_id,
    )
    assert result.status == "completed"
    return db, page


def verified_report(tmp_path: Path, page_bytes: bytes, *, run_id: str = "lead-run") -> dict:
    db, page = page_run(tmp_path, page_bytes, run_id=run_id)
    with Storage(db) as storage:
        intake = verify_lead_intake(storage, run_id, page, FIX / "intake.json")
    return analyze_lead_identities(intake)


def comments_span(html: str) -> tuple[str, str, str]:
    head, rest = html.split("<body>", 1)
    body, tail = rest.split("</body>", 1)
    return head + "<body>", body, "</body>" + tail


def replace_article(html: str, lead_id: str, **changes: str) -> str:
    pattern = re.compile(r'<article data-lead-id="' + re.escape(lead_id) + r'"[^>]*></article>')

    def patch(match: re.Match) -> str:
        article = match.group(0)
        for key, value in changes.items():
            attribute = "data-" + key.replace("_", "-")
            article = re.sub(
                attribute + r'="[^"]*"', f'{attribute}="{value}"', article
            )
        return article

    updated, count = pattern.subn(patch, html)
    assert count == 1
    return updated


# --------------------------------------------------------------------------
# Verified intake -> exact partitions and billable sales
# --------------------------------------------------------------------------


def test_verified_intake_yields_both_contracts_and_sales(tmp_path):
    db, page = page_run(tmp_path, (FIX / "mock-leads_forms.html").read_bytes())
    with Storage(db) as storage:
        intake = verify_lead_intake(storage, "lead-run", page, FIX / "intake.json")

    assert intake["run_id"] == "lead-run"
    assert len(intake["leads"]) == 8

    person = partition_lead_identities(intake, "person_identity")
    assert person["run_id"] == "lead-run"
    assert person["body_sha256"] == intake["body_sha256"]
    assert person["total_score"] == 7
    assert group_members(person) == PERSON_GROUPS
    # Every record belongs to exactly one entity group.
    flat = [lead_id for group in group_members(person) for lead_id in group]
    assert sorted(flat) == [row["lead_id"] for row in intake["leads"]]
    assert person["optimality"]["unique_optimum"] is True
    assert person["sales_summary"] == {"billable": ["S2", "S7"], "not_billable": []}

    household = partition_lead_identities(intake, "household_identity")
    assert household["total_score"] == 18
    assert group_members(household) == HOUSEHOLD_GROUPS
    assert household["unit"] == "household"
    assert person["unit"] == "individual"
    assert household["weights"] == {"email": 2, "phone": 6, "account": 7}
    assert person["weights"] == {"email": 5, "phone": 1, "account": 1}
    assert household["optimality"]["unique_optimum"] is True
    assert household["sales_summary"] == {"billable": ["S7"], "not_billable": ["S2"]}
    household_sales = {sale["sale_id"]: sale for sale in household["sales"]}
    assert household_sales["S2"]["group_id"] == "r2"
    assert household_sales["S2"]["billable"] is False
    assert "r3" in household_sales["S2"]["reason"]
    assert household_sales["S7"]["group_id"] == "r6"
    assert household_sales["S7"]["billable"] is True

    # Upper bounds are independently reviewable and never below the optimum.
    for report in (person, household):
        component_total = sum(row["bound"] for row in report["upper_bound_components"])
        assert report["upper_bound"] == component_total
        assert report["upper_bound"] >= report["total_score"]
        for group in report["groups"]:
            assert group["run_id"] == "lead-run"
            assert group["body_sha256"] == intake["body_sha256"]

    analyzed = analyze_lead_identities(intake)
    assert set(analyzed["contracts"]) == {"person_identity", "household_identity"}
    assert [s["sale_id"] for s in analyzed["contracts"]["person_identity"]["sales"]] == [
        "S2",
        "S7",
    ]


def test_naive_preview_is_not_a_legal_partition():
    intake = build_intake(fixture_leads())
    preview = preview_token_components(intake)
    assert preview == [["r1", "r2", "r3"], ["r4", "r5", "r6", "r7", "r8"]]

    person = partition_lead_identities(intake, "person_identity")
    assert group_members(person) != preview
    tenants = {row["lead_id"]: row["tenant"] for row in fixture_leads()}
    for group in person["groups"]:
        members = set(group["members"])
        assert len({tenants[lead_id] for lead_id in members}) == 1
        for left, right in CANNOT_LINK:
            assert not {left, right} <= members
    # The preview merges r1/r3 only through r2 and r4..r8 across a tenant line.
    assert any({"r1", "r3"} <= set(group) for group in preview)
    assert any({"r7", "r8"} <= set(group) for group in preview)
    assert {"r7", "r8"} not in [set(group["members"]) for group in person["groups"]]


def test_every_multi_member_group_is_token_evidence_connected():
    intake = build_intake(fixture_leads())
    for contract in CONTRACTS:
        report = partition_lead_identities(intake, contract)
        for group in report["groups"]:
            members = group["members"]
            if len(members) < 2:
                continue
            reached = {members[0]}
            changed = True
            while changed:
                changed = False
                for edge in group["evidence_edges"]:
                    if edge["left"] in reached and edge["right"] not in reached:
                        reached.add(edge["right"])
                        changed = True
                    if edge["right"] in reached and edge["left"] not in reached:
                        reached.add(edge["left"])
                        changed = True
            assert reached == set(members)
            assert group["score"] == sum(edge["score"] for edge in group["evidence_edges"])


def greedy_merge(intake: dict, contract_name: str) -> tuple[list[list[str]], int]:
    """Deterministic high-to-low edge greedy, mirroring the documented failure."""
    from crawlbudget.lead_identity_intake import EVIDENCE_KINDS

    leads = sorted(intake["leads"], key=lambda row: row["lead_id"])
    tenant = {row["lead_id"]: row["tenant"] for row in leads}
    spec = intake["contracts"][contract_name]
    weights = {kind: spec[field] for kind, _token, field in EVIDENCE_KINDS}
    cannot = {tuple(sorted(pair)) for pair in intake["cannot_link"]}

    edges = []
    for i in range(len(leads)):
        for j in range(i + 1, len(leads)):
            kinds = [
                kind
                for kind, token, _field in EVIDENCE_KINDS
                if leads[i][token] == leads[j][token]
            ]
            score = sum(weights[kind] for kind in kinds)
            if score:
                edges.append((score, leads[i]["lead_id"], leads[j]["lead_id"]))
    edges.sort(key=lambda edge: (-edge[0], edge[1], edge[2]))
    edge_scores = {(left, right): score for score, left, right in edges}

    groups = [[row["lead_id"]] for row in leads]

    def locate(lead_id):
        return next(group for group in groups if lead_id in group)

    def mergeable(left_group, right_group):
        if any(
            tenant[a] != tenant[b] for a in left_group for b in right_group
        ):
            return False
        merged = set(left_group) | set(right_group)
        return not any(
            pair in cannot
            for position, a in enumerate(sorted(merged))
            for b in sorted(merged)[position + 1:]
            for pair in [tuple(sorted((a, b)))]
        )

    for _score, left, right in edges:
        left_group, right_group = locate(left), locate(right)
        if left_group is right_group or not mergeable(left_group, right_group):
            continue
        left_group.extend(right_group)
        groups.remove(right_group)

    groups = [sorted(group) for group in groups]
    total = sum(
        edge_scores[(left, right)]
        for group in groups
        for position, left in enumerate(group)
        for right in group[position + 1:]
        if (left, right) in edge_scores
    )
    return groups, total


def test_household_greedy_merge_is_beaten_by_optimum():
    """Regression: high-to-low greedy scores 13, the exact optimum scores 18."""
    intake = build_intake(fixture_leads())
    optimal = partition_lead_identities(intake, "household_identity")
    assert optimal["total_score"] == 18

    greedy_groups, greedy_score = greedy_merge(intake, "household_identity")
    # Greedy takes the 7-point account edge r5-r6 first...
    assert ["r5", "r6"] in greedy_groups
    # ...which blocks both 6-point phone edges around it: 7 < 6 + 6 = 12.
    assert ["r4", "r5"] not in greedy_groups
    assert ["r6", "r7"] not in greedy_groups
    blocked_score = sum(
        edge["score"]
        for group in optimal["groups"]
        if group["members"] in (["r4", "r5"], ["r6", "r7"])
        for edge in group["evidence_edges"]
        if edge["kinds"] == ["phone"]
    )
    assert blocked_score == 12
    assert greedy_score == 13
    assert greedy_score < optimal["total_score"]# --------------------------------------------------------------------------
# Tenant, cannot_link, consent and sale rules
# --------------------------------------------------------------------------


def test_tenant_and_cannot_link_evidence_is_reported():
    report = partition_lead_identities(build_intake(fixture_leads()), "person_identity")
    rejected = {(row["left"], row["right"]): row for row in report["rejected_links"]}
    assert rejected[("r1", "r3")]["reason"] == "cannot_link"
    assert rejected[("r1", "r3")]["cannot_link_pair"] == ["r1", "r3"]
    assert rejected[("r6", "r8")]["reason"] == "tenant_mismatch"
    assert rejected[("r6", "r8")]["evidence"]["tenant"] == {"r6": "A", "r8": "B"}
    assert rejected[("r6", "r8")]["shared_kinds"] == ["phone"]
    assert "cannot_link" not in rejected[("r6", "r8")]["reasons"]
    # r7/r8 share every token but differ in tenant; they are never one group.
    assert rejected[("r7", "r8")]["reasons"] == ["tenant_mismatch"]
    for group in report["groups"]:
        assert len({row["tenant"] for row in fixture_leads() if row["lead_id"] in group["members"]}) == 1


def test_consent_veto_unknown_and_cross_tenant_unknown():
    leads = [
        # Same group: yes + unknown -> granted.
        {"lead_id": "a1", "tenant": "A", "email_token": "e1", "phone_token": "p1",
         "account_token": "x1", "consent": "yes", "sale_id": ""},
        {"lead_id": "a2", "tenant": "A", "email_token": "e1", "phone_token": "p2",
         "account_token": "x2", "consent": "unknown", "sale_id": "S1"},
        # Same group: unknown + unknown -> unknown, not billable.
        {"lead_id": "a3", "tenant": "A", "email_token": "e3", "phone_token": "p3",
         "account_token": "x3", "consent": "unknown", "sale_id": "S2"},
        {"lead_id": "a4", "tenant": "A", "email_token": "e4", "phone_token": "p3",
         "account_token": "x4", "consent": "unknown", "sale_id": ""},
        # Same group: yes + no -> vetoed for every sale in the group.
        {"lead_id": "a5", "tenant": "A", "email_token": "e5", "phone_token": "p5",
         "account_token": "x5", "consent": "yes", "sale_id": "S3"},
        {"lead_id": "a6", "tenant": "A", "email_token": "e6", "phone_token": "p6",
         "account_token": "x5", "consent": "no", "sale_id": ""},
        # Cross-tenant: A's lone unknown must not borrow B's yes consent.
        {"lead_id": "a7", "tenant": "A", "email_token": "e7", "phone_token": "p7",
         "account_token": "x7", "consent": "unknown", "sale_id": "S4"},
        {"lead_id": "b1", "tenant": "B", "email_token": "e7", "phone_token": "p7",
         "account_token": "x7", "consent": "yes", "sale_id": ""},
    ]
    report = partition_lead_identities(build_intake(leads, cannot_link=[]), "person_identity")
    sales = {sale["sale_id"]: sale for sale in report["sales"]}

    assert group_members(report) == [["a1", "a2"], ["a3", "a4"], ["a5", "a6"], ["a7"], ["b1"]]
    assert sales["S1"]["billable"] is True
    assert sales["S1"]["consent"] == "granted"
    assert sales["S2"]["billable"] is False
    assert sales["S2"]["consent"] == "unknown"
    assert "no affirmative consent" in sales["S2"]["reason"]
    assert sales["S3"]["billable"] is False
    assert sales["S3"]["consent"] == "denied"
    assert "vetoed" in sales["S3"]["reason"]
    assert sales["S4"]["billable"] is False
    assert sales["S4"]["group_id"] == "a7"
    assert sales["S4"]["members"] == ["a7"]
    assert {sale["sale_id"]: sale for sale in report["sales"]}["S4"]["billable"] is False


def test_unknown_cannot_be_authorized_by_linking_other_tenant():
    leads = [
        {"lead_id": "u1", "tenant": "A", "email_token": "e1", "phone_token": "p1",
         "account_token": "x1", "consent": "unknown", "sale_id": "S9"},
        {"lead_id": "v1", "tenant": "B", "email_token": "e1", "phone_token": "p1",
         "account_token": "x1", "consent": "yes", "sale_id": ""},
    ]
    report = partition_lead_identities(build_intake(leads, cannot_link=[]), "person_identity")
    assert group_members(report) == [["u1"], ["v1"]]
    assert report["sales"][0]["billable"] is False
    assert report["rejected_links"][0]["reason"] == "tenant_mismatch"


# --------------------------------------------------------------------------
# Validation
# --------------------------------------------------------------------------


def test_search_tie_breaks_on_sorted_group_ids_and_is_reorder_stable():
    leads = [
        {"lead_id": "r1", "tenant": "A", "email_token": "e1", "phone_token": "p1",
         "account_token": "a1", "consent": "yes", "sale_id": ""},
        {"lead_id": "r2", "tenant": "A", "email_token": "e1", "phone_token": "p2",
         "account_token": "a2", "consent": "yes", "sale_id": "S1"},
        {"lead_id": "r3", "tenant": "A", "email_token": "e2", "phone_token": "p2",
         "account_token": "a3", "consent": "yes", "sale_id": ""},
    ]
    contracts = {
        "tie": {
            "email_evidence": 5,
            "phone_evidence": 5,
            "account_evidence": 0,
            "unit": "individual",
        }
    }
    intake = build_intake(leads, cannot_link=[["r1", "r3"]], contracts=contracts)
    report = partition_lead_identities(intake, "tie")
    # {r1,r2}+{r3} (group IDs r1,r3) and {r1}+{r2,r3} (group IDs r1,r2) both
    # score 5; the lexicographically smaller group-ID list wins.
    assert report["total_score"] == 5
    assert report["optimality"]["optimal_partitions"] == 2
    assert report["optimality"]["unique_optimum"] is False
    assert group_members(report) == [["r1"], ["r2", "r3"]]

    reordered = build_intake(list(reversed(leads)), cannot_link=[["r1", "r3"]],
                             contracts=contracts, run_id="other-run",
                             body_sha256="1" * 64)
    again = partition_lead_identities(reordered, "tie")
    assert group_members(again) == group_members(report)
    assert again["total_score"] == report["total_score"]
    assert [sale["sale_id"] for sale in again["sales"]] == ["S1"]


def test_input_reorder_does_not_change_fixture_result():
    intake = build_intake(fixture_leads())
    shuffled = build_intake(list(reversed(fixture_leads())))
    for contract in CONTRACTS:
        left = partition_lead_identities(intake, contract)
        right = partition_lead_identities(shuffled, contract)
        assert group_members(left) == group_members(right)
        assert left["total_score"] == right["total_score"]
        assert left["sales"] == right["sales"]


def test_duplicate_lead_and_sale_ids_are_rejected():
    leads = fixture_leads()
    duplicate_lead = build_intake(leads)
    duplicate_lead["leads"][1]["lead_id"] = "r1"
    with pytest.raises(ValueError, match="lead IDs must be unique"):
        partition_lead_identities(duplicate_lead, "person_identity")

    duplicate_sale = build_intake(leads)
    duplicate_sale["leads"][2]["sale_id"] = "S2"
    with pytest.raises(ValueError, match="sale IDs must be unique"):
        partition_lead_identities(duplicate_sale, "person_identity")


@pytest.mark.parametrize(
    "mutate, message",
    [
        (lambda leads: leads[0].update(tenant=""), "tenant must be a non-empty string"),
        (lambda leads: leads[0].update(email_token=""), "email_token must be a non-empty string"),
        (lambda leads: leads[0].update(phone_token="   "), "phone_token must be a non-empty string"),
        (lambda leads: leads[0].update(account_token=""), "account_token must be a non-empty string"),
        (lambda leads: leads[0].update(consent="maybe"), "consent must be one of yes/no/unknown"),
        (lambda leads: leads[0].update(consent=None), "consent must be one of yes/no/unknown"),
    ],
)
def test_invalid_lead_fields_are_rejected(mutate, message):
    leads = fixture_leads()
    mutate(leads)
    with pytest.raises(ValueError, match=message):
        partition_lead_identities(build_intake(leads), "person_identity")


@pytest.mark.parametrize(
    "cannot_link, message",
    [
        ([["r1", "r1"]], "must not self-reference"),
        ([["r1", "missing"]], "references an unknown lead"),
        ([["r1"]], "must contain two lead IDs"),
        ([["r1", 3]], "lead IDs must be strings"),
    ],
)
def test_invalid_cannot_link_is_rejected(cannot_link, message):
    with pytest.raises(ValueError, match=message):
        partition_lead_identities(
            build_intake(fixture_leads(), cannot_link=cannot_link), "person_identity"
        )


@pytest.mark.parametrize(
    "kinds, message",
    [
        (lambda c: c["person_identity"].update(email_evidence=-1),
         "email_evidence must be a non-negative integer"),
        (lambda c: c["person_identity"].update(phone_evidence=1.5),
         "phone_evidence must be a non-negative integer"),
        (lambda c: c["person_identity"].update(account_evidence=True),
         "account_evidence must be a non-negative integer"),
        (lambda c: c["person_identity"].update(unit=""), "unit must be a non-empty string"),
    ],
)
def test_invalid_weights_are_rejected(kinds, message):
    contracts = copy.deepcopy(CONTRACTS)
    kinds(contracts)
    with pytest.raises(ValueError, match=message):
        partition_lead_identities(
            build_intake(fixture_leads(), contracts=contracts), "person_identity"
        )


def test_intake_must_be_bound_and_bounded():
    intake = build_intake(fixture_leads())
    intake["run_id"] = ""
    with pytest.raises(ValueError, match="body_sha256|run_id"):
        partition_lead_identities(intake, "person_identity")

    unbound = build_intake(fixture_leads())
    unbound["body_sha256"] = ""
    with pytest.raises(ValueError, match="body_sha256"):
        partition_lead_identities(unbound, "person_identity")

    too_many = fixture_leads() + [
        {
            "lead_id": "r9",
            "tenant": "C",
            "email_token": "e9",
            "phone_token": "p9",
            "account_token": "a9",
            "consent": "yes",
            "sale_id": "",
        }
    ]
    with pytest.raises(ValueError, match="at most 8"):
        partition_lead_identities(build_intake(too_many), "person_identity")


# --------------------------------------------------------------------------
# Receipt verification: bad SHA and failed GET
# --------------------------------------------------------------------------


def test_modified_body_rejected(tmp_path):
    db, _page = page_run(tmp_path, (FIX / "mock-leads_forms.html").read_bytes())
    changed = tmp_path / "changed.html"
    changed.write_bytes((FIX / "mock-leads_forms.html").read_bytes() + b" ")
    with Storage(db) as storage:
        with pytest.raises(ValueError, match="matching successful HTML"):
            verify_lead_intake(storage, "lead-run", changed, FIX / "intake.json")


def test_failed_get_cannot_generate_attribution(tmp_path):
    pages = tmp_path / "pages"
    pages.mkdir()
    (pages / "forms.html").write_bytes((FIX / "mock-leads_forms.html").read_bytes())
    manifest = pages / "manifest.yaml"
    manifest.write_text(
        "mock-leads:\n  forms.html:\n    status: 500\n    file: forms.html\n",
        encoding="utf-8",
    )
    db = tmp_path / "leads.sqlite3"
    result = run_demo(
        FIX / "site.yaml",
        project_root=ROOT,
        db_path=db,
        reports_dir=tmp_path / "reports",
        pages_dir=pages,
        manifest_path=manifest,
        run_id="failed-get",
    )
    assert result.executor_stats["attempts"] >= 1
    with Storage(db) as storage:
        events = storage.get_request_events("failed-get")
        assert events and all(event["outcome"] != "success" for event in events)
        with pytest.raises(ValueError, match="matching successful HTML"):
            verify_lead_intake(
                storage, "failed-get", pages / "forms.html", FIX / "intake.json"
            )


def test_unknown_run_id_cannot_generate_attribution(tmp_path):
    db, page = page_run(tmp_path, (FIX / "mock-leads_forms.html").read_bytes())
    with Storage(db) as storage:
        with pytest.raises(ValueError, match="matching successful HTML"):
            verify_lead_intake(storage, "not-run", page, FIX / "intake.json")


# --------------------------------------------------------------------------
# Captured body / extracted token / consent / sale changes drive the result
# --------------------------------------------------------------------------


def test_token_and_consent_change_alters_partition_and_sale_conclusions(tmp_path):
    baseline = verified_report(tmp_path / "baseline", (FIX / "mock-leads_forms.html").read_bytes())
    assert baseline["contracts"]["person_identity"]["total_score"] == 7
    assert baseline["contracts"]["household_identity"]["total_score"] == 18
    assert baseline["contracts"]["person_identity"]["sales_summary"]["billable"] == ["S2", "S7"]
    assert baseline["contracts"]["household_identity"]["sales_summary"]["billable"] == ["S7"]

    html = (FIX / "mock-leads_forms.html").read_text(encoding="utf-8")
    # Extracted token change: r5 no longer shares r4's phone.
    html = replace_article(html, "r5", phone_token="p9")
    # Extracted consent change: r2 no longer merely unknown.
    html = replace_article(html, "r2", consent="no")

    changed = verified_report(tmp_path / "changed", html.encode("utf-8"), run_id="changed-run")
    changed_person = changed["contracts"]["person_identity"]
    changed_household = changed["contracts"]["household_identity"]

    assert changed["run_id"] == "changed-run"
    assert changed_person["total_score"] == 6
    assert changed_household["total_score"] == 13
    assert group_members(changed_person) != PERSON_GROUPS
    # r4 and r5 no longer share evidence, so they can never be one group.
    assert not any({"r4", "r5"} <= set(group) for group in group_members(changed_person))
    assert not any({"r4", "r5"} <= set(group) for group in group_members(changed_household))
    assert group_members(changed_household) == [
        ["r1"], ["r2", "r3"], ["r4"], ["r5", "r6"], ["r7"], ["r8"]
    ]

    assert changed_person["sales_summary"]["billable"] == ["S7"]
    assert changed_person["sales_summary"]["not_billable"] == ["S2"]
    # Under the household contract S7 used to be billable through r6; with the
    # changed consent r7 is isolated and no longer authorized.
    assert changed_household["sales_summary"]["billable"] == []
    assert changed_household["sales_summary"]["not_billable"] == ["S2", "S7"]

    # The source binding is not the only thing that changed.
    assert changed_person["body_sha256"] != baseline["contracts"]["person_identity"]["body_sha256"]
    assert changed_person["groups"] != baseline["contracts"]["person_identity"]["groups"]


def test_sale_id_change_alters_billability(tmp_path):
    baseline = verified_report(tmp_path / "baseline", (FIX / "mock-leads_forms.html").read_bytes())
    assert baseline["contracts"]["household_identity"]["sales_summary"]["billable"] == ["S7"]
    assert baseline["contracts"]["household_identity"]["sales_summary"]["not_billable"] == ["S2"]

    html = (FIX / "mock-leads_forms.html").read_text(encoding="utf-8")
    html = replace_article(html, "r2", sale_id="")
    html = replace_article(html, "r1", sale_id="S2")
    html = replace_article(html, "r7", sale_id="")
    html = replace_article(html, "r8", sale_id="S7")

    changed = verified_report(tmp_path / "changed", html.encode("utf-8"), run_id="sale-run")
    assert group_members(changed["contracts"]["household_identity"]) == HOUSEHOLD_GROUPS
    person_sales = {sale["sale_id"]: sale for sale in changed["contracts"]["person_identity"]["sales"]}
    household_sales = {
        sale["sale_id"]: sale for sale in changed["contracts"]["household_identity"]["sales"]
    }
    # S2 moved from r2 to r1: under household it is now a consented singleton.
    assert household_sales["S2"]["billable"] is True
    assert household_sales["S2"]["group_id"] == "r1"
    # S7 moved onto tenant-B r8, whose "no" vetoes it under both contracts.
    assert person_sales["S7"]["billable"] is False
    assert person_sales["S7"]["group_id"] == "r8"
    assert household_sales["S7"]["billable"] is False


def test_body_receipt_binds_each_report_to_run_and_hash(tmp_path):
    db, page = page_run(tmp_path, (FIX / "mock-leads_forms.html").read_bytes())
    with Storage(db) as storage:
        intake = verify_lead_intake(storage, "lead-run", page, FIX / "intake.json")
        report = analyze_lead_identities(intake)
    body_hash = intake["body_sha256"]
    source = json.loads((FIX / "intake.json").read_text(encoding="utf-8"))
    assert source["source_url"].startswith("http://127.0.0.1:")
    assert len(body_hash) == 64
    assert body_hash == sha256_bytes(page.read_bytes())
    for contract in report["contracts"].values():
        assert contract["body_sha256"] == body_hash
        assert contract["run_id"] == "lead-run"
        for group in contract["groups"]:
            assert group["run_id"] == "lead-run"
            assert group["body_sha256"] == body_hash
        for sale in contract["sales"]:
            assert sale["run_id"] == "lead-run"
            assert sale["body_sha256"] == body_hash
            assert sale["reason"]
