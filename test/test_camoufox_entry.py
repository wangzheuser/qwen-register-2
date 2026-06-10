import types
import sys
import threading
import time
from contextlib import contextmanager

import portalocker
import pytest

import qwenv4


def test_camoufox_entry_imports_and_reuses_qwenv4_parser():
    import qwenv4_camoufox

    args = qwenv4_camoufox.parse_args(["1"])

    assert args.email_provider == "mailtm"
    assert args.concurrency == 1
    assert args.captcha_timeout == 600
    assert qwenv4_camoufox.parse_args is qwenv4.parse_args

def test_camoufox_main_prints_total_duration_after_average(monkeypatch, capsys):
    from types import SimpleNamespace
    import qwenv4_camoufox

    args = SimpleNamespace(
        camoufox_worker=False,
        count=2,
        email_provider="mailtm",
        api_proxy=None,
        browser_proxy=None,
        concurrency=1,
        captcha_solver="manual",
        captcha_timeout=600,
        captcha_ai_model="model",
        captcha_record_trace=False,
        captcha_replay_trace=None,
        captcha_drag_backend="playwright",
        captcha_drag_strategy="human",
        captcha_target_right_bias=None,
        captcha_callback_bypass=False,
        captcha_force_verify_success=False,
        sync_qwen2api=False,
        qwen2api_base_url="http://127.0.0.1",
        qwen2api_timeout=30,
        log_file="",
    )
    monkeypatch.setattr(qwenv4_camoufox, "parse_args", lambda argv=None: args)
    monkeypatch.setattr(qwenv4_camoufox, "enable_run_logging", lambda *a, **k: SimpleNamespace(path="run.log"))
    monkeypatch.setattr(qwenv4_camoufox, "close_run_logging", lambda state: None)
    monkeypatch.setattr(qwenv4_camoufox, "start_parent_stop_file_watcher", lambda: None)
    monkeypatch.setattr(qwenv4_camoufox, "submit_account_futures", lambda *a, **k: [])
    monkeypatch.setattr(
        qwenv4_camoufox,
        "collect_account_futures",
        lambda futures, total: qwenv4_camoufox.AccountRunSummary(success_count=2, success_durations=[61.0, 63.0]),
    )
    ticks = iter([10.0, 113.0])
    monkeypatch.setattr(qwenv4_camoufox.time, "perf_counter", lambda: next(ticks))

    qwenv4_camoufox.main()

    lines = capsys.readouterr().out.splitlines()
    average_index = lines.index("⏱️ 平均成功耗时: 1分02秒")
    assert lines[average_index + 1] == "⏳ 总耗时: 1分43秒"


def test_camoufox_account_timeout_default_allows_slow_verification_cleanup(monkeypatch):
    import qwenv4_camoufox

    monkeypatch.delenv("CAMOUFOX_ACCOUNT_TIMEOUT", raising=False)

    args = qwenv4_camoufox.parse_args(["1"])

    assert args.camoufox_account_timeout == 180


def test_camoufox_worker_uses_camoufox_without_chromium_args(monkeypatch, tmp_path):
    import qwenv4_camoufox

    seen = {}

    class DummyPage:
        def __init__(self, context=None):
            self.context = context

        def goto(self, *args, **kwargs):
            pass

        def screenshot(self, *args, **kwargs):
            pass

        def evaluate(self, *args, **kwargs):
            return "验证完成"

        def close(self):
            pass

    class DummyContext:
        def close(self):
            pass

    class DummyBrowser:
        def __init__(self):
            self.context = DummyContext()

        def new_page(self, **kwargs):
            seen["page_kwargs"] = kwargs
            return DummyPage(self.context)

        def close(self):
            pass

    class DummyCamoufox:
        def __init__(self, **kwargs):
            seen["launch_kwargs"] = kwargs
            self.browser = DummyBrowser()

        def __enter__(self):
            return self.browser

        def __exit__(self, exc_type, exc, tb):
            pass

    class DummyProvider:
        def __init__(self, *args, **kwargs):
            self.email = "user@example.com"

        def create_inbox(self):
            return self.email

        def get_activation_link(self, timeout=300):
            return "https://studio.qwen.ai/auth/verify?token=test"

        def cleanup(self):
            pass

    args = types.SimpleNamespace(
        email_provider="mailtm",
        verbose=False,
        api_proxy=None,
        browser_proxy="http://127.0.0.1:7890",
        captcha_timeout=600,
        sync_qwen2api=False,
        qwen2api_base_url="http://127.0.0.1:7860",
        qwen2api_admin_key="admin",
        qwen2api_timeout=30,
    )

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(qwenv4_camoufox, "Camoufox", DummyCamoufox)
    monkeypatch.setattr(qwenv4_camoufox.EmailProviderFactory, "create", lambda **_kwargs: DummyProvider())
    monkeypatch.setattr(qwenv4_camoufox.UsedEmailsStore, "claim", lambda self, email: True)
    monkeypatch.setattr(qwenv4_camoufox, "get_current_ip", lambda: ("127.0.0.1", "ZZ"))
    monkeypatch.setattr(qwenv4_camoufox, "gen_first_name", lambda: "Test")
    monkeypatch.setattr(qwenv4_camoufox, "gen_password", lambda: "Password1!")
    monkeypatch.setattr(qwenv4_camoufox, "gen_name", lambda first_name: f"{first_name} User")
    monkeypatch.setattr(qwenv4_camoufox, "register_qwen", lambda *args, **kwargs: True)
    monkeypatch.setattr(qwenv4_camoufox, "wait_for_token_extraction", lambda _page, timeout=5.0: {
        "token": "token-1",
        "active_token": None,
        "device_id": None,
        "user_role": "user",
    })
    monkeypatch.setattr(qwenv4_camoufox, "save_account", lambda **_kwargs: None)
    monkeypatch.setattr(qwenv4_camoufox, "maybe_sync_account_to_qwen2api", lambda **_kwargs: None)
    monkeypatch.setattr(qwenv4_camoufox.time, "sleep", lambda _seconds: None)

    result = qwenv4_camoufox.run_single_account(1, 1, args, None)
    assert result.success is True
    assert result.duration_seconds is not None
    assert result.duration_seconds >= 0

    launch_kwargs = seen["launch_kwargs"]
    assert launch_kwargs["headless"] is False
    assert launch_kwargs["proxy"] == {"server": "http://127.0.0.1:7890"}
    assert launch_kwargs["window"] == (1280, 800)
    assert launch_kwargs["humanize"] is True
    assert launch_kwargs["os"] == "windows"
    assert "--disable-blink-features=AutomationControlled" not in launch_kwargs.get("args", [])
    assert "--no-sandbox" not in launch_kwargs.get("args", [])
    assert "user_agent" not in seen["page_kwargs"]
    assert seen["page_kwargs"]["viewport"] == {"width": 1280, "height": 800}


