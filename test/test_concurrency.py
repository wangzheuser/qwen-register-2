import json
import threading
import time
from pathlib import Path

import pytest
import httpx

import qwenv4
from email_providers.store import UsedEmailsStore


def test_parse_args_accepts_concurrency_and_captcha_timeout():
    args = qwenv4.parse_args(["5", "--concurrency", "3", "--captcha-timeout", "600"])

    assert args.concurrency == 3
    assert args.captcha_timeout == 600



def test_parse_args_accepts_log_file_path():
    args = qwenv4.parse_args(["1", "--log-file", "logs/custom.log"])

    assert args.log_file == "logs/custom.log"


def test_build_browser_launch_args_assigns_unique_window_positions():
    first = qwenv4.build_browser_launch_args(1)
    second = qwenv4.build_browser_launch_args(2)
    tenth = qwenv4.build_browser_launch_args(10)

    assert "--window-size=1280,800" in first
    assert "--window-size=1280,800" in second
    first_position = next(arg for arg in first if arg.startswith("--window-position="))
    second_position = next(arg for arg in second if arg.startswith("--window-position="))
    tenth_position = next(arg for arg in tenth if arg.startswith("--window-position="))
    assert first_position != second_position
    assert len({first_position, second_position, tenth_position}) == 3


def test_enable_run_logging_writes_stdout_and_stderr_to_file(tmp_path):
    import sys
    from types import SimpleNamespace

    log_path = tmp_path / "run.log"
    state = qwenv4.enable_run_logging(SimpleNamespace(log_file=str(log_path)), script_stem="test-run")
    try:
        print("stdout 明文 token-123")
        print("stderr 明文 password-456", file=sys.stderr)
    finally:
        qwenv4.close_run_logging(state)

    content = log_path.read_text(encoding="utf-8")
    assert "stdout 明文 token-123" in content
    assert "stderr 明文 password-456" in content
    assert "[MainThread:" in content


def test_enable_run_logging_uses_single_default_file():
    from types import SimpleNamespace

    state = qwenv4.enable_run_logging(SimpleNamespace(log_file=""), script_stem="test-single")
    try:
        assert state.path == Path("logs") / "test-single.log"
    finally:
        qwenv4.close_run_logging(state)


def test_trim_log_file_discards_oldest_content_when_over_limit(tmp_path):
    log_path = tmp_path / "single.log"
    log_path.write_text("old-line\n" + "x" * 40 + "\nnew-line\n", encoding="utf-8")

    qwenv4.trim_log_file_to_limit(log_path, max_bytes=32)

    content = log_path.read_text(encoding="utf-8")
    assert len(log_path.read_bytes()) <= 32
    assert "old-line" not in content
    assert "new-line" in content


def test_run_logging_auto_trims_single_file_while_writing(tmp_path, monkeypatch):
    from types import SimpleNamespace

    log_path = tmp_path / "run.log"
    monkeypatch.setattr(qwenv4, "RUN_LOG_MAX_BYTES", 96)
    state = qwenv4.enable_run_logging(SimpleNamespace(log_file=str(log_path)), script_stem="ignored")
    try:
        print("old-content-" + "x" * 80)
        print("new-content")
    finally:
        qwenv4.close_run_logging(state)

    content = log_path.read_text(encoding="utf-8")
    assert len(log_path.read_bytes()) <= 96
    assert "old-content" not in content
    assert "new-content" in content


def test_parse_args_accepts_browser_proxy_template():
    proxy = "http://baokemeng.{uuid}:testpass@127.0.0.1:9200"
    args = qwenv4.parse_args(["1", "--browser-proxy", proxy])

    assert args.browser_proxy == proxy


def test_build_browser_proxy_replaces_uuid_each_time_and_hides_password():
    template = "http://baokemeng.{uuid}:testpass@127.0.0.1:9200"

    first = qwenv4.build_browser_proxy(template)
    second = qwenv4.build_browser_proxy(template)

    assert first["proxy"] == {
        "server": "http://127.0.0.1:9200",
        "username": first["username"],
        "password": "testpass",
    }
    assert first["username"].startswith("baokemeng.")
    assert second["username"].startswith("baokemeng.")
    first_uuid = first["username"].removeprefix("baokemeng.")
    second_uuid = second["username"].removeprefix("baokemeng.")
    assert len(first_uuid) == 32
    assert "-" not in first_uuid
    assert first_uuid != second_uuid
    assert first["display"] == f"http://{first['username']}@127.0.0.1:9200"
    assert "testpass" not in first["display"]


