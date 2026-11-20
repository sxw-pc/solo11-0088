"""Offline verification of receipt tokens, absence statements, and verdicts."""

from crawlbudget.receipts import (
    VERDICT_CONFLICT,
    VERDICT_NOT_RECEIVED,
    VERDICT_RECEIVED,
    VERDICT_UNVERIFIABLE,
    classify_receipt_reply,
    make_intent_id,
    sign_absence,
    sign_receipt,
    verify_absence,
    verify_receipt,
)

INTENT = {
    "intent_id": make_intent_id("run", "site-a", "job", "h" * 64, 1, "lease-1"),
    "run_id": "run", "site_id": "site-a", "job_id": "job",
    "url_hash": "h" * 64, "attempt": 1, "lease_id": "lease-1",
    "url": "http://127.0.0.1:5021/site-a/x.html",
}


def _receipt(**overrides):
    fields = {
        "intent_id": INTENT["intent_id"], "site_id": "site-a", "run_id": "run",
        "lease_id": "lease-1", "path": "x.html", "status": 200,
        "body_sha256": "b" * 64, "seq": 1,
    }
    fields.update(overrides)
    return sign_receipt("site-a", fields)


def test_intent_id_is_stable_and_binds_every_field():
    base = make_intent_id("run", "site-a", "job", "h" * 64, 1, "lease-1")
    assert base == make_intent_id("run", "site-a", "job", "h" * 64, 1, "lease-1")
    assert base.startswith("intent-")
    varied = [
        make_intent_id("run2", "site-a", "job", "h" * 64, 1, "lease-1"),
        make_intent_id("run", "site-b", "job", "h" * 64, 1, "lease-1"),
        make_intent_id("run", "site-a", "job2", "h" * 64, 1, "lease-1"),
        make_intent_id("run", "site-a", "job", "g" * 64, 1, "lease-1"),
        make_intent_id("run", "site-a", "job", "h" * 64, 2, "lease-1"),
        make_intent_id("run", "site-a", "job", "h" * 64, 1, "lease-2"),
    ]
    assert len({base, *varied}) == 7


def test_receipt_roundtrip_and_tamper_detection():
    receipt = _receipt()
    assert verify_receipt("site-a", receipt)
    assert not verify_receipt("site-b", receipt)  # wrong site key
    for mutation in (
        {"status": 500}, {"lease_id": "lease-2"}, {"body_sha256": "c" * 64},
        {"token": "0" * 64}, {"seq": 2},
    ):
        tampered = {**receipt, **mutation}
        assert not verify_receipt("site-a", tampered), mutation
    assert not verify_receipt("site-a", {"intent_id": "x"})
    assert not verify_receipt("site-a", "not-a-dict")


def test_absence_statement_roundtrip_and_tamper_detection():
    statement = sign_absence("site-a", INTENT["intent_id"])
    assert verify_absence("site-a", statement)
    assert not verify_absence("site-b", statement)
    assert not verify_absence("site-a", {**statement, "intent_id": "other"})
    assert not verify_absence("site-a", {**statement, "token": "0" * 64})
    assert not verify_absence("site-a", {**statement, "verdict": "received"})


def test_classify_reply_matrix():
    receipt = _receipt()
    verdict = classify_receipt_reply(INTENT, 200, receipt)
    assert verdict.kind == VERDICT_RECEIVED and verdict.receipt == receipt
    absence = sign_absence("site-a", INTENT["intent_id"])
    assert classify_receipt_reply(INTENT, 404, absence).kind == VERDICT_NOT_RECEIVED
    foreign = sign_absence("site-a", "intent-other")
    assert classify_receipt_reply(INTENT, 404, foreign).kind == VERDICT_UNVERIFIABLE
    bad_token = {**receipt, "token": "0" * 64}
    assert classify_receipt_reply(INTENT, 200, bad_token).kind == VERDICT_UNVERIFIABLE
    conflict = _receipt(lease_id="lease-2")
    verdict = classify_receipt_reply(INTENT, 200, conflict)
    assert verdict.kind == VERDICT_CONFLICT and verdict.receipt == conflict
    assert classify_receipt_reply(INTENT, 500, receipt).kind == VERDICT_UNVERIFIABLE
    assert classify_receipt_reply(INTENT, 200, ["not", "a", "dict"]).kind == (
        VERDICT_UNVERIFIABLE)