def test_camoufox_worker_prints_stage_timing_summary(monkeypatch, tmp_path, capsys):
    import types
    import qwenv4_camoufox

    class DummyPage:
        def __init__(self, context):
            self.context = context

        def goto(self, *args, **kwargs):
            pass

        def screenshot(self, *args, **kwargs):
            pass

        def evaluate(self, *args, **kwargs):
            return "验证完成"

        def close(self):
            pass

    class DummyContext:
        def close(self):
            pass

    class DummyBrowser:
        def new_page(self, **kwargs):
            return DummyPage(DummyContext())

        def close(self):
            pass

    class DummyCamoufox:
        def __init__(self, **kwargs):
            pass

        def __enter__(self):
            return DummyBrowser()

        def __exit__(self, exc_type, exc, tb):
            pass

    class DummyProvider:
        def create_inbox(self):
            return "user@example.com"

        def get_activation_link(self, timeout=300):
            return "https://studio.qwen.ai/auth/verify?token=test"

        def cleanup(self):
            pass

    args = types.SimpleNamespace(
        email_provider="mailtm",
        verbose=False,
        api_proxy=None,
        browser_proxy="http://127.0.0.1:7890",
        captcha_timeout=600,
        sync_qwen2api=False,
        qwen2api_base_url="http://127.0.0.1:7860",
        qwen2api_admin_key="admin",
        qwen2api_timeout=30,
    )

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(qwenv4_camoufox, "Camoufox", DummyCamoufox)
    monkeypatch.setattr(qwenv4_camoufox.EmailProviderFactory, "create", lambda **_kwargs: DummyProvider())
    monkeypatch.setattr(qwenv4_camoufox.UsedEmailsStore, "claim", lambda self, email: True)
    monkeypatch.setattr(qwenv4_camoufox, "register_qwen", lambda *args, **kwargs: True)
    monkeypatch.setattr(qwenv4_camoufox, "wait_for_token_extraction", lambda _page, timeout=5.0: {
        "token": "token-1",
        "active_token": None,
        "device_id": None,
        "user_role": "user",
    })
    monkeypatch.setattr(qwenv4_camoufox, "save_account", lambda **_kwargs: None)

    assert qwenv4_camoufox.run_single_account(1, 1, args, None).success is True
    out = capsys.readouterr().out
    assert "[账号 1/1] 耗时拆分" in out
    assert "邮箱创建" in out
    assert "滑块/注册" in out
    assert "邮箱激活" in out
    assert "验证与令牌" in out

def test_camoufox_worker_returns_false_when_browser_launch_fails(monkeypatch, capsys):
    import qwenv4_camoufox

    class BrokenCamoufox:
        def __init__(self, **kwargs):
            raise RuntimeError("camoufox missing")

    args = types.SimpleNamespace(
        email_provider="mailtm",
        verbose=False,
        api_proxy=None,
        browser_proxy="http://127.0.0.1:7890",
        captcha_timeout=600,
        sync_qwen2api=False,
        qwen2api_base_url="http://127.0.0.1:7860",
        qwen2api_admin_key="admin",
        qwen2api_timeout=30,
    )

    monkeypatch.setattr(qwenv4_camoufox, "Camoufox", BrokenCamoufox)

    result = qwenv4_camoufox.run_single_account(1, 1, args, None)
    assert result.success is False
    assert result.duration_seconds is not None
    assert "python -m camoufox fetch" in capsys.readouterr().out


def test_camoufox_worker_retries_failed_attempts(monkeypatch, capsys):
    import qwenv4_camoufox

    launch_attempts = {"count": 0}

    class BrokenCamoufox:
        def __init__(self, **kwargs):
            launch_attempts["count"] += 1
            raise RuntimeError("camoufox missing")

    args = types.SimpleNamespace(
        email_provider="mailtm",
        verbose=False,
        api_proxy=None,
        browser_proxy="http://127.0.0.1:7890",
        captcha_timeout=600,
        sync_qwen2api=False,
        qwen2api_base_url="http://127.0.0.1:7860",
        qwen2api_admin_key="admin",
        qwen2api_timeout=30,
        account_retries=2,
    )

    monkeypatch.setattr(qwenv4_camoufox, "Camoufox", BrokenCamoufox)
    monkeypatch.setattr(qwenv4_camoufox, "sleep_interruptible", lambda _seconds: True)

    result = qwenv4_camoufox.run_single_account(1, 1, args, None)

    assert result.success is False
    assert launch_attempts["count"] == 2
    assert "第 2/2 次尝试" in capsys.readouterr().out


