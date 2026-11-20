"""Tests for offline representation attestation and the two consumer contracts.

Every scenario uses in-memory SQLite, local fixture bytes, and a fake HTTP
session. No socket is opened and no external host is referenced.
"""

import json
from pathlib import Path

import pytest

from crawlbudget.conditional_capture import capture_and_record, insert_capture_receipt
from crawlbudget.models import RequestEvent
from crawlbudget.representation import (
    BODY_FILE_MISSING,
    BODY_FILE_TAMPERED,
    CONFLICTING_CANDIDATES,
    EVIDENCE_FRESH_200,
    EVIDENCE_STRONG_304,
    EVIDENCE_WEAK_304,
    ONLY_FAILED_GETS,
    VALIDATOR_MISMATCH,
    VALIDATOR_MISSING,
    VARY_WILDCARD,
    WEAK_NOT_BYTE_EXACT,
    build_representation_report,
    dump_representation_json,
    etag_strong_match,
    etag_weak_match,
    parse_vary,
    variant_signature,
)
from crawlbudget.representation_engine import failed_get_keys, get_capture_receipts
from crawlbudget.storage import Storage
from crawlbudget.util import fingerprint, sha256_bytes

FIXTURE_DIR = Path(__file__).resolve().parents[1] / "fixtures" / "conditional"
EN_SHA = "a48cb8f910a25c4561b3ad0be8df511a813bee773239909a415e2cb912e26fe3"
REFLOW_SHA = "cc17f7352986b2b59b20815d456a0b21b9c42f56365653f27f873bafa755af63"
ZH_SHA = "6ab19249441bb755986ee56cf9e393ab30a2564b50102b4d23c3c639fcfc2d8d"
URL = "http://127.0.0.1:5021/mock-alpha/calendar.html"
T0, T1, T2 = ("2026-01-15T00:00:00Z", "2026-01-16T00:00:00Z",
              "2026-01-17T00:00:00Z")


class Response:
    def __init__(self, status, body=b"", etag="", vary="Accept-Language"):
        self.status_code = status
        self.headers = {"ETag": etag, "Vary": vary}
        self.body = body

    def iter_content(self, chunk_size=4096):
        yield self.body

    def close(self):
        pass


class Session:
    def __init__(self):
        self.calls = []
        self.trust_env = False

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return self._queued.pop(0)


@pytest.fixture
def bodies(en_body, reflow_body, zh_body):
    return {"en": en_body, "reflow": reflow_body, "zh": zh_body}


@pytest.fixture
def en_body():
    return (FIXTURE_DIR / "alpha-en.html").read_bytes()


@pytest.fixture
def reflow_body():
    return (FIXTURE_DIR / "alpha-en-reflow.html").read_bytes()


@pytest.fixture
def zh_body():
    return (FIXTURE_DIR / "alpha-zh.html").read_bytes()


def _cap(site, storage, body_dir, session, headers, response, when=T0):
    session._queued = getattr(session, "_queued", [])
    session._queued.append(response)
    return capture_and_record(site, URL, headers, body_dir, storage, "r1",
                              when, session=session)


def _report(storage, body_dir):
    return build_representation_report(storage, "r1", body_dirs=[body_dir])


def _by_variant(report):
    return {v.variant_key: v for v in report.subjects}


def _key(lang):
    return variant_signature((("accept-language", lang),),
                             parse_vary("Accept-Language"))


