
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FILES = [
    ROOT / "qwenv4.py",
    ROOT / "qwenv4_camoufox.py",
    ROOT / "start.ps1",
    ROOT / "start_camoufox.ps1",
    *sorted((ROOT / "email_providers").glob("*.py")),
]

FORBIDDEN_USER_VISIBLE_ENGLISH = [
    "No proxies found",
    "Loaded ",
    "proxy(ies)",
    "Running without proxy",
    "Invalid proxy format",
    "Could not detect IP",
    "Saved to",
    "Opening inbox",
    "Waiting for verification email",
    "Inbox has",
    "Found Qwen email",
    "Click error",
    "Link extract retry",
    "Verify link",
    "First link found",
    "Inbox check error",
    "Still waiting",
    "Registering Qwen account",
    "Registration submitted",
    "Registration error",
    "Registration likely submitted",
    "Unexpected state",
    "Form likely submitted",
    "Form fill error",
    "How many accounts to create",
    "Invalid number",
    "Creating ",
    "Email provider:",
    "API proxy:",
    "Strict mode:",
    "Using browser proxy",
    "No browser proxy",
    "Browser launch error",
    "Current IP",
    "Email:    ",
    "Name:     ",
    "Password: ",
    "registration failed, skipping",
    "Opening verification link",
    "Extracting authentication tokens",
    "Token extracted",
    "Token not found",
    "Verify result",
    "verified and saved",
    "Email provider error",
    "skipping account",
    "Done:",
    "Results saved to",
    "text format",
    "JSON array format",
    "邮箱 Provider:",
    "provider 尚未创建邮箱",
]


def test_user_visible_logs_are_chinese():
    violations = []
    for path in FILES:
        text = path.read_text(encoding="utf-8")
        for snippet in FORBIDDEN_USER_VISIBLE_ENGLISH:
            if snippet in text:
                violations.append(f"{path.relative_to(ROOT)} contains {snippet!r}")

    assert not violations, "\n".join(violations)
