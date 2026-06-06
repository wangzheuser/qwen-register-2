import json
import threading
import time

import pytest
import httpx

import qwenv4
from email_providers.store import UsedEmailsStore


def test_parse_args_accepts_concurrency_and_captcha_timeout():
    args = qwenv4.parse_args(["5", "--concurrency", "3", "--captcha-timeout", "600"])

    assert args.concurrency == 3
    assert args.captcha_timeout == 600


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
    monkeypatch.setattr(qwenv4.time, "sleep", lambda _seconds: None)
    monkeypatch.setattr(qwenv4, "detect_captcha", lambda _page: True)

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
