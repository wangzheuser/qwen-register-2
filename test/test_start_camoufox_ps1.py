import json
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
START_SCRIPT = ROOT / "start_camoufox.ps1"
POWERSHELL = shutil.which("powershell") or shutil.which("pwsh")

pytestmark = pytest.mark.skipif(POWERSHELL is None, reason="PowerShell is not available")


def run_start(input_text: str, config_path: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            POWERSHELL,
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(START_SCRIPT),
            "-DryRun",
            "-SkipDependencyInstall",
            "-ConfigPath",
            str(config_path),
        ],
        input=input_text,
        text=True,
        capture_output=True,
        cwd=ROOT,
        timeout=30,
    )


def assert_success(result: subprocess.CompletedProcess[str]) -> None:
    assert result.returncode == 0, result.stdout + result.stderr


def expected_defaults() -> dict:
    return {
        "count": 1,
        "email_provider": "mailtm",
        "api_proxy": "",
        "browser_proxy": "",
        "concurrency": 1,
        "captcha_timeout": 600,
        "verbose": False,
        "sync_qwen2api": False,
        "qwen2api_base_url": "http://127.0.0.1:7860",
        "qwen2api_admin_key": "admin",
        "qwen2api_timeout": 30,
        "strict": True,
    }


def test_start_camoufox_ps1_has_valid_powershell_syntax():
    result = subprocess.run(
        [
            POWERSHELL,
            "-NoProfile",
            "-Command",
            "[scriptblock]::Create((Get-Content -Raw .\\start_camoufox.ps1)) | Out-Null",
        ],
        text=True,
        capture_output=True,
        cwd=ROOT,
        timeout=30,
    )

    assert_success(result)


def test_start_camoufox_ps1_uses_defaults_and_saves_separate_config(tmp_path):
    config_path = tmp_path / "start-camoufox-config.json"
    result = run_start("\n\n\n\n\n\n\n\n", config_path)

    assert_success(result)
    assert "qwenv4_camoufox.py 1 --email-provider mailtm --concurrency 1 --captcha-timeout 600 --captcha-solver ai --captcha-ai-attempts 3 --no-captcha-ai-fallback-manual --captcha-drag-backend os --captcha-drag-strategy fast_quadratic --strict" in result.stdout
    saved = json.loads(config_path.read_text(encoding="utf-8"))
    assert saved == expected_defaults()
    assert "captcha_ai_api_key" not in saved
    assert "captcha_solver" not in saved


def test_start_camoufox_ps1_saves_and_reuses_values(tmp_path):
    config_path = tmp_path / "start-camoufox-config.json"

    first = run_start("3\n2\nhttp://127.0.0.1:7890\nhttp://baokemeng.{uuid}:testpass@127.0.0.1:9200\ny\n2\n600\ny\nhttp://127.0.0.1:9999\nsecret\n45\n\n", config_path)
    assert_success(first)
    assert "qwenv4_camoufox.py 3 --email-provider mailtm --api-proxy http://127.0.0.1:7890 --browser-proxy http://baokemeng.{uuid}:testpass@127.0.0.1:9200 --concurrency 2 --captcha-timeout 600 --captcha-solver ai --captcha-ai-attempts 3 --no-captcha-ai-fallback-manual --captcha-drag-backend os --captcha-drag-strategy fast_quadratic --sync-qwen2api --qwen2api-base-url http://127.0.0.1:9999 --qwen2api-admin-key secret --qwen2api-timeout 45 --verbose --strict" in first.stdout

    second = run_start("\n\n\n\n\n\n\n\n\n\n\n", config_path)
    assert_success(second)
    assert "qwenv4_camoufox.py 3 --email-provider mailtm --api-proxy http://127.0.0.1:7890 --browser-proxy http://baokemeng.{uuid}:testpass@127.0.0.1:9200 --concurrency 2 --captcha-timeout 600 --captcha-solver ai --captcha-ai-attempts 3 --no-captcha-ai-fallback-manual --captcha-drag-backend os --captcha-drag-strategy fast_quadratic --sync-qwen2api --qwen2api-base-url http://127.0.0.1:9999 --qwen2api-admin-key secret --qwen2api-timeout 45 --verbose --strict" in second.stdout


def test_start_camoufox_ps1_executes_camoufox_entry_and_streams_output(tmp_path):
    temp_script = tmp_path / "start_camoufox.ps1"
    temp_config = tmp_path / "start-camoufox-config.json"
    temp_script.write_text(START_SCRIPT.read_text(encoding="utf-8"), encoding="utf-8")
    (tmp_path / "qwenv4_camoufox.py").write_text(
        """
import sys
print("Camoufox stdout 日志")
print("Camoufox stderr 日志", file=sys.stderr)
sys.exit(7)
""".strip(),
        encoding="utf-8",
    )

    result = subprocess.run(
        [
            POWERSHELL,
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(temp_script),
            "-SkipDependencyInstall",
            "-ConfigPath",
            str(temp_config),
        ],
        input="1\n\n\n\n\n1\n600\nn\n\n",
        text=True,
        capture_output=True,
        cwd=tmp_path,
        timeout=30,
    )

    assert result.returncode == 7, result.stdout + result.stderr
    assert "Camoufox stdout 日志" in result.stdout
    assert "Camoufox stderr 日志" in result.stderr


def test_start_camoufox_ps1_interrupt_cleanup_selftest_notifies_python_before_kill():
    result = subprocess.run(
        [
            POWERSHELL,
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(START_SCRIPT),
            "-SkipDependencyInstall",
            "-InterruptCleanupSelfTest",
        ],
        text=True,
        capture_output=True,
        cwd=ROOT,
        timeout=30,
    )

    assert_success(result)
    assert "等待 Python 停止新任务并关闭当前 Camoufox 浏览器" in result.stdout
    assert "Python 已完成中断清理" in result.stdout
