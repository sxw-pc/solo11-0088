import json
from pathlib import Path

import pytest

from crawlbudget.appointment_evidence import verify_appointment_evidence
from crawlbudget.pipeline import run_demo
from crawlbudget.storage import Storage


ROOT = Path(__file__).resolve().parents[1]
FIX = ROOT / "fixtures" / "appointments"


def test_executed_page_provides_bound_appointment_evidence(tmp_path):
    db = tmp_path / "appointments.sqlite3"
    result = run_demo(
        FIX / "site.yaml",
        project_root=ROOT,
        db_path=db,
        reports_dir=tmp_path / "reports",
        pages_dir=FIX,
        manifest_path=FIX / "manifest.yaml",
        run_id="appointment-run",
    )
    assert result.status == "completed"
    with Storage(db) as storage:
        evidence = verify_appointment_evidence(
            storage, "appointment-run", FIX / "mock-appointments_slots.html", FIX / "contract.json"
        )
        assert evidence["provider_id"] == "provider-p1"
        assert evidence["cancel_notice"]["occurrence_index"] == 3
        assert evidence["cadences"] == ["same_local_hour", "fixed_168h"]
        assert evidence == verify_appointment_evidence(
            storage, "appointment-run", FIX / "mock-appointments_slots.html", FIX / "contract.json"
        )
        bad_body = tmp_path / "changed.html"
        bad_body.write_bytes((FIX / "mock-appointments_slots.html").read_bytes() + b"\n")
        with pytest.raises(ValueError, match="matching HTML receipt"):
            verify_appointment_evidence(storage, "appointment-run", bad_body, FIX / "contract.json")
        bad_contract = tmp_path / "changed.json"
        contract = json.loads((FIX / "contract.json").read_text(encoding="utf-8"))
        contract["provider_id"] = "provider-other"
        bad_contract.write_text(json.dumps(contract), encoding="utf-8")
        with pytest.raises(ValueError, match="disagree on provider_id"):
            verify_appointment_evidence(
                storage, "appointment-run", FIX / "mock-appointments_slots.html", bad_contract
            )
