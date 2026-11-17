from pathlib import Path
import json
import pytest
from crawlbudget.config import load_config
from crawlbudget.pipeline import run_pipeline
from crawlbudget.quality_sampling import load_frame,draw_frame
from crawlbudget.document_completeness import inspect_documents
from crawlbudget.storage import Storage
from tests.fakes import FakeResponse,ReplaySession

ROOT=Path(__file__).resolve().parents[1]
INTAKE=ROOT/'fixtures/document_completeness/intake.json'
FRAME=ROOT/'fixtures/quality/frame.json'


def test_sampled_docs_use_real_successful_receipts(tmp_path):
    config=load_config(ROOT/'fixtures/quality_sampling.yaml',project_root=ROOT)
    frame=load_frame(FRAME,config.sites['lease-demo'])
    manifest=json.loads(INTAKE.read_text(encoding='utf-8'))
    assert set(draw_frame(frame))==set(manifest['body_paths'])
    routes={url:[FakeResponse(200,body=(ROOT/manifest['body_paths'][url]).read_bytes(),
                               headers={'Content-Type':'text/html'})]
            for url in draw_frame(frame)}
    with Storage(':memory:') as storage:
        run_pipeline(config,storage,'metadata-case',reports_dir=tmp_path,
                     session=ReplaySession(routes),sleeper=lambda _:None)
        result=inspect_documents(storage,'metadata-case',frame,INTAKE)
    assert result['frame_size']==6 and result['sample_size']==4
    assert sum(row['full_metadata'] for row in result['rows'])==3
    assert sum(row['public_index'] for row in result['rows'])==4
    assert result['population_certificate'] is None


def test_failed_receipt_cannot_be_reused_as_metadata(tmp_path):
    config=load_config(ROOT/'fixtures/quality_sampling.yaml',project_root=ROOT)
    frame=load_frame(FRAME,config.sites['lease-demo'])
    manifest=json.loads(INTAKE.read_text(encoding='utf-8'))
    routes={url:[FakeResponse(200,body=(ROOT/manifest['body_paths'][url]).read_bytes(),
                               headers={'Content-Type':'text/html'})]
            for url in draw_frame(frame)}
    bad=draw_frame(frame)[0]
    routes[bad]=[FakeResponse(200,body=b'changed',headers={'Content-Type':'text/html'})]
    with Storage(':memory:') as storage:
        run_pipeline(config,storage,'metadata-case',reports_dir=tmp_path,
                     session=ReplaySession(routes),sleeper=lambda _:None)
        with pytest.raises(ValueError,match='matching successful HTML'):
            inspect_documents(storage,'metadata-case',frame,INTAKE)
