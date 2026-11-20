"""End-to-end CLI tests for capture + representation attestation.

A real Flask app binds an ephemeral loopback port and serves 200/304
conditional responses. These tests never leave the local machine.
"""

import json
import socket
import threading
from pathlib import Path

import pytest
from werkzeug.serving import make_server

from crawlbudget.cli import main
from crawlbudget.demo_site import stop_mock_servers, RunningMockServer
from crawlbudget.storage import Storage

EN_BODY = (Path(__file__).resolve().parents[1]
           / "fixtures" / "conditional" / "alpha-en.html").read_bytes()


def _free_port() -> int:
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    return port


def _conditional_app():
    from flask import Flask, request, Response

    app = Flask("conditional-integration")

    @app.get("/mock-alpha/calendar.html")
    def calendar():
        if request.headers.get("Accept-Language") == "zh":
            body, tag = b"<html><body>zh</body></html>", '"zh-v1"'
        else:
            body, tag = EN_BODY, 'W/"en-v1"'
        if request.headers.get("If-None-Match") == tag:
            return Response(status=304,
                            headers={"ETag": tag, "Vary": "Accept-Language"})
        return Response(body, status=200,
                        headers={"ETag": tag, "Vary": "Accept-Language"})

    return app


@pytest.fixture
def server():
    port = _free_port()
    httpd = make_server("127.0.0.1", port, _conditional_app(), threaded=True)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    running = RunningMockServer("mock-alpha", "127.0.0.1", port, httpd, thread)
    try:
        yield running
    finally:
        stop_mock_servers([running])


def _config(tmp_path: Path, port: int) -> Path:
    (tmp_path / "robots.txt").write_text("User-agent: *\nAllow: /\n",
                                         encoding="utf-8")
    (tmp_path / "seeds.txt").write_text(
        f"http://127.0.0.1:{port}/mock-alpha/calendar.html\n", encoding="utf-8")
    config = tmp_path / "sites.yaml"
    config.write_text(f"""
sites:
  - site_id: mock-alpha
    base_url: http://127.0.0.1:{port}/mock-alpha/
    robots_file: {tmp_path / 'robots.txt'}
    daily_quota: 20
    max_requests_per_run: 20
    max_rps: 50
    burst: 2
    crawl_delay_ms: 1
    max_response_bytes: 65536
    max_retries: 0
    enabled: true
jobs:
  - job_id: job-alpha
    site_id: mock-alpha
    priority: 10
    seed_file: {tmp_path / 'seeds.txt'}
    enabled: true
""", encoding="utf-8")
    return config


def test_capture_and_representation_cli_full_flow(tmp_path, capsys, server):
    config = _config(tmp_path, server.port)
    db, bodies = tmp_path / "ledger.sqlite3", tmp_path / "bodies"
    url = f"http://127.0.0.1:{server.port}/mock-alpha/calendar.html"
    base = ["--config", str(config), "--db", str(db),
            "--project-root", str(tmp_path)]

    assert main(["capture", *base, "--run-id", "cap1", "--site", "mock-alpha",
                 "--url", url, "--header", "Accept-Language: en",
                 "--bodies-dir", str(bodies)]) == 0
    en_sha = json.loads(capsys.readouterr().out)["body_sha256"]
    assert main(["capture", *base, "--run-id", "cap1", "--site", "mock-alpha",
                 "--url", url, "--header", "Accept-Language: en",
                 "--header", 'If-None-Match: W/"en-v1"',
                 "--bodies-dir", str(bodies)]) == 0
    second = json.loads(capsys.readouterr().out)
    assert second["status"] == 304 and second["body_sha256"] is None

    out_file, again = tmp_path / "rep.json", tmp_path / "rep-again.json"
    rep = ["representation", "--db", str(db), "--run-id", "cap1",
           "--bodies-dir", str(bodies), "--project-root", str(tmp_path)]
    assert main([*rep, "--out", str(out_file)]) == 0
    assert "REPRESENTATION_OK" in capsys.readouterr().out
    main([*rep, "--out", str(again)])
    assert out_file.read_bytes() == again.read_bytes()

    verdict = json.loads(out_file.read_text())["subjects"][0]
    assert verdict["evidence_level"] == "weak_validator_304_semantic_only"
    assert verdict["body_sha256"] == en_sha
    assert verdict["current_bytes_identical"] is False
    assert verdict["contracts"]["semantic-readable"]["publishable"] is True
    assert verdict["contracts"]["byte-exact"] == {
        "publishable": False,
        "not_publishable_reason": "weak_validator_not_byte_exact"}
    assert [step["status"] for step in verdict["source_chain"]] == [304, 200]

    with Storage(db) as storage:
        assert [r["status"] for r in
                storage._all("SELECT * FROM capture_receipts ORDER BY receipt_id")] == [200, 304]
    # Out-of-scope URLs are refused before any socket call.
    assert main(["capture", *base, "--run-id", "cap1", "--site", "mock-alpha",
                 "--url", "http://example.invalid/mock-alpha/calendar.html",
                 "--bodies-dir", str(bodies)]) == 1


def test_capture_rejects_unsupported_header(tmp_path, capsys, server):
    config = _config(tmp_path, server.port)
    db = tmp_path / "ledger.sqlite3"
    url = f"http://127.0.0.1:{server.port}/mock-alpha/calendar.html"
    code = main(["capture", "--config", str(config), "--db", str(db),
                 "--project-root", str(tmp_path), "--run-id", "cap2",
                 "--site", "mock-alpha", "--url", url,
                 "--header", "Authorization: Bearer x",
                 "--bodies-dir", str(tmp_path / "bodies")])
    assert code == 2
    assert "unsupported conditional request header" in capsys.readouterr().err
    with Storage(db) as storage:
        assert storage._all("SELECT * FROM capture_receipts") == []


def test_representation_requires_known_run(tmp_path, capsys):
    db = tmp_path / "ledger.sqlite3"
    Storage(db).close()
    code = main(["representation", "--db", str(db), "--run-id", "ghost",
                 "--project-root", str(tmp_path)])
    assert code == 2 and "run not found" in capsys.readouterr().err