def test_build_browser_proxy_accepts_proxy_without_auth():
    result = qwenv4.build_browser_proxy("http://127.0.0.1:9200")

    assert result["proxy"] == {"server": "http://127.0.0.1:9200"}
    assert result["display"] == "http://127.0.0.1:9200"


def test_build_browser_proxy_rejects_invalid_format():
    with pytest.raises(ValueError, match="浏览器代理格式无效"):
        qwenv4.build_browser_proxy("not-a-proxy")


def test_parse_args_defaults_to_mailtm_provider():
    args = qwenv4.parse_args(["1"])

    assert args.email_provider == "mailtm"


def test_parse_args_defaults_to_qwen2api_sync_disabled():
    args = qwenv4.parse_args(["1"])

    assert args.sync_qwen2api is False
    assert args.qwen2api_base_url == "http://127.0.0.1:7860"
    assert args.qwen2api_admin_key == "admin"
    assert args.qwen2api_timeout == 30


def test_parse_args_accepts_qwen2api_sync_options():
    args = qwenv4.parse_args([
        "1",
        "--sync-qwen2api",
        "--qwen2api-base-url",
        "http://127.0.0.1:9999/",
        "--qwen2api-admin-key",
        "secret",
        "--qwen2api-timeout",
        "45",
    ])

    assert args.sync_qwen2api is True
    assert args.qwen2api_base_url == "http://127.0.0.1:9999/"
    assert args.qwen2api_admin_key == "secret"
    assert args.qwen2api_timeout == 45


def test_parse_args_rejects_non_positive_qwen2api_timeout():
    with pytest.raises(SystemExit):
        qwenv4.parse_args(["1", "--qwen2api-timeout", "0"])


def test_qwen2api_sync_failure_does_not_raise_in_main_helper(monkeypatch):
    from integrations.qwen2api import Qwen2ApiSyncConfig, Qwen2ApiSyncResult

    def fake_sync_account_to_qwen2api(**kwargs):
        return Qwen2ApiSyncResult(ok=False, skipped=False, message="同步失败")

    monkeypatch.setattr(qwenv4, "sync_account_to_qwen2api", fake_sync_account_to_qwen2api)

    result = qwenv4.maybe_sync_account_to_qwen2api(
        email="user@example.com",
        password="Password1!",
        token="token-123",
        config=Qwen2ApiSyncConfig(enabled=True),
        label="[账号 1/1]",
    )

    assert result.ok is False
    assert result.skipped is False


def test_get_current_ip_uses_http_client_without_browser_context():
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        if request.url.host == "api.ipify.org":
            return httpx.Response(200, json={"ip": "203.0.113.9"})
        if request.url.host == "ipinfo.io":
            return httpx.Response(200, json={"country": "US"})
        raise AssertionError(f"unexpected request: {request.url}")

    client = httpx.Client(transport=httpx.MockTransport(handler))

    assert qwenv4.get_current_ip(client=client) == ("203.0.113.9", "US")
    assert calls == [
        "https://api.ipify.org?format=json",
        "https://ipinfo.io/203.0.113.9/json",
    ]


def test_get_current_ip_returns_unknown_on_http_error():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("offline", request=request)

    client = httpx.Client(transport=httpx.MockTransport(handler))

    assert qwenv4.get_current_ip(client=client) == ("unknown", "unknown")


@pytest.mark.parametrize("value", ["0", "11"])
def test_parse_args_rejects_concurrency_outside_1_to_10(value):
    with pytest.raises(SystemExit):
        qwenv4.parse_args(["1", "--concurrency", value])


def test_parse_args_rejects_non_positive_captcha_timeout():
    with pytest.raises(SystemExit):
        qwenv4.parse_args(["1", "--captcha-timeout", "0"])


