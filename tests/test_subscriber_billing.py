import copy
import hashlib
import json

import pytest
import yaml

from crawlbudget.billing_contracts import _allocate_cents, settle, solo_prices
from crawlbudget.billing_inputs import BillingError, parse_inputs
from crawlbudget.billing_model import CoalitionTable, coalition_costs, optimal_plan
from crawlbudget.subscriber_billing import main, solo_quotes
from tests.billing_helpers import (
    INTAKE,
    RECEIPTS,
    ROOT,
    data,
    receipts,
    report,
    verified_weights,
)

_data = data
_receipts = receipts
_report = report


# --- Fixture truth -------------------------------------------------------

def test_subscriber_pages_and_sites_are_real_local_fixtures():
    intake = _data()
    config = yaml.safe_load((ROOT / "fixtures/demo_sites.yaml").read_text())
    assert {s["site_id"] for s in intake["subscribers"]} == {
        s["site_id"] for s in config["sites"]
    }
    for subscriber in intake["subscribers"]:
        body = (ROOT / subscriber["page_path"]).read_bytes()
        assert len(body) == subscriber["body_bytes"]
        assert hashlib.sha256(body).hexdigest() == subscriber["body_sha256"]


def test_individual_quotes_exceed_feasible_shared_sessions():
    intake = _data()
    assert solo_quotes(intake) == {"subscriber-A": 20, "subscriber-B": 18,
                                   "subscriber-C": 16}
    assert sum(solo_quotes(intake).values()) == 54
    dist = intake["navigation_cost"]
    assert (dist["D"]["A"] + dist["A"]["B"] + dist["B"]["D"]
            + dist["D"]["C"] + dist["C"]["D"]) == 37


# --- Exact coalition costs and routing ----------------------------------

def test_all_subset_costs_are_exact_and_resorted():
    assert coalition_costs(_data()) == {
        frozenset(): 0,
        frozenset({"subscriber-A"}): 20,
        frozenset({"subscriber-B"}): 18,
        frozenset({"subscriber-C"}): 16,
        frozenset({"subscriber-A", "subscriber-B"}): 21,
        frozenset({"subscriber-A", "subscriber-C"}): 23,
        frozenset({"subscriber-B", "subscriber-C"}): 23,
        frozenset({"subscriber-A", "subscriber-B", "subscriber-C"}): 37,
    }


def test_full_plan_uses_capacity_two_sessions_and_directed_edges():
    plan = optimal_plan(_data())
    assert plan["total_cost"] == 37
    assert plan["session_capacity"] == 2
    assert [(s["subscriber_ids"], s["route"], s["navigation_cost"])
            for s in plan["sessions"]] == [
        (["subscriber-A", "subscriber-B"], ["D", "A", "B", "D"], 21),
        (["subscriber-C"], ["D", "C", "D"], 16),
    ]
    for session in plan["sessions"]:
        assert session["served_count"] <= plan["session_capacity"]
        assert sum(leg["cost"] for leg in session["legs"]) == session["navigation_cost"]
        assert session["route"][0] == session["route"][-1] == "D"


def test_subset_optimal_is_not_a_deletion_of_the_full_route():
    # Capacity 1 forces solo trips; deleting a stop from the shared route would
    # understate each pair's true cost.
    costs = coalition_costs(_data(), capacity=1)
    assert costs[frozenset({"subscriber-A", "subscriber-B"})] == 38
    assert costs[frozenset({"subscriber-A", "subscriber-B", "subscriber-C"})] == 54
    pair_plan = optimal_plan(_data(), ["subscriber-A", "subscriber-B"], capacity=1)
    assert pair_plan["total_cost"] == 38
    assert len(pair_plan["sessions"]) == 2


def test_capacity_one_three_forces_solo_trips():
    plan = optimal_plan(_data(), capacity=1)
    assert plan["total_cost"] == 54
    assert len(plan["sessions"]) == 3
    assert all(s["served_count"] == 1 for s in plan["sessions"])


