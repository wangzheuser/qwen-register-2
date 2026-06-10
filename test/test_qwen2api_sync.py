import json

import httpx

from integrations.qwen2api import Qwen2ApiSyncConfig, sync_account_to_qwen2api


def test_sync_account_posts_three_fields_and_auth_header():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["method"] = request.method
        seen["path"] = request.url.path
        seen["auth"] = request.headers.get("Authorization")
        seen["body"] = json.loads(request.content.decode("utf-8"))
        return httpx.Response(200, json={"ok": True, "email": "user@example.com"})

    client = httpx.Client(transport=httpx.MockTransport(handler))
    config = Qwen2ApiSyncConfig(enabled=True, base_url="http://127.0.0.1:7860", admin_key="secret", timeout=30)

    result = sync_account_to_qwen2api(
        email="user@example.com",
        password="Password1!",
        token="token-123",
        config=config,
        client=client,
    )

    assert result.ok is True
    assert result.skipped is False
    assert seen == {
        "method": "POST",
        "path": "/api/admin/accounts",
        "auth": "Bearer secret",
        "body": {"token": "token-123", "email": "user@example.com", "password": "Password1!"},
    }


def test_sync_account_skips_when_disabled_without_request():
    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("disabled sync should not send request")

    result = sync_account_to_qwen2api(
        email="user@example.com",
        password="Password1!",
        token="token-123",
        config=Qwen2ApiSyncConfig(enabled=False),
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )

    assert result.ok is False
    assert result.skipped is True
    assert "未启用" in result.message


def test_sync_account_skips_empty_token_without_request():
    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("empty token should not send request")

    result = sync_account_to_qwen2api(
        email="user@example.com",
        password="Password1!",
        token="",
        config=Qwen2ApiSyncConfig(enabled=True),
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )

    assert result.ok is False
    assert result.skipped is True
    assert "token" in result.message.lower()


def test_sync_account_returns_failure_for_ok_false_response():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"ok": False, "error": "Invalid token"})

    result = sync_account_to_qwen2api(
        email="user@example.com",
        password="Password1!",
        token="bad-token",
        config=Qwen2ApiSyncConfig(enabled=True),
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )

    assert result.ok is False
    assert result.skipped is False
    assert result.status_code == 200
    assert "Invalid token" in result.message


def test_sync_account_returns_failure_for_http_error_status():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(403, json={"detail": "Forbidden"})

    result = sync_account_to_qwen2api(
        email="user@example.com",
        password="Password1!",
        token="token-123",
        config=Qwen2ApiSyncConfig(enabled=True),
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )

    assert result.ok is False
    assert result.skipped is False
    assert result.status_code == 403
    assert "403" in result.message


def test_sync_account_returns_failure_for_network_exception():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("service down", request=request)

    result = sync_account_to_qwen2api(
        email="user@example.com",
        password="Password1!",
        token="token-123",
        config=Qwen2ApiSyncConfig(enabled=True),
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )

    assert result.ok is False
    assert result.skipped is False
    assert "service down" in result.message


def test_sync_account_returns_failure_for_timeout_exception():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.TimeoutException("too slow")

    result = sync_account_to_qwen2api(
        email="user@example.com",
        password="Password1!",
        token="token-123",
        config=Qwen2ApiSyncConfig(enabled=True, timeout=1),
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )

    assert result.ok is False
    assert result.skipped is False
    assert "超时" in result.message



def test_async_syncer_submit_returns_immediately_and_flushes(monkeypatch):
    from integrations.qwen2api import Qwen2ApiAsyncSyncer, Qwen2ApiSyncResult

    calls = []

    def fake_sync(**kwargs):
        calls.append(kwargs["email"])
        return Qwen2ApiSyncResult(ok=True, message=f"qwen2API 同步成功: {kwargs['email']}")

    monkeypatch.setattr("integrations.qwen2api.sync_account_to_qwen2api", fake_sync)
    syncer = Qwen2ApiAsyncSyncer(max_workers=1)

    future = syncer.submit(
        email="user@example.com",
        password="Password1!",
        token="token-123",
        config=Qwen2ApiSyncConfig(enabled=True),
        label="[账号 1/1]",
    )

    assert future is not None
    assert syncer.flush(timeout=2) == 1
    assert calls == ["user@example.com"]


def test_async_syncer_skips_disabled_config_without_future():
    from integrations.qwen2api import Qwen2ApiAsyncSyncer

    syncer = Qwen2ApiAsyncSyncer(max_workers=1)

    future = syncer.submit(
        email="user@example.com",
        password="Password1!",
        token="token-123",
        config=Qwen2ApiSyncConfig(enabled=False),
        label="[账号 1/1]",
    )

    assert future is None
    assert syncer.flush(timeout=1) == 0