def test_register_qwen_passes_captcha_timeout(monkeypatch):
    monkeypatch.setenv("CAPTCHA_RECORD_TRACE", "1")

    class DummyLocator:
        def scroll_into_view_if_needed(self):
            pass

        def click(self):
            pass

    class DummyNavigation:
        def __enter__(self):
            return None

        def __exit__(self, exc_type, exc, tb):
            return False

    class DummyPage:
        def goto(self, *args, **kwargs):
            pass

        def wait_for_selector(self, *args, **kwargs):
            pass

        def fill(self, *args, **kwargs):
            pass

        def check(self, *args, **kwargs):
            pass

        def locator(self, *args, **kwargs):
            return DummyLocator()

        def expect_navigation(self, *args, **kwargs):
            return DummyNavigation()

        def evaluate(self, *args, **kwargs):
            return "待激活"

    seen = {}

    class FakeTopmost:
        def __enter__(self):
            return True

        def __exit__(self, exc_type, exc, tb):
            return False

    monkeypatch.setattr(qwenv4.time, "sleep", lambda _seconds: None)
    monkeypatch.setattr(qwenv4, "detect_captcha", lambda _page: True)
    monkeypatch.setattr(qwenv4, "hold_page_topmost", lambda _page, label="": FakeTopmost(), raising=False)

    def fake_wait_for_captcha_completion(page, email, password, name, timeout=300):
        seen["timeout"] = timeout
        return True

    monkeypatch.setattr(qwenv4, "wait_for_captcha_completion", fake_wait_for_captcha_completion)

    assert qwenv4.register_qwen(
        DummyPage(),
        "测试用户",
        "test@example.com",
        "Password1!",
        captcha_timeout=600,
    ) is True
    assert seen["timeout"] == 600


def test_run_single_account_retries_failed_attempt(monkeypatch):
    import types
    import qwenv4

    calls = []

    def fake_once(account_index, total_accounts, args, proxy_str=None):
        calls.append((account_index, total_accounts, proxy_str))
        return len(calls) == 2

    monkeypatch.setattr(qwenv4, "_run_single_account_once", fake_once)
    args = types.SimpleNamespace(account_retries=2)

    assert qwenv4.run_single_account(1, 10, args, "proxy") is True
    assert calls == [(1, 10, "proxy"), (1, 10, "proxy")]


def test_used_emails_store_claim_is_atomic_for_same_email(tmp_path):
    path = tmp_path / "used_emails.json"
    barrier = threading.Barrier(20)
    results = []
    results_lock = threading.Lock()

    def claim_email():
        store = UsedEmailsStore(path)
        barrier.wait(timeout=5)
        result = store.claim("Same@Example.com")
        with results_lock:
            results.append(result)

    threads = [threading.Thread(target=claim_email) for _ in range(20)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=5)

    assert results.count(True) == 1
    assert results.count(False) == 19
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data == {"emails": ["same@example.com"], "count": 1}


def test_save_account_concurrent_writes_keep_json_valid(tmp_path, monkeypatch):
    txt_path = tmp_path / "qwen_accounts.txt"
    json_path = tmp_path / "qwen_accounts.json"
    monkeypatch.setattr(qwenv4, "OUTPUT_FILE_TXT", str(txt_path))
    monkeypatch.setattr(qwenv4, "OUTPUT_FILE_JSON", str(json_path))

    barrier = threading.Barrier(20)

    def save(index):
        barrier.wait(timeout=5)
        time.sleep(0.01)
        qwenv4.save_account(
            email=f"user{index}@example.com",
            password="Password1!",
            name=f"User {index}",
            ip="127.0.0.1",
            country="ZZ",
            token=f"token-{index}",
        )

    threads = [threading.Thread(target=save, args=(i,)) for i in range(20)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)

    data = json.loads(json_path.read_text(encoding="utf-8"))
    assert len(data) == 20
    assert {item["email"] for item in data} == {f"user{i}@example.com" for i in range(20)}
    assert len(txt_path.read_text(encoding="utf-8").strip().splitlines()) == 20