def test_camoufox_worker_returns_false_when_browser_enter_fails(monkeypatch, capsys):
    import qwenv4_camoufox

    class BrokenEnterCamoufox:
        def __init__(self, **kwargs):
            pass

        def __enter__(self):
            raise RuntimeError("camoufox browser missing")

        def __exit__(self, exc_type, exc, tb):
            pass

    args = types.SimpleNamespace(
        email_provider="mailtm",
        verbose=False,
        api_proxy=None,
        browser_proxy="http://127.0.0.1:7890",
        captcha_timeout=600,
        sync_qwen2api=False,
        qwen2api_base_url="http://127.0.0.1:7860",
        qwen2api_admin_key="admin",
        qwen2api_timeout=30,
    )

    monkeypatch.setattr(qwenv4_camoufox, "Camoufox", BrokenEnterCamoufox)

    result = qwenv4_camoufox.run_single_account(1, 1, args, None)
    assert result.success is False
    assert result.duration_seconds is not None
    assert "python -m camoufox fetch" in capsys.readouterr().out


def test_camoufox_browser_enter_runs_inside_foreground_lock(monkeypatch, tmp_path):
    import qwenv4_camoufox

    lock_state = {"active": False, "enter_inside_lock": False}

    @contextmanager
    def fake_foreground_lock(**_kwargs):
        lock_state["active"] = True
        try:
            yield
        finally:
            lock_state["active"] = False

    class DummyPage:
        def __init__(self, context=None):
            self.context = context

        def goto(self, *args, **kwargs):
            pass

        def screenshot(self, *args, **kwargs):
            pass

        def evaluate(self, *args, **kwargs):
            return "验证完成"

        def close(self):
            pass

    class DummyContext:
        def close(self):
            pass

    class DummyBrowser:
        def __init__(self):
            self.context = DummyContext()

        def new_page(self, **kwargs):
            return DummyPage(self.context)

    class DummyCamoufox:
        def __init__(self, **kwargs):
            pass

        def __enter__(self):
            lock_state["enter_inside_lock"] = lock_state["active"]
            return DummyBrowser()

        def __exit__(self, exc_type, exc, tb):
            pass

    class DummyProvider:
        def create_inbox(self):
            return "user@example.com"

        def get_activation_link(self, timeout=300):
            return "https://studio.qwen.ai/auth/verify?token=test"

        def cleanup(self):
            pass

    args = types.SimpleNamespace(
        email_provider="mailtm",
        verbose=False,
        api_proxy=None,
        browser_proxy="http://127.0.0.1:7890",
        captcha_timeout=600,
        sync_qwen2api=False,
        qwen2api_base_url="http://127.0.0.1:7860",
        qwen2api_admin_key="admin",
        qwen2api_timeout=30,
    )

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(qwenv4_camoufox, "acquire_foreground_window_lock", fake_foreground_lock)
    monkeypatch.setattr(qwenv4_camoufox, "Camoufox", DummyCamoufox)
    monkeypatch.setattr(qwenv4_camoufox.EmailProviderFactory, "create", lambda **_kwargs: DummyProvider())
    monkeypatch.setattr(qwenv4_camoufox.UsedEmailsStore, "claim", lambda self, email: True)
    monkeypatch.setattr(qwenv4_camoufox, "register_qwen", lambda *args, **kwargs: True)
    monkeypatch.setattr(qwenv4_camoufox, "wait_for_token_extraction", lambda _page, timeout=5.0: {
        "token": "token-1",
        "active_token": None,
        "device_id": None,
        "user_role": "user",
    })
    monkeypatch.setattr(qwenv4_camoufox, "save_account", lambda **_kwargs: None)
    monkeypatch.setattr(qwenv4_camoufox, "maybe_sync_account_to_qwen2api", lambda **_kwargs: None)

    result = qwenv4_camoufox.run_single_account(1, 1, args, None)

    assert result.success is True
    assert lock_state["enter_inside_lock"] is True

def test_camoufox_worker_runs_browser_api_inside_runtime_lock(monkeypatch, tmp_path):
    import qwenv4_camoufox

    lock_state = {
        "runtime_active": False,
        "enter_inside_runtime": False,
        "new_page_inside_runtime": False,
        "register_inside_runtime": False,
    }

    @contextmanager
    def fake_runtime_lock(label):
        lock_state["runtime_active"] = True
        try:
            yield
        finally:
            lock_state["runtime_active"] = False

    class DummyPage:
        def __init__(self, context=None):
            self.context = context

        def goto(self, *args, **kwargs):
            pass

        def screenshot(self, *args, **kwargs):
            pass

        def evaluate(self, *args, **kwargs):
            return "验证完成"

        def close(self):
            pass

    class DummyContext:
        def close(self):
            pass

    class DummyBrowser:
        def __init__(self):
            self.context = DummyContext()

        def new_page(self, **kwargs):
            lock_state["new_page_inside_runtime"] = lock_state["runtime_active"]
            return DummyPage(self.context)

    class DummyCamoufox:
        def __init__(self, **kwargs):
            pass

        def __enter__(self):
            lock_state["enter_inside_runtime"] = lock_state["runtime_active"]
            return DummyBrowser()

        def __exit__(self, exc_type, exc, tb):
            pass

    class DummyProvider:
        def create_inbox(self):
            return "user@example.com"

        def get_activation_link(self, timeout=300):
            return "https://studio.qwen.ai/auth/verify?token=test"

        def cleanup(self):
            pass

    def fake_register(*args, **kwargs):
        lock_state["register_inside_runtime"] = lock_state["runtime_active"]
        return True

    args = types.SimpleNamespace(
        email_provider="mailtm",
        verbose=False,
        api_proxy=None,
        browser_proxy="http://127.0.0.1:7890",
        captcha_timeout=600,
        sync_qwen2api=False,
        qwen2api_base_url="http://127.0.0.1:7860",
        qwen2api_admin_key="admin",
        qwen2api_timeout=30,
    )

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(qwenv4_camoufox, "acquire_camoufox_runtime_lock", fake_runtime_lock, raising=False)
    monkeypatch.setattr(qwenv4_camoufox, "Camoufox", DummyCamoufox)
    monkeypatch.setattr(qwenv4_camoufox.EmailProviderFactory, "create", lambda **_kwargs: DummyProvider())
    monkeypatch.setattr(qwenv4_camoufox.UsedEmailsStore, "claim", lambda self, email: True)
    monkeypatch.setattr(qwenv4_camoufox, "register_qwen", fake_register)
    monkeypatch.setattr(qwenv4_camoufox, "wait_for_token_extraction", lambda _page, timeout=5.0: {
        "token": "token-1",
        "active_token": None,
        "device_id": None,
        "user_role": "user",
    })
    monkeypatch.setattr(qwenv4_camoufox, "save_account", lambda **_kwargs: None)
    monkeypatch.setattr(qwenv4_camoufox, "maybe_sync_account_to_qwen2api", lambda **_kwargs: None)

    result = qwenv4_camoufox.run_single_account(1, 1, args, None)

    assert result.success is True
    assert lock_state["enter_inside_runtime"] is True
    assert lock_state["new_page_inside_runtime"] is True
    assert lock_state["register_inside_runtime"] is True