def test_weak_etag_304_keeps_language_variants_isolated(demo_config, tmp_path, bodies):
    site = demo_config.sites["mock-alpha"]
    body_dir = tmp_path / "bodies"
    with Storage(":memory:") as storage, Session() as s:
        _cap(site, storage, body_dir, s, {"Accept-Language": "en"},
             Response(200, bodies["en"], 'W/"en-v1"'))
        _cap(site, storage, body_dir, s, {"Accept-Language": "zh"},
             Response(200, bodies["zh"], '"zh-v1"'))
        _cap(site, storage, body_dir, s,
             {"Accept-Language": "en", "If-None-Match": 'W/"en-v1"'},
             Response(304, etag='W/"en-v1"'))
        report = _report(storage, body_dir)

    assert len(report.subjects) == 2
    en, zh = _by_variant(report)[_key("en")], _by_variant(report)[_key("zh")]
    assert en.evidence_level == EVIDENCE_WEAK_304
    assert en.body_sha256 == EN_SHA and en.current_bytes_identical is False
    assert en.contracts["semantic-readable"]["publishable"] is True
    assert en.contracts["byte-exact"] == {
        "publishable": False, "not_publishable_reason": WEAK_NOT_BYTE_EXACT}
    assert zh.trigger_status == 200 and zh.body_sha256 == ZH_SHA
    assert zh.contracts["byte-exact"]["publishable"] is True
    assert ZH_SHA not in [step.get("body_sha256") for step in en.source_chain]


def test_strong_etag_304_publishes_byte_exact_for_same_variant(demo_config, tmp_path, bodies):
    site = demo_config.sites["mock-alpha"]
    body_dir = tmp_path / "bodies"
    with Storage(":memory:") as storage, Session() as s:
        _cap(site, storage, body_dir, s, {"Accept-Language": "en"},
             Response(200, bodies["en"], '"en-strong-1"'))
        _cap(site, storage, body_dir, s,
             {"Accept-Language": "en", "If-None-Match": '"en-strong-1"'},
             Response(304, etag='"en-strong-1"'))
        verdict = _report(storage, body_dir).subjects[0]
    assert verdict.evidence_level == EVIDENCE_STRONG_304
    assert verdict.current_bytes_identical is True
    assert verdict.body_sha256 == EN_SHA
    assert verdict.contracts["byte-exact"]["publishable"] is True
    assert [step["status"] for step in verdict.source_chain] == [304, 200]


def test_fresh_200_with_reflow_bytes_publishes_both_contracts(demo_config, tmp_path, bodies):
    site = demo_config.sites["mock-alpha"]
    body_dir = tmp_path / "bodies"
    with Storage(":memory:") as storage, Session() as s:
        _cap(site, storage, body_dir, s, {"Accept-Language": "en"},
             Response(200, bodies["en"], 'W/"en-v1"'))
        _cap(site, storage, body_dir, s,
             {"Accept-Language": "en", "If-None-Match": 'W/"en-v1"'},
             Response(200, bodies["reflow"], 'W/"en-v2"'))
        verdict = _report(storage, body_dir).subjects[0]
    assert verdict.evidence_level == EVIDENCE_FRESH_200
    assert verdict.body_sha256 == REFLOW_SHA
    assert verdict.current_bytes_identical is True
    assert verdict.contracts["byte-exact"]["publishable"] is True


def test_missing_and_tampered_body_files_block_publication(demo_config, tmp_path, bodies):
    site = demo_config.sites["mock-alpha"]
    body_dir = tmp_path / "bodies"
    with Storage(":memory:") as storage, Session() as s:
        _cap(site, storage, body_dir, s, {"Accept-Language": "en"},
             Response(200, bodies["en"], '"s1"'))
        body_file = body_dir / f"{EN_SHA}.body"
        body_file.unlink()
        missing = _report(storage, body_dir).subjects[0]
        assert missing.not_publishable_reason == BODY_FILE_MISSING
        assert all(not c["publishable"] for c in missing.contracts.values())

        body_file.write_bytes(b"tampered bytes")
        tampered = _report(storage, body_dir).subjects[0]
        assert tampered.not_publishable_reason == BODY_FILE_TAMPERED
        assert tampered.body_path is None
        assert all(not c["publishable"] for c in tampered.contracts.values())