def test_workers_guard_foreground_sensitive_launch_and_new_page():
    import inspect
    import qwenv4_camoufox

    normal_source = inspect.getsource(qwenv4._run_single_account_once)
    camoufox_source = inspect.getsource(qwenv4_camoufox._run_single_account_once)

    assert "acquire_foreground_window_lock(label=f\"{label} 浏览器启动\"" in normal_source
    assert "acquire_foreground_window_lock(label=f\"{label} 新建注册页\"" in normal_source
    assert "acquire_foreground_window_lock(label=f\"{label} Camoufox 启动\"" in camoufox_source
    # Camoufox new_page 偶发卡顿几十秒；它不应长时间持有前台窗口锁，
    # 否则会阻塞后续滑块。滑块阶段仍由 register_qwen 内的 slider 锁保护。
    assert "acquire_foreground_window_lock(label=f\"{label} 新建注册页\"" not in camoufox_source
    assert "with acquire_slider_lock(label=label" in inspect.getsource(qwenv4.register_qwen)


def test_start_parent_stop_file_watcher_requests_shutdown_without_force_exit(tmp_path, monkeypatch):
    stop_file = tmp_path / "stop.signal"
    calls = []
    monkeypatch.setenv(qwenv4.START_STOP_FILE_ENV, str(stop_file))
    monkeypatch.setattr(qwenv4.time, "sleep", lambda seconds: None)

    def fake_request_shutdown(reason, force_exit=True):
        calls.append((reason, force_exit))
        qwenv4.STOP_EVENT.set()

    monkeypatch.setattr(qwenv4, "request_shutdown", fake_request_shutdown)
    qwenv4.STOP_EVENT.clear()
    try:
        stop_file.write_text("stop", encoding="utf-8")
        watcher = qwenv4.start_parent_stop_file_watcher()
        watcher.join(timeout=2)
    finally:
        qwenv4.STOP_EVENT.clear()

    assert calls == [("收到启动脚本停止请求", False)]


def test_submit_account_futures_staggers_worker_start_by_100ms(monkeypatch):
    sleep_calls = []
    submitted = []

    class Executor:
        def submit(self, func, *args):
            submitted.append((func, args))
            return f"future-{len(submitted)}"

    monkeypatch.setattr(qwenv4, "sleep_interruptible", lambda seconds: sleep_calls.append(seconds) or True)
    qwenv4.STOP_EVENT.clear()

    futures = qwenv4.submit_account_futures(
        Executor(),
        total_accounts=4,
        args=object(),
        worker=lambda *args: True,
        start_interval=0.1,
    )

    assert list(futures.values()) == [1, 2, 3, 4]
    assert [args[0] for _func, args in submitted] == [1, 2, 3, 4]
    assert sleep_calls == [0.1, 0.1, 0.1]


def test_submit_account_futures_stops_staggering_when_shutdown_requested(monkeypatch):
    sleep_calls = []
    submitted = []

    class Executor:
        def submit(self, func, *args):
            submitted.append((func, args))
            return f"future-{len(submitted)}"

    def fake_sleep(seconds):
        sleep_calls.append(seconds)
        qwenv4.STOP_EVENT.set()
        return False

    monkeypatch.setattr(qwenv4, "sleep_interruptible", fake_sleep)
    qwenv4.STOP_EVENT.clear()
    try:
        futures = qwenv4.submit_account_futures(
            Executor(),
            total_accounts=4,
            args=object(),
            worker=lambda *args: True,
            start_interval=0.1,
        )
    finally:
        qwenv4.STOP_EVENT.clear()

    assert list(futures.values()) == [1]
    assert len(submitted) == 1
    assert sleep_calls == [0.1]


def test_wait_for_captcha_completion_returns_quickly_when_stop_set(monkeypatch):
    class Page:
        def __init__(self):
            self.detect_calls = 0

    page = Page()
    qwenv4.STOP_EVENT.clear()

    def fake_detect(_page):
        qwenv4.STOP_EVENT.set()
        return True

    slept = {"seconds": 0}
    monkeypatch.setattr(qwenv4, "detect_captcha", fake_detect)
    monkeypatch.setattr(qwenv4.time, "sleep", lambda seconds: slept.__setitem__("seconds", slept["seconds"] + seconds))

    assert qwenv4.wait_for_captcha_completion(page, "e@example.com", "pw", "name", timeout=30) is False
    assert slept["seconds"] == 0
    qwenv4.STOP_EVENT.clear()