def test_camoufox_worker_creates_registration_page_via_browser_new_page(monkeypatch, tmp_path):
    import qwenv4_camoufox

    seen = {"browser_new_page": False, "browser_new_context": False}

    class DummyPage:
        def __init__(self, context):
            self.context = context

        def goto(self, *args, **kwargs):
            pass

        def screenshot(self, *args, **kwargs):
            pass

        def evaluate(self, *args, **kwargs):
            return "验证完成"

        def close(self):
            pass

    class DummyContext:
        def close(self):
            pass

    class DummyBrowser:
        def new_context(self, **kwargs):
            seen["browser_new_context"] = True
            raise AssertionError("Camoufox worker should avoid browser.new_context().new_page()")

        def new_page(self, **kwargs):
            seen["browser_new_page"] = kwargs
            return DummyPage(DummyContext())

    class DummyCamoufox:
        def __init__(self, **kwargs):
            pass

        def __enter__(self):
            return DummyBrowser()

        def __exit__(self, exc_type, exc, tb):
            pass

    class DummyProvider:
        def create_inbox(self):
            return "user@example.com"

        def get_activation_link(self, timeout=300):
            return "https://studio.qwen.ai/auth/verify?token=test"

        def cleanup(self):
            pass

    args = types.SimpleNamespace(
        email_provider="mailtm",
        verbose=False,
        api_proxy=None,
        browser_proxy="http://127.0.0.1:7890",
        captcha_timeout=600,
        sync_qwen2api=False,
        qwen2api_base_url="http://127.0.0.1:7860",
        qwen2api_admin_key="admin",
        qwen2api_timeout=30,
    )

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(qwenv4_camoufox, "Camoufox", DummyCamoufox)
    monkeypatch.setattr(qwenv4_camoufox.EmailProviderFactory, "create", lambda **_kwargs: DummyProvider())
    monkeypatch.setattr(qwenv4_camoufox.UsedEmailsStore, "claim", lambda self, email: True)
    monkeypatch.setattr(qwenv4_camoufox, "register_qwen", lambda *args, **kwargs: True)
    monkeypatch.setattr(qwenv4_camoufox, "wait_for_token_extraction", lambda _page, timeout=5.0: {
        "token": "token-1",
        "active_token": None,
        "device_id": None,
        "user_role": "user",
    })
    monkeypatch.setattr(qwenv4_camoufox, "save_account", lambda **_kwargs: None)
    monkeypatch.setattr(qwenv4_camoufox, "maybe_sync_account_to_qwen2api", lambda **_kwargs: None)

    result = qwenv4_camoufox.run_single_account(1, 1, args, None)

    assert result.success is True
    assert seen["browser_new_context"] is False
    assert seen["browser_new_page"] == {"viewport": {"width": 1280, "height": 800}}


