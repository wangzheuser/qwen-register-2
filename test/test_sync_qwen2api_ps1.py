import json
import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SYNC_SCRIPT = ROOT / "sync_qwen2api.ps1"
POWERSHELL = shutil.which("powershell") or shutil.which("pwsh")

pytestmark = pytest.mark.skipif(POWERSHELL is None, reason="PowerShell is not available")


def run_sync(input_text: str, config_path: Path, *, accounts_file: Path | None = None) -> subprocess.CompletedProcess[str]:
    command = [
        POWERSHELL,
        "-NoProfile",
        "-ExecutionPolicy",
        "Bypass",
        "-File",
        str(SYNC_SCRIPT),
        "-DryRun",
        "-ConfigPath",
        str(config_path),
    ]
    if accounts_file is not None:
        command += ["-AccountsFile", str(accounts_file)]
    return subprocess.run(
        command,
        input=input_text,
        text=True,
        capture_output=True,
        cwd=ROOT,
        timeout=30,
        env=os.environ.copy(),
    )


def assert_success(result: subprocess.CompletedProcess[str]) -> None:
    assert result.returncode == 0, result.stdout + result.stderr


def normalize_command(text: str) -> str:
    text = text.replace("\\", "/")
    text = re.sub(r"[A-Za-z]:/[^\r\n ]*?/qwen-register-2/", "", text)
    return text


def test_sync_qwen2api_ps1_has_valid_powershell_syntax():
    result = subprocess.run(
        [
            POWERSHELL,
            "-NoProfile",
            "-Command",
            "[scriptblock]::Create((Get-Content -Raw .\\sync_qwen2api.ps1)) | Out-Null",
        ],
        text=True,
        capture_output=True,
        cwd=ROOT,
        timeout=30,
    )

    assert_success(result)


def test_sync_qwen2api_ps1_uses_builtin_defaults_without_config(tmp_path):
    config_path = tmp_path / "missing-start-config.json"
    result = run_sync("\n\n\n\n\n\n", config_path)

    assert_success(result)
    output = normalize_command(result.stdout)
    assert "sync_qwen2api_accounts.py --accounts-file qwen_accounts.json --qwen2api-base-url http://127.0.0.1:7860 --qwen2api-admin-key admin --qwen2api-timeout 30" in output
    assert "--force" not in output
    assert "--dry-run" not in output


def test_sync_qwen2api_ps1_reads_qwen2api_defaults_from_start_config(tmp_path):
    config_path = tmp_path / "start-config.json"
    original_config = {
        "qwen2api_base_url": "http://127.0.0.1:9999",
        "qwen2api_admin_key": "secret",
        "qwen2api_timeout": 45,
    }
    config_path.write_text(json.dumps(original_config, ensure_ascii=False), encoding="utf-8")

    result = run_sync("\n\n\n\n\n\n", config_path)

    assert_success(result)
    output = normalize_command(result.stdout)
    assert "--qwen2api-base-url http://127.0.0.1:9999 --qwen2api-admin-key secret --qwen2api-timeout 45" in output
    assert json.loads(config_path.read_text(encoding="utf-8")) == original_config


def test_sync_qwen2api_ps1_input_overrides_defaults_without_saving(tmp_path):
    config_path = tmp_path / "start-config.json"
    original_config = {
        "qwen2api_base_url": "http://old.example",
        "qwen2api_admin_key": "old-key",
        "qwen2api_timeout": 30,
    }
    config_path.write_text(json.dumps(original_config, ensure_ascii=False), encoding="utf-8")
    accounts_file = tmp_path / "custom_accounts.json"

    result = run_sync(f"{accounts_file}\nhttp://new.example\nnew-key\n60\ny\ny\n", config_path)

    assert_success(result)
    output = normalize_command(result.stdout)
    assert "--accounts-file" in output
    assert "custom_accounts.json" in output
    assert "--qwen2api-base-url http://new.example --qwen2api-admin-key new-key --qwen2api-timeout 60 --force --dry-run" in output
    assert json.loads(config_path.read_text(encoding="utf-8")) == original_config


def test_sync_qwen2api_ps1_accounts_file_parameter_sets_default(tmp_path):
    config_path = tmp_path / "missing-start-config.json"
    accounts_file = tmp_path / "from-param.json"

    result = run_sync("\n\n\n\n\n\n", config_path, accounts_file=accounts_file)

    assert_success(result)
    output = normalize_command(result.stdout)
    assert "--accounts-file" in output
    assert "from-param.json" in output


def test_sync_qwen2api_ps1_dry_run_does_not_execute_python(tmp_path):
    config_path = tmp_path / "missing-start-config.json"
    result = run_sync("\n\n\n\n\n\n", config_path)

    assert_success(result)
    assert "包装层 DryRun：不会执行同步。" in result.stdout