def test_collect_futures_honors_stop_without_waiting_for_unfinished_future(monkeypatch):
    class PendingFuture:
        def __init__(self):
            self.cancelled = False

        def done(self):
            return False

        def cancel(self):
            self.cancelled = True
            return True

    pending = PendingFuture()
    qwenv4.STOP_EVENT.set()
    try:
        success = qwenv4.collect_account_futures({pending: 1}, 1, poll_interval=0)
    finally:
        qwenv4.STOP_EVENT.clear()

    assert success == 0
    assert pending.cancelled is True


def test_collect_account_futures_summarizes_success_durations(monkeypatch):
    class DoneFuture:
        def __init__(self, result):
            self._result = result

        def result(self):
            return self._result

    futures = {
        DoneFuture(qwenv4.AccountRunResult(success=True, duration_seconds=10.0)): 1,
        DoneFuture(qwenv4.AccountRunResult(success=False, duration_seconds=99.0)): 2,
        DoneFuture(qwenv4.AccountRunResult(success=True, duration_seconds=20.0)): 3,
    }
    monkeypatch.setattr(qwenv4, "wait", lambda pending, timeout, return_when: (set(pending), set()))
    qwenv4.STOP_EVENT.clear()

    summary = qwenv4.collect_account_futures(futures, 3, poll_interval=0)

    assert summary.success_count == 2
    assert sorted(summary.success_durations) == [10.0, 20.0]
    assert bool(qwenv4.AccountRunResult(success=True, duration_seconds=1.0)) is True
    assert bool(qwenv4.AccountRunResult(success=False, duration_seconds=1.0)) is False


def test_collect_account_futures_still_accepts_bool_results(monkeypatch):
    class DoneFuture:
        def __init__(self, result):
            self._result = result

        def result(self):
            return self._result

    futures = {DoneFuture(True): 1, DoneFuture(False): 2, DoneFuture(True): 3}
    monkeypatch.setattr(qwenv4, "wait", lambda pending, timeout, return_when: (set(pending), set()))
    qwenv4.STOP_EVENT.clear()

    summary = qwenv4.collect_account_futures(futures, 3, poll_interval=0)

    assert summary.success_count == 2
    assert summary.success_durations == []
    assert summary == 2


@pytest.mark.parametrize(
    ("success_count", "total", "expected"),
    [
        (0, 10, "0.00%"),
        (8, 10, "80.00%"),
        (10, 10, "100.00%"),
    ],
)
def test_format_success_rate(success_count, total, expected):
    assert qwenv4.format_success_rate(success_count, total) == expected


def test_format_average_success_duration_uses_only_success_durations():
    summary = qwenv4.AccountRunSummary(success_count=2, success_durations=[61.0, 63.0])

    assert qwenv4.format_average_success_duration(summary) == "1分02秒"


def test_format_average_success_duration_handles_no_success():
    summary = qwenv4.AccountRunSummary(success_count=0, success_durations=[])

    assert qwenv4.format_average_success_duration(summary) == "无成功账号"

def test_format_duration_seconds_formats_minutes_and_seconds():
    assert qwenv4.format_duration_seconds(103) == "1分43秒"
    assert qwenv4.format_duration_seconds(59.4) == "59秒"
    assert qwenv4.format_duration_seconds(62.0) == "1分02秒"