def test_conflicting_200_candidates_for_one_variant_are_blocked(demo_config, tmp_path, bodies):
    site = demo_config.sites["mock-alpha"]
    body_dir = tmp_path / "bodies"
    with Storage(":memory:") as storage, Session() as s:
        _cap(site, storage, body_dir, s, {"Accept-Language": "en"},
             Response(200, bodies["en"], '"same-strong"'))
        _cap(site, storage, body_dir, s, {"Accept-Language": "en"},
             Response(200, bodies["reflow"], '"same-strong"'), when=T1)
        _cap(site, storage, body_dir, s,
             {"Accept-Language": "en", "If-None-Match": '"same-strong"'},
             Response(304, etag='"same-strong"'), when=T2)
        verdict = _report(storage, body_dir).subjects[0]
    assert verdict.not_publishable_reason == CONFLICTING_CANDIDATES
    assert verdict.body_sha256 is None
    assert {c["body_sha256"] for c in verdict.candidates if c["verified"]} == {
        EN_SHA, REFLOW_SHA}


def test_validator_mismatch_and_missing_etag_block_reuse(demo_config, tmp_path, bodies):
    site = demo_config.sites["mock-alpha"]
    body_dir = tmp_path / "bodies"
    with Storage(":memory:") as storage, Session() as s:
        _cap(site, storage, body_dir, s, {"Accept-Language": "en"},
             Response(200, bodies["en"], '"en-v1"'))
        _cap(site, storage, body_dir, s,
             {"Accept-Language": "en", "If-None-Match": '"en-v2"'},
             Response(304, etag='"en-v2"'))
        mismatch = _report(storage, body_dir).subjects[0]
        assert mismatch.not_publishable_reason == VALIDATOR_MISMATCH
        assert mismatch.body_sha256 is None

        _cap(site, storage, body_dir, s,
             {"Accept-Language": "en", "If-None-Match": '"en-v1"'},
             Response(304, etag=""), when=T1)
        report = _report(storage, body_dir)
    missing = next(v for v in report.subjects
                   if v.not_publishable_reason == VALIDATOR_MISSING)
    assert missing.body_sha256 is None


def test_vary_wildcard_and_failed_only_gets_are_not_publishable(demo_config, tmp_path, bodies):
    site = demo_config.sites["mock-alpha"]
    body_dir = tmp_path / "bodies"
    with Storage(":memory:") as storage, Session() as s:
        _cap(site, storage, body_dir, s, {"Accept-Language": "en"},
             Response(200, bodies["en"], '"v1"', vary="*"))
        _cap(site, storage, body_dir, s,
             {"Accept-Language": "en", "If-None-Match": '"v1"'},
             Response(304, etag='"v1"', vary="*"))
        wildcard = next(v for v in _report(storage, body_dir).subjects
                        if v.not_publishable_reason == VARY_WILDCARD)
        assert wildcard.body_sha256 is None

    failed = RequestEvent(
        run_id="r1", site_id=site.site_id, job_id="j1", url_hash="h1", attempt=1,
        url=URL, status_code=500, outcome="http_error", elapsed_ms=1,
        response_bytes=0, content_type="", body_sha256="", error_code=None,
        evidence={"status": 500}, recorded_at=T0)
    with Storage(":memory:") as storage, Session() as s:
        storage.insert_request_event(failed)
        assert failed_get_keys(storage.get_request_events("r1")) == {
            (site.site_id, URL)}
        _cap(site, storage, body_dir, s,
             {"Accept-Language": "en", "If-None-Match": '"v1"'},
             Response(304, etag='"v1"'))
        verdict = _report(storage, body_dir).subjects[0]
    assert verdict.not_publishable_reason == ONLY_FAILED_GETS
    assert verdict.body_sha256 is None


