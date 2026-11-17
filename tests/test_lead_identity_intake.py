from pathlib import Path
import pytest
from crawlbudget.lead_identity_intake import verify_lead_intake,preview_token_components
from crawlbudget.pipeline import run_demo
from crawlbudget.storage import Storage

ROOT=Path(__file__).resolve().parents[1]
FIX=ROOT/'fixtures/lead_identity'


def test_verified_forms_have_conflicting_token_component(tmp_path):
    db=tmp_path/'leads.sqlite3'
    result=run_demo(FIX/'site.yaml',project_root=ROOT,db_path=db,
                    reports_dir=tmp_path/'reports',pages_dir=FIX,
                    manifest_path=FIX/'manifest.yaml',run_id='lead-run')
    assert result.status=='completed'
    with Storage(db) as storage:
        intake=verify_lead_intake(storage,'lead-run',FIX/'mock-leads_forms.html',
                                  FIX/'intake.json')
    assert len(intake['leads'])==8
    assert intake['leads'][1]['sale_id']=='S2'
    assert intake['leads'][6]['sale_id']=='S7'
    assert preview_token_components(intake)==[['r1','r2','r3'],['r4','r5','r6','r7','r8']]
    assert intake['cannot_link'][0]==['r1','r3']


def test_modified_body_rejected(tmp_path):
    db=tmp_path/'leads.sqlite3'
    run_demo(FIX/'site.yaml',project_root=ROOT,db_path=db,
             reports_dir=tmp_path/'reports',pages_dir=FIX,
             manifest_path=FIX/'manifest.yaml',run_id='lead-run')
    changed=tmp_path/'changed.html'
    changed.write_bytes((FIX/'mock-leads_forms.html').read_bytes()+b' ')
    with Storage(db) as storage:
        with pytest.raises(ValueError,match='matching successful HTML'):
            verify_lead_intake(storage,'lead-run',changed,FIX/'intake.json')