def test_camoufox_new_page_does_not_hold_foreground_window_lock(monkeypatch, tmp_path):
    import qwenv4_camoufox

    lock_state = {"active": False, "new_page_inside_foreground": None}

    @contextmanager
    def fake_foreground_lock(**_kwargs):
        lock_state["active"] = True
        try:
            yield
        finally:
            lock_state["active"] = False

    class DummyPage:
        def __init__(self, context):
            self.context = context

        def goto(self, *args, **kwargs):
            pass

        def screenshot(self, *args, **kwargs):
            pass

        def evaluate(self, *args, **kwargs):
            return "验证完成"

        def close(self):
            pass

    class DummyContext:
        def close(self):
            pass

    class DummyBrowser:
        def new_page(self, **kwargs):
            lock_state["new_page_inside_foreground"] = lock_state["active"]
            return DummyPage(DummyContext())

    class DummyCamoufox:
        def __init__(self, **kwargs):
            pass

        def __enter__(self):
            return DummyBrowser()

        def __exit__(self, exc_type, exc, tb):
            pass

    class DummyProvider:
        def create_inbox(self):
            return "user@example.com"

        def get_activation_link(self, timeout=300):
            return "https://studio.qwen.ai/auth/verify?token=test"

        def cleanup(self):
            pass

    args = types.SimpleNamespace(
        email_provider="mailtm",
        verbose=False,
        api_proxy=None,
        browser_proxy="http://127.0.0.1:7890",
        captcha_timeout=600,
        sync_qwen2api=False,
        qwen2api_base_url="http://127.0.0.1:7860",
        qwen2api_admin_key="admin",
        qwen2api_timeout=30,
    )

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(qwenv4_camoufox, "acquire_foreground_window_lock", fake_foreground_lock)
    monkeypatch.setattr(qwenv4_camoufox, "Camoufox", DummyCamoufox)
    monkeypatch.setattr(qwenv4_camoufox.EmailProviderFactory, "create", lambda **_kwargs: DummyProvider())
    monkeypatch.setattr(qwenv4_camoufox.UsedEmailsStore, "claim", lambda self, email: True)
    monkeypatch.setattr(qwenv4_camoufox, "register_qwen", lambda *args, **kwargs: True)
    monkeypatch.setattr(qwenv4_camoufox, "wait_for_token_extraction", lambda _page, timeout=5.0: {
        "token": "token-1",
        "active_token": None,
        "device_id": None,
        "user_role": "user",
    })
    monkeypatch.setattr(qwenv4_camoufox, "save_account", lambda **_kwargs: None)
    monkeypatch.setattr(qwenv4_camoufox, "maybe_sync_account_to_qwen2api", lambda **_kwargs: None)

    result = qwenv4_camoufox.run_single_account(1, 1, args, None)

    assert result.success is True
    assert lock_state["new_page_inside_foreground"] is False


def test_camoufox_worker_defers_force_verify_route_to_register_flow(monkeypatch, tmp_path):
    import qwenv4_camoufox

    installed = {"callback_probe": False, "verify_route": False}

    class DummyPage:
        def __init__(self, context):
            self.context = context

        def goto(self, *args, **kwargs):
            pass

        def screenshot(self, *args, **kwargs):
            pass

        def evaluate(self, *args, **kwargs):
            return "验证完成"

        def close(self):
            pass

    class DummyContext:
        def close(self):
            pass

    class DummyBrowser:
        def new_page(self, **kwargs):
            return DummyPage(DummyContext())

    class DummyCamoufox:
        def __init__(self, **kwargs):
            pass

        def __enter__(self):
            return DummyBrowser()

        def __exit__(self, exc_type, exc, tb):
            pass

    class DummyProvider:
        def create_inbox(self):
            return "user@example.com"

        def get_activation_link(self, timeout=300):
            return "https://studio.qwen.ai/auth/verify?token=test"

        def cleanup(self):
            pass

    args = types.SimpleNamespace(
        email_provider="mailtm",
        verbose=False,
        api_proxy=None,
        browser_proxy="http://127.0.0.1:7890",
        captcha_timeout=600,
        captcha_force_verify_success=True,
        sync_qwen2api=False,
        qwen2api_base_url="http://127.0.0.1:7860",
        qwen2api_admin_key="admin",
        qwen2api_timeout=30,
    )

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(qwenv4_camoufox, "Camoufox", DummyCamoufox)
    monkeypatch.setattr(qwenv4_camoufox.EmailProviderFactory, "create", lambda **_kwargs: DummyProvider())
    monkeypatch.setattr(qwenv4_camoufox.UsedEmailsStore, "claim", lambda self, email: True)
    monkeypatch.setattr(qwenv4_camoufox, "register_qwen", lambda *args, **kwargs: True)
    monkeypatch.setattr(qwenv4_camoufox, "wait_for_token_extraction", lambda _page, timeout=5.0: {
        "token": "token-1",
        "active_token": None,
        "device_id": None,
        "user_role": "user",
    })
    monkeypatch.setattr(qwenv4_camoufox, "save_account", lambda **_kwargs: None)
    monkeypatch.setattr(qwenv4_camoufox, "maybe_sync_account_to_qwen2api", lambda **_kwargs: None)
    monkeypatch.setattr(
        qwenv4_camoufox,
        "install_aliyun_callback_probe",
        lambda _page: installed.__setitem__("callback_probe", True),
    )
    monkeypatch.setattr(
        qwenv4_camoufox,
        "install_aliyun_verify_success_route",
        lambda _page: installed.__setitem__("verify_route", True),
    )

    result = qwenv4_camoufox.run_single_account(1, 1, args, None)

    assert result.success is True
    assert installed == {"callback_probe": True, "verify_route": False}



