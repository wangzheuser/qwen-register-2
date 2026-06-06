
import json
import threading
from pathlib import Path

import httpx
import pytest

from email_providers import EmailProviderFactory
from email_providers.base import EmailCreationError, EmailParseError
from email_providers.mailporary import MailporaryProvider
from email_providers.mailtm import MailtmProvider
from email_providers.store import UsedEmailsStore
from email_providers.utils import extract_activation_link


def test_extract_activation_link_prefers_qwen_verify_link_from_html():
    html = '''
    <html><body>
      <a href="https://tracking.example.test/click">tracking</a>
      <a href="https://studio.qwen.ai/auth/verify?token=abc.def.ghi&amp;lang=en">Activate Qwen</a>
    </body></html>
    '''

    assert extract_activation_link(html=html) == "https://studio.qwen.ai/auth/verify?token=abc.def.ghi&lang=en"


def test_extract_activation_link_excludes_tracking_and_finds_text_link():
    html = '<a href="https://studio.qwen.ai/unsubscribe?token=bad">unsubscribe</a>'
    text = "Open https://studio.qwen.ai/auth/verify?token=good123. Thanks."

    assert extract_activation_link(html=html, text=text) == "https://studio.qwen.ai/auth/verify?token=good123"


def test_extract_activation_link_raises_parse_error_when_missing():
    assert extract_activation_link(html="<p>No link</p>", text="no qwen url") is None


def test_factory_creates_all_supported_providers():
    assert EmailProviderFactory.create("mailtm").__class__.__name__ == "MailtmProvider"
    assert EmailProviderFactory.create("mailporary").__class__.__name__ == "MailporaryProvider"
    assert EmailProviderFactory.create("generator.email", context=object()).__class__.__name__ == "GeneratorEmailProvider"
    assert EmailProviderFactory.create("generator", context=object()).__class__.__name__ == "GeneratorEmailProvider"


def test_factory_requires_context_for_generator_email():
    with pytest.raises(ValueError, match="BrowserContext"):
        EmailProviderFactory.create("generator.email")


def test_used_emails_store_lowercases_and_deduplicates(tmp_path):
    path = tmp_path / "used_emails.json"
    store = UsedEmailsStore(path)

    assert not store.is_used("Test@Example.COM")
    store.add("Test@Example.COM")
    store.add("test@example.com")

    reloaded = UsedEmailsStore(path)
    assert reloaded.is_used("TEST@example.com")
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data == {"emails": ["test@example.com"], "count": 1}


def test_used_emails_store_concurrent_writes(tmp_path):
    path = tmp_path / "used_emails.json"

    def add_email(index):
        UsedEmailsStore(path).add(f"User{index}@Example.com")

    threads = [threading.Thread(target=add_email, args=(i,)) for i in range(20)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["count"] == 20
    assert set(data["emails"]) == {f"user{i}@example.com" for i in range(20)}


def test_mailtm_retries_rate_limit_and_extracts_link(monkeypatch):
    monkeypatch.setattr("email_providers.mailtm.time.sleep", lambda _seconds: None)
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append((request.method, request.url.path))
        if request.method == "GET" and request.url.path == "/domains":
            return httpx.Response(200, json={"hydra:member": [{"domain": "example.com", "isActive": True}]})
        if request.method == "POST" and request.url.path == "/accounts":
            return httpx.Response(201, json={"id": "acct", "address": json.loads(request.content)["address"]})
        if request.method == "POST" and request.url.path == "/token":
            return httpx.Response(200, json={"token": "token-1", "id": "acct"})
        if request.method == "GET" and request.url.path == "/messages":
            message_list_calls = [call for call in calls if call == ("GET", "/messages")]
            if len(message_list_calls) == 1:
                return httpx.Response(429, json={"detail": "rate limited"})
            return httpx.Response(200, json={"hydra:member": [{"id": "msg1", "subject": "Qwen verify", "from": {"address": "no-reply@qwen.ai"}}]})
        if request.method == "GET" and request.url.path == "/messages/msg1":
            return httpx.Response(200, json={"text": "https://studio.qwen.ai/auth/verify?token=mailtm-token", "html": []})
        raise AssertionError(f"unexpected request: {request.method} {request.url}")

    provider = MailtmProvider(client=httpx.Client(transport=httpx.MockTransport(handler)))
    email = provider.create_inbox()
    link = provider.get_activation_link(timeout=1)

    assert email.endswith("@example.com")
    assert link == "https://studio.qwen.ai/auth/verify?token=mailtm-token"
    assert calls.count(("GET", "/messages")) == 2


def test_mailporary_refreshes_token_after_401(monkeypatch):
    monkeypatch.setattr("email_providers.mailporary.time.sleep", lambda _seconds: None)
    token_page_calls = 0
    mailbox_calls = 0

    def nuxt_page(token):
        return '<script type="application/json" id="__NUXT_DATA__">[null,{"mailServiceToken":2},"' + token + '"]</script>'

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal token_page_calls, mailbox_calls
        if request.method == "GET" and request.url.host == "mailporary.com":
            token_page_calls += 1
            token = "expired-token" if token_page_calls == 1 else "fresh-token"
            return httpx.Response(200, text=nuxt_page(token))
        if request.method == "GET" and request.url.path.startswith("/api/v1/mailbox/"):
            auth = request.headers.get("Authorization")
            if auth == "Bearer expired-token":
                return httpx.Response(401, json={"message": "expired"})
            mailbox_calls += 1
            parts = request.url.path.strip("/").split("/")
            if len(parts) == 4:
                if mailbox_calls == 1:
                    return httpx.Response(200, json={"messages": []})
                return httpx.Response(200, json={"messages": [{"id": "msg1", "subject": "Qwen verify", "from": {"address": "no-reply@qwen.ai"}}]})
            if len(parts) == 5:
                return httpx.Response(200, json={"body": {"text": "https://studio.qwen.ai/auth/verify?token=mailporary-token", "html": ""}})
        raise AssertionError(f"unexpected request: {request.method} {request.url}")

    provider = MailporaryProvider(
        client=httpx.Client(transport=httpx.MockTransport(handler)),
        email_prefix_factory=lambda: "abcdef",
    )

    email = provider.create_inbox()
    link = provider.get_activation_link(timeout=1)

    assert email == "abcdef@oeralb.com"
    assert link == "https://studio.qwen.ai/auth/verify?token=mailporary-token"
    assert token_page_calls == 2
    assert mailbox_calls >= 2
