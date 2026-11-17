from pathlib import Path
import pytest
from crawlbudget.landing_rights_intake import verify_landing_intake,single_right_preview
from crawlbudget.pipeline import run_demo
from crawlbudget.storage import Storage

ROOT=Path(__file__).resolve().parents[1]
FIX=ROOT/'fixtures/landing_rights'


def test_real_capture_yields_creative_rights_preview(tmp_path):
    db=tmp_path/'landing.sqlite3'
    result=run_demo(FIX/'site.yaml',project_root=ROOT,db_path=db,
                    reports_dir=tmp_path/'reports',pages_dir=FIX,
                    manifest_path=FIX/'manifest.yaml',run_id='landing-run')
    assert result.status=='completed'
    with Storage(db) as storage:
        intake=verify_landing_intake(storage,'landing-run',
                                     FIX/'mock-landing_landing.html',FIX/'intake.json')
    assert intake['site_id']=='mock-landing'
    assert len(intake['creatives'])==6
    assert len(intake['possible_worlds'])==7
    assert sum(w['prior_weight'] for w in intake['possible_worlds'])==57
    assert single_right_preview(intake)=={'first_audit':'A','mention_value':15,
                                           'adaptive_policy_computed':False}


def test_changed_body_or_failed_receipt_cannot_supply_creatives(tmp_path):
    db=tmp_path/'landing.sqlite3'
    run_demo(FIX/'site.yaml',project_root=ROOT,db_path=db,
             reports_dir=tmp_path/'reports',pages_dir=FIX,
             manifest_path=FIX/'manifest.yaml',run_id='landing-run')
    changed=tmp_path/'changed.html'
    changed.write_bytes((FIX/'mock-landing_landing.html').read_bytes()+b' ')
    with Storage(db) as storage:
        with pytest.raises(ValueError,match='matching successful HTML'):
            verify_landing_intake(storage,'landing-run',changed,FIX/'intake.json')
