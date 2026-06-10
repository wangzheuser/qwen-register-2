import json
import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
START_SCRIPT = ROOT / "start.ps1"
POWERSHELL = shutil.which("powershell") or shutil.which("pwsh")

pytestmark = pytest.mark.skipif(POWERSHELL is None, reason="PowerShell is not available")


def run_start(input_text: str, config_path: Path, *, captcha_ai_key: str | None = None) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env.pop("CAPTCHA_AI_API_KEY", None)
    if captcha_ai_key is not None:
        env["CAPTCHA_AI_API_KEY"] = captcha_ai_key
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
        env=env,
    )


def without_log_arg(text: str) -> str:
    return re.sub(r" --log-file (?:(?:[A-Za-z]:)?[^\r\n]+?\.log)", "", text)


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
        "captcha_solver": "ddddocr",
        "verbose": False,
        "sync_qwen2api": False,
        "qwen2api_base_url": "http://127.0.0.1:7860",
        "qwen2api_admin_key": "admin",
        "qwen2api_timeout": 30,
        "strict": True,
    }


def test_start_ps1_has_valid_powershell_syntax():
    result = subprocess.run(
        [
            POWERSHELL,
            "-NoProfile",
            "-Command",
            "[scriptblock]::Create((Get-Content -Raw .\\start.ps1)) | Out-Null",
        ],
        text=True,
        capture_output=True,
        cwd=ROOT,
        timeout=30,
    )

    assert_success(result)


def test_start_ps1_uses_builtin_defaults_and_saves_config(tmp_path):
    config_path = tmp_path / "start-config.json"
    result = run_start("\n\n\n\n\n\n\n\n\n", config_path)

    assert_success(result)
    command = without_log_arg(result.stdout)
    assert "qwenv4.py 1 --email-provider mailtm --concurrency 1 --account-retries 3 --captcha-solver ddddocr --captcha-ai-attempts 3 --no-captcha-ai-fallback-manual --captcha-drag-backend os --captcha-drag-strategy fast_quadratic --strict" in command
    assert "--captcha-timeout" not in command
    saved = json.loads(config_path.read_text(encoding="utf-8"))
    assert saved == expected_defaults()
    assert "captcha_ai_api_key" not in saved


def test_start_ps1_saves_custom_values_then_reuses_them_as_defaults(tmp_path):
    config_path = tmp_path / "start-config.json"

    first = run_start("5\n2\nhttp://127.0.0.1:7890\nhttp://baokemeng.{uuid}:testpass@127.0.0.1:9200\ny\n4\n\ny\nhttp://127.0.0.1:9999\nsecret\n45\n\n", config_path)
    assert_success(first)
    assert "qwenv4.py 5 --email-provider mailtm --api-proxy http://127.0.0.1:7890 --browser-proxy http://baokemeng.{uuid}:testpass@127.0.0.1:9200 --concurrency 4 --account-retries 3 --captcha-solver ddddocr --captcha-ai-attempts 3 --no-captcha-ai-fallback-manual --captcha-drag-backend os --captcha-drag-strategy fast_quadratic --sync-qwen2api --qwen2api-base-url http://127.0.0.1:9999 --qwen2api-admin-key secret --qwen2api-timeout 45 --verbose --strict" in without_log_arg(first.stdout)
    expected = expected_defaults() | {
        "count": 5,
        "api_proxy": "http://127.0.0.1:7890",
        "browser_proxy": "http://baokemeng.{uuid}:testpass@127.0.0.1:9200",
        "concurrency": 4,
        "verbose": True,
        "sync_qwen2api": True,
        "qwen2api_base_url": "http://127.0.0.1:9999",
        "qwen2api_admin_key": "secret",
        "qwen2api_timeout": 45,
    }
    assert json.loads(config_path.read_text(encoding="utf-8")) == expected

    second = run_start("\n\n\n\n\n\n\n\n\n\n\n", config_path)
    assert_success(second)
    assert "qwenv4.py 5 --email-provider mailtm --api-proxy http://127.0.0.1:7890 --browser-proxy http://baokemeng.{uuid}:testpass@127.0.0.1:9200 --concurrency 4 --account-retries 3 --captcha-solver ddddocr --captcha-ai-attempts 3 --no-captcha-ai-fallback-manual --captcha-drag-backend os --captcha-drag-strategy fast_quadratic --sync-qwen2api --qwen2api-base-url http://127.0.0.1:9999 --qwen2api-admin-key secret --qwen2api-timeout 45 --verbose --strict" in without_log_arg(second.stdout)


def test_start_ps1_plan_dry_run_input_sets_concurrency_without_verbose(tmp_path):
    config_path = tmp_path / "start-config.json"
    result = run_start("\n\n\n\n\n4\n\nn\n\n", config_path)

    assert_success(result)
    assert "qwenv4.py 1 --email-provider mailtm --concurrency 4 --account-retries 3 --captcha-solver ddddocr --captcha-ai-attempts 3 --no-captcha-ai-fallback-manual --captcha-drag-backend os --captcha-drag-strategy fast_quadratic --strict" in without_log_arg(result.stdout)
    assert "--verbose" not in without_log_arg(result.stdout)


def test_start_ps1_defaults_do_not_enable_qwen2api_sync(tmp_path):
    config_path = tmp_path / "start-config.json"
    result = run_start("\n\n\n\n\n\n\n\n\n", config_path)

    assert_success(result)
    assert "--sync-qwen2api" not in without_log_arg(result.stdout)
    assert "--qwen2api-admin-key" not in without_log_arg(result.stdout)
    assert "--captcha-solver ddddocr" in without_log_arg(result.stdout)
    assert "--browser-proxy" not in without_log_arg(result.stdout)