def test_capacity_three_serves_everyone_in_one_cheaper_session():
    # Capacity 3 allows one tour; exhaustive search finds D->B->A->C->D = 24,
    # which is cheaper than the capacity-2 plan of 37.
    plan = optimal_plan(_data(), capacity=3)
    assert plan["total_cost"] == 24
    assert len(plan["sessions"]) == 1
    session = plan["sessions"][0]
    assert session["served_count"] == 3
    assert session["route"] == ["D", "B", "A", "C", "D"]
    assert session["navigation_cost"] == 24


def test_directed_edges_change_route_cost():
    intake = copy.deepcopy(_data())
    intake["navigation_cost"]["A"]["B"] = 100
    intake["navigation_cost"]["B"]["A"] = 1
    plan = optimal_plan(intake, ["subscriber-A", "subscriber-B"])
    assert plan["sessions"][0]["stops"] == ["B", "A"]
    assert plan["total_cost"] == 9 + 1 + 10


def test_input_reordering_does_not_change_any_cost_or_invoice():
    baseline = _report()
    intake = _data()
    intake["subscribers"] = list(reversed(intake["subscribers"]))
    intake["navigation_cost"] = dict(reversed(list(intake["navigation_cost"].items())))
    reordered = _report(intake)
    assert reordered["full_plan"] == baseline["full_plan"]
    assert reordered["subset_costs"] == baseline["subset_costs"]
    for name in ("byte_share", "marginal_share"):
        assert (reordered["settlement"]["contracts"][name]["invoices"]
                == baseline["settlement"]["contracts"][name]["invoices"])


# --- Contracts ----------------------------------------------------------

def test_byte_share_splits_37_by_511_262_216():
    contract = _report()["settlement"]["contracts"]["byte_share"]
    assert contract["signed"] is True and contract["status"] == "signed"
    assert {row["subscriber_id"]: row["amount"] for row in contract["invoices"]} == {
        "subscriber-A": "19.12", "subscriber-B": "9.80", "subscriber-C": "8.08"}
    assert contract["total_cents"] == 3700
    assert contract["evidence"]["weights"] == {
        "subscriber-A": 511, "subscriber-B": 262, "subscriber-C": 216}


def test_marginal_share_replans_each_coalition_to_13_12_12():
    contract = _report()["settlement"]["contracts"]["marginal_share"]
    assert contract["signed"] is True
    assert {row["subscriber_id"]: row["cents"] for row in contract["invoices"]} == {
        "subscriber-A": 1300, "subscriber-B": 1200, "subscriber-C": 1200}
    assert contract["evidence"]["permutations"] == 6
    assert contract["evidence"]["subset_costs_used"]["subscriber-A+subscriber-B"] == 21


def test_marginal_share_is_not_the_leave_one_out_shortcut():
    parsed = parse_inputs(_data())
    table = CoalitionTable(parsed)
    shortcut = {
        subscriber_id: table.cost(table.full)
        - table.cost(table.full ^ table.mask_for([subscriber_id]))
        for subscriber_id in table.ids
    }
    assert shortcut == {"subscriber-A": 14, "subscriber-B": 14, "subscriber-C": 16}
    invoices = {row["subscriber_id"]: row["cents"] // 100 for row in
                _report()["settlement"]["contracts"]["marginal_share"]["invoices"]}
    assert invoices == {"subscriber-A": 13, "subscriber-B": 12, "subscriber-C": 12}
    assert invoices != shortcut


def test_naive_equal_split_would_break_c_solo_upper_bound():
    # Splitting the 54 solo draft three ways charges C 18 > 16.  The 37/3 split
    # is under the bound but is not either signed contract.
    assert 54 * 100 // 3 > 16 * 100
    parsed = parse_inputs(_data())
    table = CoalitionTable(parsed)
    equal_shared_split = table.cost(table.full) * 100 // 3
    contract = settle(table, verified_weights(parsed))["contracts"]["byte_share"]
    c = next(row for row in contract["invoices"]
             if row["subscriber_id"] == "subscriber-C")
    assert c["cents"] <= 16 * 100
    assert c["cents"] != equal_shared_split