def _summary_args(**overrides):
    from types import SimpleNamespace

    values = dict(
        count=2,
        email_provider="mailtm",
        api_proxy=None,
        browser_proxy=None,
        concurrency=1,
        captcha_record_only=False,
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
    values.update(overrides)
    return SimpleNamespace(**values)


def test_qwenv4_main_prints_total_duration_after_average(monkeypatch, capsys):
    from types import SimpleNamespace

    monkeypatch.setattr(qwenv4, "parse_args", lambda argv=None: _summary_args())
    monkeypatch.setattr(qwenv4, "enable_run_logging", lambda *a, **k: SimpleNamespace(path="run.log"))
    monkeypatch.setattr(qwenv4, "close_run_logging", lambda state: None)
    monkeypatch.setattr(qwenv4, "start_parent_stop_file_watcher", lambda: None)
    monkeypatch.setattr(qwenv4, "submit_account_futures", lambda *a, **k: [])
    monkeypatch.setattr(
        qwenv4,
        "collect_account_futures",
        lambda futures, total: qwenv4.AccountRunSummary(success_count=2, success_durations=[61.0, 63.0]),
    )
    ticks = iter([10.0, 113.0])
    monkeypatch.setattr(qwenv4.time, "perf_counter", lambda: next(ticks))

    qwenv4.main()

    lines = capsys.readouterr().out.splitlines()
    average_index = lines.index("⏱️ 平均成功耗时: 1分02秒")
    assert lines[average_index + 1] == "⏳ 总耗时: 1分43秒"


def test_stage_timer_prints_account_timing_summary(capsys):
    timer = qwenv4.AccountStageTimer("[账号 1/1]", clock_values=iter([0.0, 1.2, 2.7]))

    timer.mark("邮箱创建")
    timer.mark("滑块处理")
    timer.print_summary(success=True)

    out = capsys.readouterr().out
    assert "[账号 1/1] 耗时拆分" in out
    assert "邮箱创建: 1.2s" in out
    assert "滑块处理: 1.5s" in out


def test_wait_for_registration_submission_returns_as_soon_as_success(monkeypatch):
    class Page:
        def __init__(self):
            self.calls = 0

        def evaluate(self, _script):
            self.calls += 1
            if self.calls >= 2:
                return "verification email 已发送"
            return "处理中"

    sleeps = []
    monkeypatch.setattr(qwenv4, "sleep_interruptible", lambda seconds: sleeps.append(seconds) or True)

    assert qwenv4.wait_for_registration_submission(Page(), timeout=3, interval=0.2) is True
    assert sleeps == [0.2]


def test_wait_for_token_extraction_returns_token_without_fixed_sleep(monkeypatch):
    calls = {"count": 0}

    def fake_extract(_page):
        calls["count"] += 1
        return {
            "token": "token-ok" if calls["count"] == 2 else None,
            "active_token": None,
            "device_id": None,
            "user_role": "user",
        }

    sleeps = []
    monkeypatch.setattr(qwenv4, "extract_tokens", fake_extract)
    monkeypatch.setattr(qwenv4, "sleep_interruptible", lambda seconds: sleeps.append(seconds) or True)

    tokens = qwenv4.wait_for_token_extraction(object(), timeout=3, interval=0.2)

    assert tokens["token"] == "token-ok"
    assert sleeps == [0.2]



def test_request_shutdown_forces_exit_after_short_grace(monkeypatch):
    exits = []
    monkeypatch.setattr(qwenv4, "INTERRUPT_FORCE_EXIT_SECONDS", 0.01)
    monkeypatch.setattr(qwenv4.os, "_exit", lambda code: exits.append(code))
    qwenv4.STOP_EVENT.clear()
    qwenv4.INTERRUPT_COUNT = 0

    qwenv4.request_shutdown("测试 Ctrl+C")

    deadline = time.time() + 1
    while time.time() < deadline and not exits:
        time.sleep(0.01)

    try:
        assert exits == [130]
        assert qwenv4.STOP_EVENT.is_set()
    finally:
        qwenv4.STOP_EVENT.clear()
        qwenv4.INTERRUPT_COUNT = 0



def test_maybe_sync_account_to_qwen2api_async_submits_without_blocking(monkeypatch):
    from integrations.qwen2api import Qwen2ApiSyncConfig

    submitted = []

    class FakeSyncer:
        def submit(self, **kwargs):
            submitted.append(kwargs)
            return "future-1"

    monkeypatch.setattr(qwenv4, "QWEN2API_ASYNC_SYNCER", FakeSyncer())

    result = qwenv4.maybe_sync_account_to_qwen2api_async(
        email="user@example.com",
        password="Password1!",
        token="token-123",
        config=Qwen2ApiSyncConfig(enabled=True, base_url="http://127.0.0.1:7860", admin_key="secret"),
        label="[账号 1/1]",
    )

    assert result is True
    assert submitted[0]["email"] == "user@example.com"
    assert submitted[0]["token"] == "token-123"
    assert submitted[0]["config"].admin_key == "secret"