def test_camoufox_worker_saves_account_before_noncritical_screenshot(monkeypatch, tmp_path):
    import qwenv4_camoufox

    events = []

    class DummyPage:
        def __init__(self, context):
            self.context = context

        def goto(self, *args, **kwargs):
            pass

        def screenshot(self, *args, **kwargs):
            events.append("screenshot")
            raise RuntimeError("slow screenshot should be noncritical")

        def evaluate(self, *args, **kwargs):
            return "验证完成"

        def close(self):
            pass

    class DummyContext:
        def close(self):
            pass

    class DummyBrowser:
        def new_page(self, **kwargs):
            return DummyPage(DummyContext())

    class DummyCamoufox:
        def __init__(self, **kwargs):
            pass

        def __enter__(self):
            return DummyBrowser()

        def __exit__(self, exc_type, exc, tb):
            pass

    class DummyProvider:
        def create_inbox(self):
            return "user@example.com"

        def get_activation_link(self, timeout=300):
            return "https://studio.qwen.ai/auth/verify?token=test"

        def cleanup(self):
            pass

    args = types.SimpleNamespace(
        email_provider="mailtm",
        verbose=False,
        api_proxy=None,
        browser_proxy="http://127.0.0.1:7890",
        captcha_timeout=600,
        sync_qwen2api=False,
        qwen2api_base_url="http://127.0.0.1:7860",
        qwen2api_admin_key="admin",
        qwen2api_timeout=30,
    )

    def fake_save_account(**_kwargs):
        events.append("save")

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(qwenv4_camoufox, "Camoufox", DummyCamoufox)
    monkeypatch.setattr(qwenv4_camoufox.EmailProviderFactory, "create", lambda **_kwargs: DummyProvider())
    monkeypatch.setattr(qwenv4_camoufox.UsedEmailsStore, "claim", lambda self, email: True)
    monkeypatch.setattr(qwenv4_camoufox, "register_qwen", lambda *args, **kwargs: True)
    monkeypatch.setattr(qwenv4_camoufox, "wait_for_token_extraction", lambda _page, timeout=5.0: {
        "token": "token-1",
        "active_token": None,
        "device_id": None,
        "user_role": "user",
    })
    monkeypatch.setattr(qwenv4_camoufox, "save_account", fake_save_account)
    monkeypatch.setattr(qwenv4_camoufox, "maybe_sync_account_to_qwen2api_async", lambda **_kwargs: events.append("sync"))

    result = qwenv4_camoufox.run_single_account(1, 1, args, None)

    assert result.success is True
    assert events[:2] == ["save", "sync"]
    assert "screenshot" in events[2:]


def test_camoufox_worker_treats_screenshot_failure_as_warning(monkeypatch, tmp_path, capsys):
    import qwenv4_camoufox

    saved = {"called": False}

    class DummyPage:
        def __init__(self, context):
            self.context = context

        def goto(self, *args, **kwargs):
            pass

        def screenshot(self, *args, **kwargs):
            raise RuntimeError("driver closed during screenshot")

        def evaluate(self, *args, **kwargs):
            return "验证完成"

        def close(self):
            pass

    class DummyContext:
        def close(self):
            pass

    class DummyBrowser:
        def new_page(self, **kwargs):
            return DummyPage(DummyContext())

    class DummyCamoufox:
        def __init__(self, **kwargs):
            pass

        def __enter__(self):
            return DummyBrowser()

        def __exit__(self, exc_type, exc, tb):
            pass

    class DummyProvider:
        def create_inbox(self):
            return "user@example.com"

        def get_activation_link(self, timeout=300):
            return "https://studio.qwen.ai/auth/verify?token=test"

        def cleanup(self):
            pass

    def fake_save_account(**_kwargs):
        saved["called"] = True

    args = types.SimpleNamespace(
        email_provider="mailtm",
        verbose=False,
        api_proxy=None,
        browser_proxy="http://127.0.0.1:7890",
        captcha_timeout=600,
        sync_qwen2api=False,
        qwen2api_base_url="http://127.0.0.1:7860",
        qwen2api_admin_key="admin",
        qwen2api_timeout=30,
    )

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(qwenv4_camoufox, "Camoufox", DummyCamoufox)
    monkeypatch.setattr(qwenv4_camoufox.EmailProviderFactory, "create", lambda **_kwargs: DummyProvider())
    monkeypatch.setattr(qwenv4_camoufox.UsedEmailsStore, "claim", lambda self, email: True)
    monkeypatch.setattr(qwenv4_camoufox, "register_qwen", lambda *args, **kwargs: True)
    monkeypatch.setattr(qwenv4_camoufox, "wait_for_token_extraction", lambda _page, timeout=5.0: {
        "token": "token-1",
        "active_token": None,
        "device_id": None,
        "user_role": "user",
    })
    monkeypatch.setattr(qwenv4_camoufox, "save_account", fake_save_account)
    monkeypatch.setattr(qwenv4_camoufox, "maybe_sync_account_to_qwen2api", lambda **_kwargs: None)

    result = qwenv4_camoufox.run_single_account(1, 1, args, None)

    assert result.success is True
    assert saved["called"] is True
    assert "截图失败" in capsys.readouterr().out


def test_camoufox_worker_command_isolated_child_process():
    import qwenv4_camoufox

    args = qwenv4_camoufox.parse_args([
        "10",
        "--email-provider",
        "mailtm",
        "--api-proxy",
        "http://127.0.0.1:7890",
        "--browser-proxy",
        "http://user.{uuid}:pass@127.0.0.1:9200",
        "--concurrency",
        "3",
        "--account-retries",
        "3",
        "--captcha-solver",
        "ddddocr",
        "--captcha-drag-backend",
        "os",
        "--captcha-drag-strategy",
        "fast_quadratic",
        "--sync-qwen2api",
        "--qwen2api-base-url",
        "https://qwen2api.example.test",
        "--qwen2api-admin-key",
        "secret",
        "--qwen2api-timeout",
        "30",
        "--verbose",
        "--strict",
    ])

    command = qwenv4_camoufox.build_camoufox_worker_command(args, account_index=2, total_accounts=10)

    assert command[0] == sys.executable
    assert command[2] == "1"
    assert "--camoufox-worker" in command
    assert command[command.index("--account-index") + 1] == "2"
    assert command[command.index("--total-accounts") + 1] == "10"
    assert command[command.index("--concurrency") + 1] == "1"
    assert command[command.index("--browser-proxy") + 1] == "http://user.{uuid}:pass@127.0.0.1:9200"
    assert "--sync-qwen2api" in command
    assert command[command.index("--qwen2api-admin-key") + 1] == "secret"