def test_executor_success_event_requires_matching_body_file(demo_config, tmp_path):
    site = demo_config.sites["mock-alpha"]
    body_dir = tmp_path / "bodies"
    event = RequestEvent(
        run_id="r1", site_id=site.site_id, job_id="j1", url_hash="h1", attempt=1,
        url=URL, status_code=200, outcome="success", elapsed_ms=1,
        response_bytes=78, content_type="text/html", body_sha256=EN_SHA,
        error_code=None, evidence={"status": 200, "retained_bytes": 78},
        recorded_at=T0)
    with Storage(":memory:") as storage:
        storage.insert_request_event(event)
        assert _report(storage, body_dir).subjects[0].not_publishable_reason == BODY_FILE_MISSING
        body_dir.mkdir(parents=True, exist_ok=True)
        (body_dir / f"{EN_SHA}.body").write_bytes(
            (FIXTURE_DIR / "alpha-en.html").read_bytes())
        verdict = _report(storage, body_dir).subjects[0]
    assert verdict.trigger_origin == "executor_success"
    assert verdict.evidence_level == EVIDENCE_FRESH_200
    assert verdict.contracts["byte-exact"]["publishable"] is True


def test_changing_upstream_sha_recomputes_and_blocks_instead_of_warning(
    demo_config, tmp_path, bodies
):
    site = demo_config.sites["mock-alpha"]
    body_dir = tmp_path / "bodies"
    with Storage(":memory:") as storage, Session() as s:
        _cap(site, storage, body_dir, s, {"Accept-Language": "en"},
             Response(200, bodies["en"], 'W/"en-v1"'))
        assert _report(storage, body_dir).subjects[0].body_sha256 == EN_SHA
        storage.conn.execute(
            "UPDATE capture_receipts SET body_sha256=? WHERE body_sha256=?",
            (REFLOW_SHA, EN_SHA))
        storage.conn.commit()
        verdict = _report(storage, body_dir).subjects[0]
        assert verdict.body_sha256 == REFLOW_SHA
        assert verdict.not_publishable_reason == BODY_FILE_TAMPERED
        assert all(not c["publishable"] for c in verdict.contracts.values())
        storage.conn.execute(
            "UPDATE capture_receipts SET body_sha256=? WHERE body_sha256=?",
            (EN_SHA, REFLOW_SHA))
        storage.conn.commit()
        restored = _report(storage, body_dir).subjects[0]
    assert restored.body_sha256 == EN_SHA
    assert restored.contracts["byte-exact"]["publishable"] is True


def test_repeated_runs_are_byte_stable_and_self_fingerprinted(demo_config, tmp_path, bodies):
    site = demo_config.sites["mock-alpha"]
    body_dir = tmp_path / "bodies"
    with Storage(":memory:") as storage, Session() as s:
        _cap(site, storage, body_dir, s, {"Accept-Language": "en"},
             Response(200, bodies["en"], 'W/"en-v1"'))
        _cap(site, storage, body_dir, s, {"Accept-Language": "zh"},
             Response(200, bodies["zh"], '"zh-v1"'))
        _cap(site, storage, body_dir, s,
             {"Accept-Language": "en", "If-None-Match": 'W/"en-v1"'},
             Response(304, etag='W/"en-v1"'))
        first = dump_representation_json(_report(storage, body_dir))
        assert first == dump_representation_json(_report(storage, body_dir))
    payload = json.loads(first)
    copy = dict(payload)
    copy["fingerprint"] = ""
    assert payload["fingerprint"] == fingerprint(copy)


def test_receipt_persistence_is_idempotent(demo_config, tmp_path, bodies):
    site = demo_config.sites["mock-alpha"]
    body_dir = tmp_path / "bodies"
    with Storage(":memory:") as storage, Session() as s:
        receipt = _cap(site, storage, body_dir, s, {"Accept-Language": "en"},
                       Response(200, bodies["en"], 'W/"en-v1"'))
        assert insert_capture_receipt(storage, "r1", site.site_id, receipt, T0) is False
        rows = get_capture_receipts(storage, "r1")
    assert len(rows) == 1
    assert rows[0]["request_headers"] == [("Accept-Language", "en")]