def test_start_ps1_saves_and_reuses_qwen2api_sync_values(tmp_path):
    config_path = tmp_path / "start-config.json"

    first = run_start("1\n\n\n\n\n1\n\ny\nhttp://127.0.0.1:9999\nsecret\n45\n\n", config_path)
    assert_success(first)
    assert "--sync-qwen2api --qwen2api-base-url http://127.0.0.1:9999 --qwen2api-admin-key secret --qwen2api-timeout 45" in without_log_arg(first.stdout)
    saved = json.loads(config_path.read_text(encoding="utf-8"))
    assert saved["sync_qwen2api"] is True
    assert saved["qwen2api_base_url"] == "http://127.0.0.1:9999"
    assert saved["qwen2api_admin_key"] == "secret"
    assert saved["qwen2api_timeout"] == 45

    second = run_start("\n\n\n\n\n\n\n\n\n\n\n", config_path)
    assert_success(second)
    assert "--sync-qwen2api --qwen2api-base-url http://127.0.0.1:9999 --qwen2api-admin-key secret --qwen2api-timeout 45" in without_log_arg(second.stdout)


def test_start_ps1_skip_dependency_install_executes_python_launcher_with_args(tmp_path):
    temp_script = tmp_path / "start.ps1"
    temp_config = tmp_path / "start-config.json"
    marker = tmp_path / "launched.json"
    temp_script.write_text(START_SCRIPT.read_text(encoding="utf-8"), encoding="utf-8")
    (tmp_path / "qwenv4.py").write_text(
        """
import json
import os
import re
import sys
from pathlib import Path
Path('launched.json').write_text(json.dumps({'argv': sys.argv[1:]}, ensure_ascii=False), encoding='utf-8')
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
        input="1\n\n\n\n\n1\n\nn\n\n",
        text=True,
        capture_output=True,
        cwd=tmp_path,
        timeout=30,
    )

    assert_success(result)
    launched = json.loads(marker.read_text(encoding="utf-8"))
    log_index = launched["argv"].index("--log-file")
    assert launched["argv"][log_index + 1].endswith("logs\\qwenv4.log")
    argv_without_log = launched["argv"][:log_index] + launched["argv"][log_index + 2:]
    assert argv_without_log == [
        "1",
        "--email-provider",
        "mailtm",
            "--concurrency",
            "1",
            "--account-retries",
            "3",
            "--captcha-solver",
            "ddddocr",
            "--captcha-ai-attempts",
            "3",
            "--no-captcha-ai-fallback-manual",
            "--captcha-drag-backend",
            "os",
            "--captcha-drag-strategy",
            "fast_quadratic",
                    "--strict",
        ]


def test_start_ps1_streams_qwenv4_stdout_and_preserves_exit_code(tmp_path):
    temp_script = tmp_path / "start.ps1"
    temp_config = tmp_path / "start-config.json"
    temp_script.write_text(START_SCRIPT.read_text(encoding="utf-8"), encoding="utf-8")
    (tmp_path / "qwenv4.py").write_text(
        """
import sys
print("主流程 stdout 日志")
print("邮箱服务 stderr 日志", file=sys.stderr)
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
        input="1\n\n\n\n\n1\n\nn\n\n",
        text=True,
        capture_output=True,
        cwd=tmp_path,
        timeout=30,
    )

    assert result.returncode == 7, result.stdout + result.stderr
    assert "主流程 stdout 日志" in without_log_arg(result.stdout)
    assert "邮箱服务 stderr 日志" in result.stderr


def test_start_ps1_interrupt_cleanup_selftest_notifies_python_before_kill():
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
    assert "等待 Python 停止新任务并关闭当前 Playwright 浏览器" in without_log_arg(result.stdout)
    assert "Python 已完成中断清理" in without_log_arg(result.stdout)


def test_start_script_adds_log_file_to_python_command(tmp_path):
    config_path = tmp_path / "start-config.json"
    result = run_start("\n\n\n\n\n\n\n\n\n", config_path)

    assert_success(result)
    assert "--log-file" in result.stdout
    assert "logs/qwenv4.log" in result.stdout.replace("\\", "/")
    assert "qwenv4.py" in without_log_arg(result.stdout)

def test_start_script_ai_mode_requires_api_key(tmp_path):
    config_path = tmp_path / "start-config.json"
    result = run_start("\n\n\n\n\n\n2\n600\nn\n\n", config_path)

    assert result.returncode == 1
    assert "选择滑块AI模式需要先设置环境变量 CAPTCHA_AI_API_KEY" in (result.stdout + result.stderr)


def test_start_script_ai_mode_saves_timeout_and_uses_key(tmp_path):
    config_path = tmp_path / "start-config.json"
    result = run_start("\n\n\n\n\n\n2\n123\nn\n\n", config_path, captcha_ai_key="sk-test")

    assert_success(result)
    command = without_log_arg(result.stdout)
    assert "qwenv4.py 1 --email-provider mailtm --concurrency 1 --account-retries 3 --captcha-solver ai --captcha-timeout 123 --captcha-ai-attempts 3 --no-captcha-ai-fallback-manual --captcha-drag-backend os --captcha-drag-strategy fast_quadratic --strict" in command
    saved = json.loads(config_path.read_text(encoding="utf-8"))
    assert saved["captcha_solver"] == "ai"
    assert saved["captcha_timeout"] == 123


def test_start_script_manual_mode_uses_timeout(tmp_path):
    config_path = tmp_path / "start-config.json"
    result = run_start("\n\n\n\n\n\n3\n456\nn\n\n", config_path)

    assert_success(result)
    command = without_log_arg(result.stdout)
    assert "qwenv4.py 1 --email-provider mailtm --concurrency 1 --account-retries 3 --captcha-solver manual --captcha-timeout 456 --strict" in command
    assert "--captcha-ai-attempts" not in command