def test_camoufox_subprocess_result_parses_exit_code_and_duration(monkeypatch, capsys):
    import qwenv4_camoufox

    launched = {}
    start_slot = {"waited": False}

    class DummyStdout:
        def __iter__(self):
            return iter(["child line\n"])

        def close(self):
            pass

    class DummyProcess:
        def __init__(self, command, **kwargs):
            assert start_slot["waited"] is True
            launched["command"] = command
            launched["kwargs"] = kwargs
            self.stdout = DummyStdout()
            self.returncode = 0

        def poll(self):
            return self.returncode

        def wait(self, timeout=None):
            return self.returncode

    def fake_wait_for_camoufox_start_slot(*_args, **_kwargs):
        start_slot["waited"] = True
        return True

    monkeypatch.setattr(qwenv4_camoufox.subprocess, "Popen", DummyProcess)
    monkeypatch.setattr(
        qwenv4_camoufox,
        "wait_for_camoufox_start_slot",
        fake_wait_for_camoufox_start_slot,
        raising=False,
    )

    args = qwenv4_camoufox.parse_args(["10", "--concurrency", "3"])
    result = qwenv4_camoufox.run_account_subprocess(1, 10, args, timeout_seconds=30)

    assert result.success is True
    assert result.duration_seconds is not None
    assert "--camoufox-worker" in launched["command"]
    assert launched["kwargs"]["stdout"] == qwenv4_camoufox.subprocess.PIPE
    assert "PYTHONUNBUFFERED" in launched["kwargs"]["env"]
    assert "child line" in capsys.readouterr().out


def test_camoufox_subprocess_uses_worker_reported_success_duration(monkeypatch, capsys):
    import qwenv4_camoufox

    class DummyStdout:
        def __iter__(self):
            return iter([
                "before marker\n",
                '__QWENV4_CAMOUFOX_WORKER_RESULT__ {"success": true, "duration_seconds": 12.5}\n',
                "after marker\n",
            ])

    class DummyProcess:
        def __init__(self, *_args, **_kwargs):
            self.stdout = DummyStdout()
            self.returncode = 0

        def poll(self):
            return self.returncode

    monkeypatch.setattr(qwenv4_camoufox.subprocess, "Popen", DummyProcess)

    args = qwenv4_camoufox.parse_args(["10", "--concurrency", "3"])
    result = qwenv4_camoufox.run_account_subprocess(1, 10, args, timeout_seconds=30)

    output = capsys.readouterr().out
    assert result.success is True
    assert result.duration_seconds == 12.5
    assert "before marker" in output
    assert "after marker" in output
    assert "__QWENV4_CAMOUFOX_WORKER_RESULT__" not in output


def test_camoufox_subprocess_timeout_after_saved_log_returns_success(monkeypatch, capsys):
    import qwenv4_camoufox

    class DummyStdout:
        def __iter__(self):
            return iter([
                "  ✅ [账号 2/10] 已验证并保存！\n",
            ])

    class DummyProcess:
        def __init__(self, *_args, **_kwargs):
            self.stdout = DummyStdout()
            self.returncode = None
            self.pid = 12345

        def poll(self):
            return None

    terminated = {"called": False}

    monkeypatch.setattr(qwenv4_camoufox.subprocess, "Popen", DummyProcess)
    monkeypatch.setattr(qwenv4_camoufox, "_terminate_process_tree", lambda *_args, **_kwargs: terminated.__setitem__("called", True))
    monkeypatch.setattr(qwenv4_camoufox, "sleep_interruptible", lambda _seconds: True)

    args = qwenv4_camoufox.parse_args(["10", "--concurrency", "3"])
    result = qwenv4_camoufox.run_account_subprocess(2, 10, args, timeout_seconds=0.001)

    output = capsys.readouterr().out
    assert result.success is True
    assert result.duration_seconds is not None
    assert terminated["called"] is True
    assert "已验证并保存" in output
    assert "已保存成功但子进程未及时退出" in output



def test_camoufox_subprocess_new_page_stage_has_short_idle_timeout(monkeypatch, capsys):
    import qwenv4_camoufox

    class DummyStdout:
        def __iter__(self):
            return iter([
                "  🔐 [账号 1/10] 新建注册页 已获得 Camoufox 运行时锁 waited=0.000s\n",
            ])

    class DummyProcess:
        def __init__(self, *_args, **_kwargs):
            self.stdout = DummyStdout()
            self.returncode = None
            self.pid = 12345

        def poll(self):
            return None

    terminated = {"called": False}
    times = iter([0.0, 0.0, 6.1, 6.1, 6.1])

    monkeypatch.setenv("CAMOUFOX_STAGE_NEW_PAGE_IDLE_TIMEOUT", "5")
    monkeypatch.setattr(qwenv4_camoufox.subprocess, "Popen", DummyProcess)
    monkeypatch.setattr(qwenv4_camoufox, "_terminate_process_tree", lambda *_args, **_kwargs: terminated.__setitem__("called", True))
    monkeypatch.setattr(qwenv4_camoufox.time, "monotonic", lambda: next(times, 6.1))
    monkeypatch.setattr(qwenv4_camoufox.time, "perf_counter", lambda: 0.0)
    monkeypatch.setattr(qwenv4_camoufox, "sleep_interruptible", lambda _seconds: True)
    monkeypatch.setattr(qwenv4_camoufox, "wait_for_camoufox_start_slot", lambda *args, **kwargs: True)

    args = qwenv4_camoufox.parse_args(["10", "--concurrency", "3", "--account-retries", "1"])
    result = qwenv4_camoufox.run_account_subprocess(1, 10, args, timeout_seconds=5)

    output = capsys.readouterr().out
    assert result.success is False
    assert terminated["called"] is True
    assert "新建注册页阶段无输出超时" in output


