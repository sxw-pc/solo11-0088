import dataclasses
import shutil

import pytest

from crawlbudget.billing_inputs import BillingError
from crawlbudget.billing_verify import verify_receipts
from crawlbudget.subscriber_billing import build_report
from tests.billing_helpers import (
    ROOT,
    context,
    data,
    receipts,
    replace_receipt,
    report,
)


def _rejected(changes, subscriber_id="subscriber-A"):
    config, manifest, parsed, urls = context()
    items = replace_receipt(subscriber_id=subscriber_id, **changes)
    return verify_receipts(parsed, config, items,
                           project_root=ROOT, manifest=manifest, urls=urls)


def test_receipt_identity_and_verification_are_recorded():
    config, manifest, parsed, urls = context()
    result = verify_receipts(parsed, config, receipts(),
                             project_root=ROOT, manifest=manifest, urls=urls)
    assert result["all_verified"] is True
    assert set(result["accepted"]) == {"subscriber-A", "subscriber-B", "subscriber-C"}
    identities = report()["receipts"]["identities"]
    assert {item["subscriber_id"] for item in identities} == set(urls)
    for item in identities:
        assert item["outcome"] == "success" and item["status_code"] == 200
        assert len(item["body_sha256"]) == 64 and item["lease_id"]


@pytest.mark.parametrize("changes,code", [
    ({"outcome": "http_error", "status_code": 500}, "not_success"),
    ({"response_bytes": 4097}, "receipt_bytes_mismatch"),
    ({"body_sha256": "0" * 64}, "receipt_sha_mismatch"),
    ({"planned_allowed": False, "block_reason": "quota_exceeded"}, "not_authorized"),
    ({"robots_decision": "disallow"}, "robots_not_allowed"),
    ({"lease_granted": 0, "lease_used": 0}, "lease_not_granted"),
])
def test_failed_oversized_sha_or_unauthorized_receipts_are_rejected(changes, code):
    result = _rejected(changes)
    assert result["all_verified"] is False
    assert any(issue["code"] == code
               for rejection in result["rejected"]
               for issue in rejection["evidence"].get("issues", []))


def test_receipt_attestation_drives_invoices_or_refusal():
    assert report()["refused"] is False
    bad = replace_receipt(subscriber_id="subscriber-B", body_sha256="f" * 64)
    refused = build_report(data(), project_root=ROOT, receipts=bad)
    assert refused["refused"] is True
    assert refused["settlement"]["contracts"] == {}
    assert refused["refusal_reasons"][0]["code"] == "unverified_delivery"
    assert refused["refusal_reasons"][0]["rejected"]


def test_missing_receipt_blocks_that_subscriber():
    items = [r for r in receipts() if r.subscriber_id != "subscriber-C"]
    result = build_report(data(), project_root=ROOT, receipts=items)
    assert result["refused"] is True
    assert any(item["code"] == "billing_receipts_missing"
               and item["subscriber_id"] == "subscriber-C"
               for item in result["receipts"]["rejected"])


def test_unconfigured_site_is_rejected():
    intake = data()
    intake["subscribers"][0]["site_id"] = "mock-delta"
    with pytest.raises(BillingError) as excinfo:
        build_report(intake, project_root=ROOT, receipts=receipts())
    assert excinfo.value.code == "billing_unknown_site"


def test_changed_local_fixture_bytes_invalidate_receipt(tmp_path):
    tree = tmp_path / "root"
    tree.mkdir()
    shutil.copytree(ROOT / "fixtures", tree / "fixtures")
    page = tree / "fixtures/pages/mock-beta_page_public_a.html"
    page.write_bytes(page.read_bytes() + b"x")
    result = build_report(
        data(), project_root=tree,
        config_path=tree / "fixtures/demo_sites.yaml",
        manifest_path=tree / "fixtures/pages/manifest.yaml",
        receipts=receipts(),
    )
    assert result["refused"] is True
    codes = {issue["code"]
             for rejection in result["receipts"]["rejected"]
             for issue in rejection["evidence"].get("issues", [])}
    assert {"fixture_bytes_mismatch", "fixture_sha_mismatch"} & codes


def test_robots_missing_denies_the_page_and_refuses(tmp_path):
    tree = tmp_path / "root"
    tree.mkdir()
    shutil.copytree(ROOT / "fixtures", tree / "fixtures")
    (tree / "fixtures/robots/mock-beta.txt").unlink()
    result = build_report(
        data(), project_root=tree,
        config_path=tree / "fixtures/demo_sites.yaml",
        manifest_path=tree / "fixtures/pages/manifest.yaml",
        receipts=receipts(),
    )
    assert result["refused"] is True
    codes = {issue["code"]
             for rejection in result["receipts"]["rejected"]
             for issue in rejection["evidence"].get("issues", [])}
    assert "robots_not_allowed" in codes


def test_duplicate_and_unmatched_receipts_are_rejected():
    duplicated = receipts() + [receipts()[0]]
    result = build_report(data(), project_root=ROOT, receipts=duplicated)
    assert result["refused"] is True
    codes = {item["code"] for item in result["receipts"]["rejected"]}
    assert {"billing_duplicate_receipt", "billing_unmatched_receipt"} <= codes

    stray = dataclasses.replace(
        receipts()[0], url="http://127.0.0.1:5021/mock-alpha/docs/page-2.html")
    result = build_report(data(), project_root=ROOT,
                          receipts=receipts() + [stray])
    assert result["refused"] is True
    assert any(item["code"] == "billing_unmatched_receipt"
               for item in result["receipts"]["rejected"])


def test_executor_ledger_is_a_valid_receipt_source(tmp_path):
    from crawlbudget.config import load_config
    from crawlbudget.pipeline import run_pipeline
    from crawlbudget.storage import Storage
    from tests.fakes import demo_replay_session

    config = load_config(ROOT / "fixtures/demo_sites.yaml", project_root=ROOT)
    with Storage(tmp_path / "ledger.sqlite3") as storage:
        run_pipeline(config, storage, "billing-ledger",
                     reports_dir=tmp_path / "reports",
                     session=demo_replay_session(), sleeper=lambda delay: None)
        result = build_report(
            {**data(), "run_id": "billing-ledger"}, project_root=ROOT,
            config_path=ROOT / "fixtures/demo_sites.yaml",
            manifest_path=ROOT / "fixtures/pages/manifest.yaml",
            storage=storage,
        )
    assert result["receipts"]["all_verified"] is True
    assert result["refused"] is False
    assert result["full_plan"]["total_cost"] == 37
    assert result["settlement"]["contracts"]["byte_share"]["total_cents"] == 3700