def test_both_contracts_conserve_full_cost_and_respect_solo_prices():
    parsed = parse_inputs(_data())
    solo = solo_prices(CoalitionTable(parsed))
    for name, contract in _report()["settlement"]["contracts"].items():
        assert contract["signed"] is True
        assert sum(row["cents"] for row in contract["invoices"]) == 3700
        assert contract["total_cents"] == contract["full_cost_cents"] == 3700
        assert contract["conserved"] is True
        for row in contract["invoices"]:
            assert 0 <= row["cents"] <= solo[row["subscriber_id"]] * 100


def test_cents_are_floored_then_largest_remainder_ties_by_id():
    from fractions import Fraction

    rows = _allocate_cents(
        {"subscriber-B": Fraction(1, 2), "subscriber-A": Fraction(1, 2),
         "subscriber-C": Fraction(0)}, 1)
    assert {row["subscriber_id"]: row["cents"] for row in rows} == {
        "subscriber-A": 1, "subscriber-B": 0, "subscriber-C": 0}
    rows = _allocate_cents(
        {"subscriber-A": Fraction(1, 10), "subscriber-B": Fraction(9, 10)}, 1)
    assert {row["subscriber_id"]: row["cents"] for row in rows} == {
        "subscriber-A": 0, "subscriber-B": 1}
    contract = _report()["settlement"]["contracts"]["byte_share"]
    assert sum(row["cents"] for row in contract["invoices"]) == 3700


def test_contract_refuses_when_rule_breaks_solo_upper_bound():
    # A fabricated zero-cost route makes C's solo price 0, so any positive
    # invoice exceeds it.  The contract must refuse, not trim a bill.
    intake = _data()
    intake["navigation_cost"]["D"]["C"] = 0
    intake["navigation_cost"]["C"]["D"] = 0
    parsed = parse_inputs(intake)
    settlement = settle(CoalitionTable(parsed), verified_weights(parsed))
    assert settlement["all_signed"] is False
    contract = settlement["contracts"]["byte_share"]
    assert contract["signed"] is False and contract["status"] == "refused"
    assert contract["reason"] == "solo_price_conflict"
    assert contract["invoices"] is None
    assert any(item["code"] == "solo_price_exceeded"
               for item in contract["evidence"]["violations"])


def test_negative_costs_duplicates_and_bounds_are_rejected():
    negative = _data()
    negative["navigation_cost"]["A"]["B"] = -2
    with pytest.raises(BillingError) as excinfo:
        parse_inputs(negative)
    assert excinfo.value.code == "billing_negative_cost"

    duplicate = _data()
    duplicate["subscribers"].append(copy.deepcopy(duplicate["subscribers"][0]))
    with pytest.raises(BillingError) as excinfo:
        parse_inputs(duplicate)
    assert excinfo.value.code == "billing_duplicate_subscriber"

    too_many = _data()
    too_many["subscribers"] = too_many["subscribers"] * 3
    with pytest.raises(BillingError) as excinfo:
        parse_inputs(too_many)
    assert excinfo.value.code == "billing_too_many_subscribers"

    over_capacity = _data()
    over_capacity["session_capacity"] = 4
    with pytest.raises(BillingError) as excinfo:
        parse_inputs(over_capacity)
    assert excinfo.value.code == "billing_capacity_exceeded"


# --- CLI ----------------------------------------------------------------

def test_cli_emits_shared_plan_and_both_invoices(capsys):
    assert main(["--fixture", str(INTAKE), "--project-root", str(ROOT)]) == 0
    out = capsys.readouterr().out
    assert "solo_quotes_total=54" in out
    assert "shared_full_cost=37" in out
    assert "D->A->B->D" in out
    assert "subscriber-A 19.12" in out
    assert "subscriber-A 13.00" in out


def test_cli_refuses_with_evidence_and_nonzero_exit(tmp_path, capsys):
    bad = json.loads(RECEIPTS.read_text(encoding="utf-8"))
    bad["receipts"][0]["outcome"] = "http_error"
    target = tmp_path / "receipts.json"
    target.write_text(json.dumps(bad), encoding="utf-8")
    code = main(["--fixture", str(INTAKE), "--receipts", str(target),
                 "--project-root", str(ROOT)])
    captured = capsys.readouterr()
    assert code == 2
    assert "REFUSED" in captured.err
    assert "unverified_delivery" in captured.err
