import json
import subprocess
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
START_SCRIPT = ROOT / "start.sh"


class StartShTest(unittest.TestCase):
    """验证 macOS/Linux 统一启动脚本的两种浏览器模式。"""

    def run_start(self, mode: str, config_path: Path) -> subprocess.CompletedProcess[str]:
        """以默认交互输入执行一次 dry-run。"""
        return subprocess.run(
            [
                "bash",
                str(START_SCRIPT),
                "--mode",
                mode,
                "--dry-run",
                "--skip-dependency-install",
                "--config-path",
                str(config_path),
            ],
            input="\n" * 9,
            text=True,
            capture_output=True,
            cwd=ROOT,
            timeout=30,
        )

    def test_playwright_mode_uses_matching_entry_and_log(self) -> None:
        """Playwright 模式应启动 qwenv4.py 并保存通用配置。"""
        with tempfile.TemporaryDirectory() as temp_dir:
            config_path = Path(temp_dir) / "start.json"
            result = self.run_start("playwright", config_path)

            self.assertEqual(0, result.returncode, result.stdout + result.stderr)
            self.assertIn("qwenv4.py", result.stdout)
            self.assertIn("logs/qwenv4.log", result.stdout)
            self.assertIn("--captcha-drag-backend playwright", result.stdout)
            self.assertEqual("mailtm", json.loads(config_path.read_text())["email_provider"])

    def test_camoufox_mode_uses_matching_entry_and_log(self) -> None:
        """Camoufox 模式应启动专用入口并使用独立日志。"""
        with tempfile.TemporaryDirectory() as temp_dir:
            config_path = Path(temp_dir) / "start-camoufox.json"
            result = self.run_start("camoufox", config_path)

            self.assertEqual(0, result.returncode, result.stdout + result.stderr)
            self.assertIn("qwenv4_camoufox.py", result.stdout)
            self.assertIn("logs/qwenv4-camoufox.log", result.stdout)
            self.assertEqual(1, json.loads(config_path.read_text())["concurrency"])


if __name__ == "__main__":
    unittest.main()
