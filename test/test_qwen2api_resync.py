import json
from pathlib import Path

import pytest


def write_accounts(path: Path, accounts):
    path.write_text(json.dumps(accounts, ensure_ascii=False, indent=2), encoding="utf-8")


def read_accounts(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def test_default_syncs_only_unsuccessful_accounts_with_token(tmp_path, monkeypatch, capsys):
    import sync_qwen2api_accounts as resync
    from integrations.qwen2api import Qwen2ApiSyncResult

    accounts_path = tmp_path / "qwen_accounts.json"
    write_accounts(
        accounts_path,
        [
            {"email": "new@example.com", "password": "p1", "token": "token-new"},
            {
                "email": "done@example.com",
                "password": "p2",
                "token": "token-done",
                "qwen2api_sync": {"status": "success"},
            },
            {"email": "missing-token@example.com", "password": "p3", "token": ""},
            {
                "email": "failed@example.com",
                "password": "p4",
                "token": "token-failed",
                "qwen2api_sync": {"status": "failed", "message": "old"},
            },
        ],
    )
    calls = []

    def fake_sync_account_to_qwen2api(*, email, password, token, config):
        calls.append((email, password, token, config.base_url, config.admin_key, config.timeout))
        return Qwen2ApiSyncResult(ok=True, message=f"qwen2API 同步成功: {email}", status_code=200)

    monkeypatch.setattr(resync, "sync_account_to_qwen2api", fake_sync_account_to_qwen2api)

    exit_code = resync.main(["--accounts-file", str(accounts_path), "--qwen2api-base-url", "http://api", "--qwen2api-admin-key", "secret", "--qwen2api-timeout", "45"])

    assert exit_code == 0
    assert [call[0] for call in calls] == ["new@example.com", "failed@example.com"]
    assert calls[0][3:] == ("http://api", "secret", 45)
    data = read_accounts(accounts_path)
    assert data[0]["qwen2api_sync"]["status"] == "success"
    assert data[0]["qwen2api_sync"]["status_code"] == 200
    assert data[0]["qwen2api_sync"]["synced_at"]
    assert data[1]["qwen2api_sync"] == {"status": "success"}
    assert "qwen2api_sync" not in data[2]
    assert data[3]["qwen2api_sync"]["status"] == "success"
    out = capsys.readouterr().out
    assert "总账号数: 4" in out
    assert "待同步: 2" in out
    assert "成功: 2" in out


def test_force_resyncs_successful_accounts(tmp_path, monkeypatch):
    import sync_qwen2api_accounts as resync
    from integrations.qwen2api import Qwen2ApiSyncResult

    accounts_path = tmp_path / "qwen_accounts.json"
    write_accounts(
        accounts_path,
        [
            {
                "email": "done@example.com",
                "password": "p2",
                "token": "token-done",
                "qwen2api_sync": {"status": "success"},
            }
        ],
    )
    calls = []

    def fake_sync_account_to_qwen2api(*, email, password, token, config):
        calls.append(email)
        return Qwen2ApiSyncResult(ok=True, message="ok", status_code=200)

    monkeypatch.setattr(resync, "sync_account_to_qwen2api", fake_sync_account_to_qwen2api)

    assert resync.main(["--accounts-file", str(accounts_path), "--force"]) == 0

    assert calls == ["done@example.com"]
    assert read_accounts(accounts_path)[0]["qwen2api_sync"]["status"] == "success"


def test_failure_result_is_written_back_and_returns_nonzero(tmp_path, monkeypatch):
    import sync_qwen2api_accounts as resync
    from integrations.qwen2api import Qwen2ApiSyncResult

    accounts_path = tmp_path / "qwen_accounts.json"
    write_accounts(accounts_path, [{"email": "bad@example.com", "password": "p", "token": "bad-token"}])

    def fake_sync_account_to_qwen2api(*, email, password, token, config):
        return Qwen2ApiSyncResult(ok=False, message="Invalid token", status_code=400)

    monkeypatch.setattr(resync, "sync_account_to_qwen2api", fake_sync_account_to_qwen2api)

    assert resync.main(["--accounts-file", str(accounts_path)]) == 1

    sync_state = read_accounts(accounts_path)[0]["qwen2api_sync"]
    assert sync_state["status"] == "failed"
    assert sync_state["message"] == "Invalid token"
    assert sync_state["status_code"] == 400
    assert sync_state["synced_at"]


def test_dry_run_does_not_send_requests_or_write_file(tmp_path, monkeypatch, capsys):
    import sync_qwen2api_accounts as resync

    accounts_path = tmp_path / "qwen_accounts.json"
    original = [{"email": "new@example.com", "password": "p1", "token": "token-new"}]
    write_accounts(accounts_path, original)

    def fail_sync(**_kwargs):
        raise AssertionError("dry-run should not sync")

    monkeypatch.setattr(resync, "sync_account_to_qwen2api", fail_sync)

    assert resync.main(["--accounts-file", str(accounts_path), "--dry-run"]) == 0

    assert read_accounts(accounts_path) == original
    out = capsys.readouterr().out
    assert "DRY-RUN" in out
    assert "new@example.com" in out


def test_missing_token_accounts_are_skipped_without_write(tmp_path, monkeypatch, capsys):
    import sync_qwen2api_accounts as resync

    accounts_path = tmp_path / "qwen_accounts.json"
    original = [{"email": "pending@example.com", "password": "p", "token": None}]
    write_accounts(accounts_path, original)

    def fail_sync(**_kwargs):
        raise AssertionError("missing-token account should not sync")

    monkeypatch.setattr(resync, "sync_account_to_qwen2api", fail_sync)

    assert resync.main(["--accounts-file", str(accounts_path)]) == 0

    assert read_accounts(accounts_path) == original
    assert "跳过: 1" in capsys.readouterr().out


def test_load_accounts_requires_json_array(tmp_path):
    import sync_qwen2api_accounts as resync

    accounts_path = tmp_path / "qwen_accounts.json"
    accounts_path.write_text("{}", encoding="utf-8")

    with pytest.raises(ValueError, match="JSON 数组"):
        resync.load_accounts(accounts_path)