def test_no_vary_header_still_isolates_language_variants(demo_config, tmp_path, bodies):
    # Without Vary, request-header identity must keep en and zh apart and must
    # not let the zh body backfill an en 304.
    site = demo_config.sites["mock-alpha"]
    body_dir = tmp_path / "bodies"
    with Storage(":memory:") as storage, Session() as s:
        _cap(site, storage, body_dir, s, {"Accept-Language": "en"},
             Response(200, bodies["en"], '"en-v1"', vary=""))
        _cap(site, storage, body_dir, s, {"Accept-Language": "zh"},
             Response(200, bodies["zh"], '"zh-v1"', vary=""))
        _cap(site, storage, body_dir, s,
             {"Accept-Language": "en", "If-None-Match": '"en-v1"'},
             Response(304, etag='"en-v1"', vary=""))
        report = _report(storage, body_dir)
    # The latest fact is the en 304; its single subject must reuse only the en
    # body and never the zh body sharing the same empty-Vary URL.
    assert len(report.subjects) == 1
    en = report.subjects[0]
    assert en.body_sha256 == EN_SHA
    assert en.trigger_status == 304
    assert en.evidence_level == EVIDENCE_STRONG_304
    assert en.contracts["byte-exact"]["publishable"] is True
    chain_shas = [step.get("body_sha256") for step in en.source_chain]
    assert EN_SHA in chain_shas and ZH_SHA not in chain_shas


def test_no_vary_en_304_cannot_reuse_zh_body(demo_config, tmp_path, bodies):
    from crawlbudget.representation import NO_CANDIDATE_200

    site = demo_config.sites["mock-alpha"]
    body_dir = tmp_path / "bodies"
    with Storage(":memory:") as storage, Session() as s:
        # Only a zh 200 exists; an en conditional 304 must not borrow it.
        _cap(site, storage, body_dir, s, {"Accept-Language": "zh"},
             Response(200, bodies["zh"], '"zh-v1"', vary=""))
        _cap(site, storage, body_dir, s,
             {"Accept-Language": "en", "If-None-Match": '"en-v1"'},
             Response(304, etag='"en-v1"', vary=""))
        verdict = _report(storage, body_dir).subjects[0]
    assert verdict.not_publishable_reason == NO_CANDIDATE_200
    assert verdict.body_sha256 is None
    assert all(not c["publishable"] for c in verdict.contracts.values())


def test_executor_304_is_blocked_and_never_feeds_a_representation(
    demo_config, demo_plan, tmp_path
):
    """The ordinary executor entry point must not act as a 304 cache."""
    from dataclasses import replace

    from crawlbudget.executor import ControlledExecutor
    from tests.conftest import prepare_plan

    request = replace(demo_plan.requests[0])
    plan = replace(demo_plan, requests=[request], leases=[demo_plan.leases[0]])
    body_dir = tmp_path / "bodies"
    with Storage(":memory:") as storage:
        prepare_plan(storage, plan)

        class _304Session:
            trust_env = False
            calls = []

            def get(self, url, **kwargs):
                self.calls.append(url)
                return Response(304, etag='"x"')

        ControlledExecutor(
            storage, demo_config.sites, plan, session=_304Session(),
            sleeper=lambda delay: None,
        ).run()
        events = storage.get_request_events(plan.run_id)
        assert events[0]["status_code"] == 304
        assert events[0]["outcome"] == "redirect_blocked"
        report = build_representation_report(storage, plan.run_id,
                                             body_dirs=[body_dir])
        # A blocked executor 304 yields no representation subject at all.
        assert report.subjects == []


def test_etag_comparison_helpers():
    assert etag_weak_match('W/"v1"', '"v1"')
    assert etag_strong_match('"v1"', '"v1"')
    assert not etag_strong_match('W/"v1"', '"v1"')
    assert not etag_weak_match('"v1"', '"v2"')
    assert parse_vary("Accept-Language, accept-encoding") == (
        "accept-encoding", "accept-language")
    assert sha256_bytes(b"x") and EN_SHA != ZH_SHA