def test_camoufox_subprocess_timeout_is_based_on_output_idle_time(monkeypatch):
    import qwenv4_camoufox

    class DummyStdout:
        def __iter__(self):
            return iter(["still working\n"])

    class DummyProcess:
        def __init__(self, *_args, **_kwargs):
            self.stdout = DummyStdout()
            self.returncode = None
            self.pid = 12345
            self.poll_count = 0

        def poll(self):
            self.poll_count += 1
            if self.poll_count >= 4:
                self.returncode = 0
                return 0
            return None

    # 初始化 last_output_at=0；reader 读到输出时把 last_output_at 推进到 10。
    # 旧墙钟 deadline=0.001 会在主循环看到 now=10 后误杀；
    # 新的空闲超时逻辑会以最后输出时间 10 为基准，不会误杀。
    times = iter([0.0, 10.0, 10.0002, 10.0004, 10.0006, 10.0008])
    terminated = {"called": False}

    monkeypatch.setattr(qwenv4_camoufox.subprocess, "Popen", DummyProcess)
    monkeypatch.setattr(qwenv4_camoufox, "_terminate_process_tree", lambda *_args, **_kwargs: terminated.__setitem__("called", True))
    monkeypatch.setattr(qwenv4_camoufox.time, "monotonic", lambda: next(times, 0.010))
    monkeypatch.setattr(qwenv4_camoufox.time, "perf_counter", lambda: 0.0)
    monkeypatch.setattr(qwenv4_camoufox, "sleep_interruptible", lambda _seconds: True)

    args = qwenv4_camoufox.parse_args(["10", "--concurrency", "3"])
    result = qwenv4_camoufox.run_account_subprocess(2, 10, args, timeout_seconds=0.001)

    assert result.success is True
    assert terminated["called"] is False


def test_camoufox_start_slot_waits_for_slider_intent_to_clear(monkeypatch, tmp_path):
    import qwenv4_camoufox

    monkeypatch.chdir(tmp_path)
    intent_path = tmp_path / ".slider-captcha.lock.intent"
    called = {"foreground": False, "sleep": 0}

    @contextmanager
    def fake_foreground_lock(*_args, **_kwargs):
        called["foreground"] = True
        yield

    held_lock = None

    def fake_sleep(_seconds):
        called["sleep"] += 1
        nonlocal held_lock
        if held_lock is not None:
            held_lock.release()
            held_lock = None
        return True

    monkeypatch.setattr(qwenv4_camoufox, "acquire_foreground_window_lock", fake_foreground_lock)
    monkeypatch.setattr(qwenv4_camoufox, "sleep_interruptible", fake_sleep)

    held_lock = portalocker.Lock(str(intent_path), timeout=0)
    held_lock.acquire()
    result = qwenv4_camoufox.wait_for_camoufox_start_slot(
        "[账号 1/10]",
        stop_event=threading.Event(),
        file_lock_timeout=1.0,
    )

    assert result is True
    assert called["foreground"] is True
    assert called["sleep"] >= 1

def test_camoufox_worker_retries_transient_verification_navigation_error(monkeypatch, tmp_path):
    import qwenv4_camoufox

    attempts = {"goto": 0, "saved": False}

    class DummyPage:
        def __init__(self, context):
            self.context = context

        def goto(self, *args, **kwargs):
            attempts["goto"] += 1
            if attempts["goto"] == 1:
                raise RuntimeError("Page.goto: NS_BINDING_ABORTED")

        def screenshot(self, *args, **kwargs):
            pass

        def evaluate(self, *args, **kwargs):
            return "验证完成"

        def close(self):
            pass

    class DummyContext:
        def close(self):
            pass

    class DummyBrowser:
        def new_page(self, **kwargs):
            return DummyPage(DummyContext())

    class DummyCamoufox:
        def __init__(self, **kwargs):
            pass

        def __enter__(self):
            return DummyBrowser()

        def __exit__(self, exc_type, exc, tb):
            pass

    class DummyProvider:
        def create_inbox(self):
            return "user@example.com"

        def get_activation_link(self, timeout=300):
            return "https://studio.qwen.ai/auth/verify?token=test"

        def cleanup(self):
            pass

    args = types.SimpleNamespace(
        email_provider="mailtm",
        verbose=False,
        api_proxy=None,
        browser_proxy="http://127.0.0.1:7890",
        captcha_timeout=600,
        sync_qwen2api=False,
        qwen2api_base_url="http://127.0.0.1:7860",
        qwen2api_admin_key="admin",
        qwen2api_timeout=30,
    )

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(qwenv4_camoufox, "Camoufox", DummyCamoufox)
    monkeypatch.setattr(qwenv4_camoufox.EmailProviderFactory, "create", lambda **_kwargs: DummyProvider())
    monkeypatch.setattr(qwenv4_camoufox.UsedEmailsStore, "claim", lambda self, email: True)
    monkeypatch.setattr(qwenv4_camoufox, "register_qwen", lambda *args, **kwargs: True)
    monkeypatch.setattr(qwenv4_camoufox, "wait_for_token_extraction", lambda _page, timeout=5.0: {
        "token": "token-1",
        "active_token": None,
        "device_id": None,
        "user_role": "user",
    })
    monkeypatch.setattr(qwenv4_camoufox, "save_account", lambda **_kwargs: attempts.__setitem__("saved", True))
    monkeypatch.setattr(qwenv4_camoufox, "maybe_sync_account_to_qwen2api", lambda **_kwargs: None)
    monkeypatch.setattr(qwenv4_camoufox, "sleep_interruptible", lambda _seconds: True)

    result = qwenv4_camoufox.run_single_account(1, 1, args, None)

    assert result.success is True
    assert attempts["goto"] == 2
    assert attempts["saved"] is True


