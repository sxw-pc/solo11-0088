from pathlib import Path

import pytest

from crawlbudget.conditional_capture import capture_conditional


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
    def __init__(self, responses):
        self.responses = iter(responses)
        self.calls = []

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return next(self.responses)


def test_conditional_receipts_capture_bytes_and_304_without_reuse(demo_config, tmp_path):
    site = demo_config.sites["mock-alpha"]
    url = site.base_url + "calendar.html"
    base = Path(__file__).resolve().parents[1] / "fixtures" / "conditional"
    en = (base / "alpha-en.html").read_bytes()
    reflow = (base / "alpha-en-reflow.html").read_bytes()
    zh = (base / "alpha-zh.html").read_bytes()
    assert en != reflow
    session = Session([
        Response(200, en, 'W/"en-v1"'),
        Response(200, zh, '"zh-v1"'),
        Response(304, etag='W/"en-v1"'),
    ])
    first = capture_conditional(site, url, {"Accept-Language": "en"}, tmp_path, session=session)
    second = capture_conditional(site, url, {"Accept-Language": "zh"}, tmp_path, session=session)
    third = capture_conditional(site, url, {"Accept-Language": "en", "If-None-Match": 'W/"en-v1"'}, tmp_path, session=session)
    assert first.status == second.status == 200
    assert first.body_sha256 != second.body_sha256
    assert Path(first.body_path).read_bytes() == en
    assert Path(second.body_path).read_bytes() == zh
    assert third.status == 304 and third.body_sha256 is None and third.body_path is None
    assert third.etag == first.etag == 'W/"en-v1"'
    assert len(session.calls) == 3
    with pytest.raises(ValueError, match="outside"):
        capture_conditional(site, "http://example.invalid/calendar.html", {}, tmp_path, session=session)
