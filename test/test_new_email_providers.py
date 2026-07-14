"""GoneBox 与 TempMail.lol 邮箱 provider 的解析逻辑单元测试。

只测响应解析与激活链接提取，用假的 httpx.Client 桩件，不触网。
"""

from __future__ import annotations

import json

from email_providers.freecustom import FreeCustomProvider
from email_providers.gonebox import GoneBoxProvider
from email_providers.tempmail_lol import TempMailLolProvider


class _FakeResponse:
    def __init__(self, payload, status_code=200):
        self._payload = payload
        self.status_code = status_code
        self.text = json.dumps(payload)

    def json(self):
        return self._payload

    def raise_for_status(self):
        return None


class _FakeClient:
    """按 (method, url_substring) 顺序返回预设响应。"""

    def __init__(self, responses):
        self._responses = list(responses)

    def request(self, method, url, **_kwargs):
        payload = self._responses.pop(0)
        return _FakeResponse(payload)

    def close(self):
        pass


_ACTIVATION_HTML = (
    '<a href="https://chat.qwen.ai/api/v1/auth/verify?token=abc123">激活</a>'
)


def test_gonebox_create_and_activation_link():
    client = _FakeClient([
        {"success": True, "data": {"address": "USER@GoneBox.Email"}},   # 建箱
        {"data": {"messages": [{"id": "m1", "subject": "Qwen 验证"}]}},   # 列信
        {"data": {"body": {"html": _ACTIVATION_HTML}}},                  # 取详情
    ])
    provider = GoneBoxProvider(client=client)

    assert provider.create_inbox() == "user@gonebox.email"
    link = provider.get_activation_link(timeout=5)
    assert link == "https://chat.qwen.ai/api/v1/auth/verify?token=abc123"


def test_tempmail_lol_create_and_activation_link():
    client = _FakeClient([
        {"address": "USER@x.icodetensor.com", "token": "tok"},                       # 建箱
        {"expired": False, "emails": [{"id": "e1", "subject": "Qwen", "html": _ACTIVATION_HTML}]},  # 列信含全文
    ])
    provider = TempMailLolProvider(client=client)

    assert provider.create_inbox() == "user@x.icodetensor.com"
    link = provider.get_activation_link(timeout=5)
    assert link == "https://chat.qwen.ai/api/v1/auth/verify?token=abc123"


def test_freecustom_create_and_activation_link():
    client = _FakeClient([
        {"token": "anon-token"},                                          # auth
        {"data": [{"domain": "sqlcompiler.info", "tier": "free", "expires_in_days": None}]},  # domains
        {"data": []},                                                     # create_inbox 首次列信(空)
        {"data": [{"id": "m1", "subject": "Qwen 验证"}]},                  # get_activation_link 列信
        {"data": {"html": _ACTIVATION_HTML}},                             # 取详情
    ])
    provider = FreeCustomProvider(client=client)

    addr = provider.create_inbox()
    assert addr.endswith("@sqlcompiler.info")
    link = provider.get_activation_link(timeout=5)
    assert link == "https://chat.qwen.ai/api/v1/auth/verify?token=abc123"


def test_freecustom_filters_expired_domains():
    client = _FakeClient([
        {"token": "t"},
        {"data": [
            {"domain": "ditpay.info", "tier": "free", "expires_in_days": -3},   # 过期，应跳过
            {"domain": "addmy.space", "tier": "free", "expires_in_days": None},  # 存活
        ]},
    ])
    provider = FreeCustomProvider(client=client)
    provider._ensure_token()
    assert provider._live_domains() == ["addmy.space"]


if __name__ == "__main__":
    test_gonebox_create_and_activation_link()
    test_tempmail_lol_create_and_activation_link()
    test_freecustom_create_and_activation_link()
    test_freecustom_filters_expired_domains()
    print("OK")
