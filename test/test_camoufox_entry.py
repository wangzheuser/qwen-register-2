import types

import pytest

import qwenv4


def test_camoufox_entry_imports_and_reuses_qwenv4_parser():
    import qwenv4_camoufox

    args = qwenv4_camoufox.parse_args(["1"])

    assert args.email_provider == "mailtm"
    assert args.concurrency == 1
    assert args.captcha_timeout == 600
    assert qwenv4_camoufox.parse_args is qwenv4.parse_args


def test_camoufox_worker_uses_camoufox_without_chromium_args(monkeypatch, tmp_path):
    import qwenv4_camoufox

    seen = {}

    class DummyPage:
        def goto(self, *args, **kwargs):
            pass

        def screenshot(self, *args, **kwargs):
            pass

        def evaluate(self, *args, **kwargs):
            return "验证完成"

        def close(self):
            pass

    class DummyContext:
        def __init__(self):
            self.pages = []

        def new_page(self):
            page = DummyPage()
            self.pages.append(page)
            return page

        def close(self):
            pass

    class DummyBrowser:
        def __init__(self):
            self.contexts = []

        def new_context(self, **kwargs):
            seen["context_kwargs"] = kwargs
            context = DummyContext()
            self.contexts.append(context)
            return context

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
    monkeypatch.setattr(qwenv4_camoufox, "extract_tokens", lambda _page: {
        "token": "token-1",
        "active_token": None,
        "device_id": None,
        "user_role": "user",
    })
    monkeypatch.setattr(qwenv4_camoufox, "save_account", lambda **_kwargs: None)
    monkeypatch.setattr(qwenv4_camoufox, "maybe_sync_account_to_qwen2api", lambda **_kwargs: None)
    monkeypatch.setattr(qwenv4_camoufox.time, "sleep", lambda _seconds: None)

    assert qwenv4_camoufox.run_single_account(1, 1, args, "127.0.0.1:7890") is True

    launch_kwargs = seen["launch_kwargs"]
    assert launch_kwargs["headless"] is False
    assert launch_kwargs["proxy"] == {"server": "http://127.0.0.1:7890"}
    assert launch_kwargs["window"] == (1280, 800)
    assert launch_kwargs["humanize"] is True
    assert launch_kwargs["os"] == "windows"
    assert "--disable-blink-features=AutomationControlled" not in launch_kwargs.get("args", [])
    assert "--no-sandbox" not in launch_kwargs.get("args", [])
    assert "user_agent" not in seen["context_kwargs"]
    assert seen["context_kwargs"]["viewport"] == {"width": 1280, "height": 800}


def test_camoufox_worker_returns_false_when_browser_launch_fails(monkeypatch, capsys):
    import qwenv4_camoufox

    class BrokenCamoufox:
        def __init__(self, **kwargs):
            raise RuntimeError("camoufox missing")

    args = types.SimpleNamespace(
        email_provider="mailtm",
        verbose=False,
        api_proxy=None,
        captcha_timeout=600,
        sync_qwen2api=False,
        qwen2api_base_url="http://127.0.0.1:7860",
        qwen2api_admin_key="admin",
        qwen2api_timeout=30,
    )

    monkeypatch.setattr(qwenv4_camoufox, "Camoufox", BrokenCamoufox)

    assert qwenv4_camoufox.run_single_account(1, 1, args, None) is False
    assert "python -m camoufox fetch" in capsys.readouterr().out


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
        captcha_timeout=600,
        sync_qwen2api=False,
        qwen2api_base_url="http://127.0.0.1:7860",
        qwen2api_admin_key="admin",
        qwen2api_timeout=30,
    )

    monkeypatch.setattr(qwenv4_camoufox, "Camoufox", BrokenEnterCamoufox)

    assert qwenv4_camoufox.run_single_account(1, 1, args, None) is False
    assert "python -m camoufox fetch" in capsys.readouterr().out
