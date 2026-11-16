import json
import socket
from pathlib import Path

from crawlbudget.cli import main
from crawlbudget.storage import Storage
from tests.conftest import FIXTURES, write_simple_site_config


def _free_port() -> int:
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    return port


def _common_args(tmp_path: Path, config: Path, pages: Path, manifest: Path) -> list[str]:
    return ["--config", str(config), "--db", str(tmp_path / "ledger.sqlite3"),
            "--reports-dir", str(tmp_path / "reports"), "--pages-dir", str(pages),
            "--manifest", str(manifest), "--project-root", str(tmp_path)]


def test_cli_demo_reset_and_rerun_are_idempotent(tmp_path, capsys):
    port = _free_port()
    config, _seed, pages, manifest = write_simple_site_config(tmp_path, port)
    common = _common_args(tmp_path, config, pages, manifest)
    assert main(["demo", *common, "--run-id", "cli-run", "--reset"]) == 0
    output = capsys.readouterr().out
    assert "run: cli-run status: completed" in output and "granted=2" in output
    with Storage(tmp_path / "ledger.sqlite3") as storage:
        assert storage.get_run("cli-run")["status"] == "completed"
        assert storage.attempt_count("cli-run") == 2
        assert storage.get_report("cli-run") is not None
    for name in ("summary.json", "summary.md", "summary.html"):
        assert (tmp_path / "reports" / "cli-run" / name).exists()
    summary = json.loads((tmp_path / "reports" / "cli-run" / "summary.json").read_text())
    assert summary["totals"]["http_attempts"] == 2
    assert main(["demo", *common, "--run-id", "cli-run"]) == 0
    with Storage(tmp_path / "ledger.sqlite3") as storage:
        assert storage.attempt_count("cli-run") == 2
        assert [lease["used_requests"] for lease in storage.get_leases("cli-run")] == [2]


def test_cli_plan_and_inspect(tmp_path, capsys):
    port = _free_port()
    config, _seed, pages, manifest = write_simple_site_config(tmp_path, port)
    common = _common_args(tmp_path, config, pages, manifest)
    assert main(["plan", *common, "--run-id", "plan-run"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["run_id"] == "plan-run"
    assert len(payload["requests"]) == 2
    assert payload["quota_available"]["mock-alpha"] == 3
    assert main(["demo", *common, "--run-id", "cli-run", "--reset"]) == 0
    capsys.readouterr()
    code = main(["inspect-run", "--db", str(tmp_path / "ledger.sqlite3"),
                 "--run-id", "cli-run", "--project-root", str(tmp_path)])
    inspected = json.loads(capsys.readouterr().out)
    assert code == 0
    assert inspected["run"]["run_id"] == "cli-run"
    assert inspected["request_events"] == 2


def test_cli_config_error_returns_two(tmp_path, capsys):
    port = _free_port()
    config, _seed, pages, manifest = write_simple_site_config(tmp_path, port)
    text = config.read_text(encoding="utf-8").replace("daily_quota: 3", "daily_quota: 0")
    bad = tmp_path / "bad.yaml"
    bad.write_text(text, encoding="utf-8")
    code = main(["demo", *_common_args(tmp_path, bad, pages, manifest), "--reset"])
    captured = capsys.readouterr()
    assert code == 2 and "CFG_NON_POSITIVE_LIMIT" in captured.err


def test_cli_check_golden_mismatch_is_nonzero(tmp_path, capsys):
    port = _free_port()
    config, _seed, pages, manifest = write_simple_site_config(tmp_path, port)
    common = _common_args(tmp_path, config, pages, manifest)
    assert main(["demo", *common, "--run-id", "cli-run", "--reset"]) == 0
    capsys.readouterr()
    code = main(["check-golden", "--db", str(tmp_path / "ledger.sqlite3"),
                 "--run-id", "cli-run", "--expected-dir", str(FIXTURES / "expected"),
                 "--project-root", str(tmp_path)])
    captured = capsys.readouterr()
    assert code == 3 and "GOLDEN_MISMATCH" in captured.err and "$." in captured.err
