import json
import os
import threading
from contextlib import contextmanager
from pathlib import Path

from PIL import Image, ImageDraw

import httpx
import pytest
from playwright.sync_api import sync_playwright

import qwenv4
from captcha_solvers.ai_slider import (
    CaptchaAiClient,
    CaptchaSolverConfig,
    CaptchaSolverResult,
    _parse_distance,
    solve_slider_captcha,
)


@pytest.fixture(autouse=True)
def restore_captcha_environment():
    keys = [
        "CAPTCHA_DRAG_BACKEND",
        "CAPTCHA_DRAG_STRATEGY",
        "CAPTCHA_RECORD_TRACE",
        "CAPTCHA_DEBUG_NETWORK",
        "CAPTCHA_REPLAY_TRACE",
        "CAPTCHA_CALLBACK_BYPASS",
        "CAPTCHA_FORCE_VERIFY_SUCCESS",
        "CAPTCHA_SUCCESS_WAIT_SECONDS",
    ]
    original = {key: os.environ.get(key) for key in keys}
    yield
    for key, value in original.items():
        if value is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = value


def test_parse_args_accepts_captcha_ai_options():
    args = qwenv4.parse_args([
        "1",
        "--captcha-solver",
        "ai",
        "--captcha-ai-base-url",
        "http://127.0.0.1:8000/v1",
        "--captcha-ai-api-key",
        "sk-test",
        "--captcha-ai-model",
        "gpt-5.5",
        "--captcha-ai-timeout",
        "120",
        "--captcha-ai-attempts",
        "3",
        "--no-captcha-ai-fallback-manual",
    ])

    assert args.captcha_solver == "ai"
    assert args.captcha_ai_base_url == "http://127.0.0.1:8000/v1"
    assert args.captcha_ai_api_key == "sk-test"
    assert args.captcha_ai_model == "gpt-5.5"
    assert args.captcha_ai_timeout == 120
    assert args.captcha_ai_attempts == 3
    assert args.no_captcha_ai_fallback_manual is True


def test_parse_args_defaults_to_ddddocr_solver():
    args = qwenv4.parse_args(["1"])

    assert args.captcha_solver == "ddddocr"


@pytest.mark.parametrize("solver", ["ddddocr", "ai", "manual"])
def test_parse_args_accepts_all_captcha_solver_modes(solver):
    args = qwenv4.parse_args(["1", "--captcha-solver", solver])

    assert args.captcha_solver == solver


def test_parse_args_accepts_captcha_trace_options():
    args = qwenv4.parse_args([
        "1",
        "--captcha-record-trace",
        "--captcha-record-only",
        "--captcha-replay-trace",
        "images/aliyun_probe/manual_trace.json",
        "--captcha-drag-backend",
        "os",
        "--captcha-callback-bypass",
        "--captcha-force-verify-success",
    ])

    assert args.captcha_record_trace is True
    assert args.captcha_record_only is True
    assert args.captcha_replay_trace == "images/aliyun_probe/manual_trace.json"
    assert args.captcha_drag_backend == "os"
    assert args.captcha_callback_bypass is True
    assert args.captcha_force_verify_success is True



def test_record_only_forces_manual_solver_and_enables_trace(monkeypatch):
    monkeypatch.delenv("CAPTCHA_RECORD_TRACE", raising=False)
    args = qwenv4.parse_args(["1", "--captcha-solver", "ai", "--captcha-record-only"])

    config = qwenv4.build_captcha_solver_config(args)

    assert config.enabled is False
    assert args.captcha_record_trace is True
    assert os.environ["CAPTCHA_RECORD_TRACE"] == "1"


def test_build_captcha_solver_config_defaults_to_ddddocr():
    config = qwenv4.build_captcha_solver_config(qwenv4.parse_args(["1"]))

    assert config.enabled is True
    assert config.mode == "ddddocr"
    assert config.base_url == "http://127.0.0.1:8000/v1"
    assert config.model == "gpt-5.5"
    assert config.attempts == 3
    assert config.fallback_manual is False


def test_build_captcha_solver_config_ai_uses_overall_timeout_and_no_manual_fallback():
    args = qwenv4.parse_args([
        "1",
        "--captcha-solver",
        "ai",
        "--captcha-ai-api-key",
        "sk-test",
        "--captcha-timeout",
        "77",
    ])

    config = qwenv4.build_captcha_solver_config(args)

    assert config.enabled is True
    assert config.mode == "ai"
    assert config.api_key == "sk-test"
    assert config.overall_timeout == 77
    assert config.fallback_manual is False


def test_build_captcha_solver_config_manual_disables_auto_solver():
    config = qwenv4.build_captcha_solver_config(qwenv4.parse_args(["1", "--captcha-solver", "manual"]))

    assert config.enabled is False
    assert config.mode == "manual"


@pytest.mark.parametrize("value", ["abc", "距离大约 142px", "-83"])
def test_parse_distance_extracts_integer(value):
    if value == "abc":
        with pytest.raises(ValueError):
            _parse_distance(value)
    else:
        assert _parse_distance(value) in {142, 83}


def test_ai_client_posts_openai_compatible_payload(tmp_path):
    image = tmp_path / "captcha.png"
    image.write_bytes(b"fake-png")
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["auth"] = request.headers.get("authorization")
        seen["payload"] = json.loads(request.content.decode("utf-8"))
        return httpx.Response(200, json={"choices": [{"message": {"content": "128"}}]})

    client = CaptchaAiClient(
        base_url="http://127.0.0.1:8000/v1",
        api_key="sk-test",
        model="gpt-5.5",
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )

    assert client.ask_slider_distance(str(image)) == 128
    assert seen["url"] == "http://127.0.0.1:8000/v1/chat/completions"
    assert seen["auth"] == "Bearer sk-test"
    assert seen["payload"]["model"] == "gpt-5.5"
    assert seen["payload"]["messages"][1]["content"][1]["image_url"]["url"].startswith("data:image/png;base64,")


def test_ai_client_reposts_post_body_after_http_to_https_redirect(monkeypatch, tmp_path):
    image = tmp_path / "captcha.png"
    image.write_bytes(b"fake-png")
    seen = {"urls": []}

    class DummyRequest:
        class Url:
            def __init__(self, value):
                self.value = value

            def join(self, location):
                return location

        def __init__(self, url):
            self.url = self.Url(url)

    class DummyRedirectResponse:
        status_code = 301
        headers = {"location": "https://127.0.0.1:8000/v1/chat/completions"}

        def __init__(self, url):
            self.request = DummyRequest(url)

    class DummyOkResponse:
        status_code = 200
        headers = {}

        def raise_for_status(self):
            pass

        def json(self):
            return {"choices": [{"message": {"content": "156"}}]}

    class DummyClient:
        def __init__(self, **kwargs):
            seen["client_kwargs"] = kwargs

        def post(self, url, headers=None, json=None):
            seen["urls"].append(url)
            if len(seen["urls"]) == 1:
                return DummyRedirectResponse(url)
            seen["second_payload"] = json
            return DummyOkResponse()

        def close(self):
            seen["closed"] = True

    monkeypatch.setattr("captcha_solvers.ai_slider.httpx.Client", DummyClient)
    client = CaptchaAiClient(
        base_url="http://127.0.0.1:8000/v1",
        api_key="sk-test",
        model="gpt-5.5",
    )

    assert client.ask_slider_distance(str(image)) == 156
    assert seen["urls"] == [
        "http://127.0.0.1:8000/v1/chat/completions",
        "https://127.0.0.1:8000/v1/chat/completions",
    ]
    assert seen["second_payload"]["model"] == "gpt-5.5"
    assert seen["closed"] is True


def test_ai_client_requires_api_key(tmp_path):
    image = tmp_path / "captcha.png"
    image.write_bytes(b"fake-png")
    client = CaptchaAiClient(api_key="")

    with pytest.raises(ValueError, match="API 密钥"):
        client.ask_slider_distance(str(image))


class FakeMouse:
    def __init__(self):
        self.events = []

    def move(self, x, y):
        self.events.append(("move", round(x), round(y)))

    def down(self):
        self.events.append(("down",))

    def up(self):
        self.events.append(("up",))


class FakeLocator:
    def __init__(self, page, name, box, visible=True, text=""):
        self.page = page
        self.name = name
        self._box = box
        self._visible = visible
        self._text = text

    def first(self):
        return self

    def is_visible(self, *args, **kwargs):
        return self._visible

    def bounding_box(self):
        return self._box

    def screenshot(self, path):
        Path(path).write_bytes(b"fake-captcha")

    def locator(self, selector):
        if "btn_slide" in selector or "slider" in selector or selector == "button":
            return self.page.slider
        return FakeLocator(self.page, selector, None, False)

    def inner_text(self, *args, **kwargs):
        return self._text


class FakePage:
    def __init__(self):
        self.mouse = FakeMouse()
        self.root_visible = True
        self.root = FakeLocator(self, "root", {"x": 10, "y": 20, "width": 300, "height": 180}, True)
        self.slider = FakeLocator(self, "slider", {"x": 30, "y": 150, "width": 40, "height": 32}, True)

    def locator(self, selector):
        if selector == "#waf_nc_block":
            return self.root if self.root_visible else FakeLocator(self, "hidden", None, False)
        return FakeLocator(self, selector, None, False)


class FakeDistanceClient:
    def ask_slider_distance(self, image_path):
        assert Path(image_path).exists()
        return 210


def test_solve_slider_captcha_sets_callback_bypass_mode_auto_by_default(tmp_path, monkeypatch):
    from captcha_solvers import ai_slider

    class Page:
        def __init__(self):
            self.mode = None

        def evaluate(self, script, *args):
            if "CAPTCHA_CALLBACK_BYPASS_MODE" in script:
                self.mode = args[0] if args else None
            return False

    class Root:
        pass

    page = Page()
    monkeypatch.setenv("CAPTCHA_CALLBACK_BYPASS", "1")
    monkeypatch.delenv("CAPTCHA_CALLBACK_BYPASS_MODE", raising=False)
    monkeypatch.setattr(ai_slider, "_find_captcha_root", lambda _page: Root())
    monkeypatch.setattr(ai_slider, "_wait_for_captcha_ready", lambda *args, **kwargs: True)
    monkeypatch.setattr(ai_slider, "_try_aliyun_callback_success", lambda _page: False)
    monkeypatch.setattr(ai_slider, "_screenshot_locator", lambda _root, path: Path(path).write_bytes(b"fake"))
    monkeypatch.setattr(ai_slider, "_build_drag_plan", lambda *_args: {
        "start_x": 10,
        "start_y": 10,
        "distance": 100,
        "max_distance": 300,
        "source": "图像匹配校准",
    })
    monkeypatch.setattr(ai_slider, "_perform_drag", lambda *_args: 100)
    monkeypatch.setattr(ai_slider, "_captcha_solved", lambda _page: True)
    monkeypatch.setattr(ai_slider.time, "sleep", lambda _seconds: None)

    result = ai_slider.solve_slider_captcha(
        page,
        ai_slider.CaptchaSolverConfig(enabled=True, attempts=1),
        image_dir=str(tmp_path),
    )

    assert result.ok is True
    assert page.mode == "auto"


def test_solve_slider_captcha_retries_callback_bypass_until_config_ready(tmp_path, monkeypatch):
    from captcha_solvers import ai_slider

    class Page:
        def evaluate(self, _script, *args):
            return False

    class Root:
        pass

    calls = {"callback": 0, "slept": 0}

    def fake_callback(_page):
        calls["callback"] += 1
        return calls["callback"] >= 3

    def fake_sleep(_seconds, _stop_event=None):
        calls["slept"] += 1

    monkeypatch.setenv("CAPTCHA_CALLBACK_BYPASS", "1")
    monkeypatch.setattr(ai_slider, "_find_captcha_root", lambda _page: Root())
    monkeypatch.setattr(ai_slider, "_wait_for_captcha_ready", lambda *args, **kwargs: True)
    monkeypatch.setattr(ai_slider, "_try_aliyun_callback_success", fake_callback)
    monkeypatch.setattr(ai_slider, "_sleep", fake_sleep)
    monkeypatch.setattr(ai_slider, "_captcha_solved", lambda _page: True)

    result = ai_slider.solve_slider_captcha(
        Page(),
        ai_slider.CaptchaSolverConfig(enabled=True, attempts=1),
        image_dir=str(tmp_path),
    )

    assert result.ok is True
    assert result.message == "本地靶场回调实验已通过滑块"
    assert calls["callback"] == 3
    assert calls["slept"] >= 2


def test_solve_slider_captcha_uses_playwright_mouse_and_detects_success(tmp_path, monkeypatch):
    page = FakePage()
    calls = {"count": 0}

    def fake_solved(_page):
        calls["count"] += 1
        return calls["count"] >= 1

    monkeypatch.setattr("captcha_solvers.ai_slider._captcha_solved", fake_solved)
    monkeypatch.setattr("captcha_solvers.ai_slider.time.sleep", lambda _seconds: None)

    result = solve_slider_captcha(
        page,
        CaptchaSolverConfig(enabled=True, api_key="sk-test"),
        image_dir=str(tmp_path),
        client=FakeDistanceClient(),
    )

    assert result.ok is True
    assert result.distance is not None
    assert ("down",) in page.mouse.events
    assert ("up",) in page.mouse.events


def test_solve_slider_captcha_prepares_before_drag_guard_and_confirms_inside_it(tmp_path, monkeypatch):
    from captcha_solvers import ai_slider

    events = []

    @contextmanager
    def drag_guard():
        events.append("guard_enter")
        try:
            yield True
        finally:
            events.append("guard_exit")

    monkeypatch.setattr(ai_slider, "_find_captcha_root", lambda _page: object())
    monkeypatch.setattr(ai_slider, "_wait_for_captcha_ready", lambda *_args, **_kwargs: True)
    monkeypatch.setattr(ai_slider, "_screenshot_locator", lambda *_args, **_kwargs: events.append("screenshot"))
    monkeypatch.setattr(
        ai_slider,
        "_build_drag_plan",
        lambda *_args, **_kwargs: events.append("plan") or {
            "source": "ddddocr",
            "distance": 100.0,
            "alternatives": [100.0],
            "calibrate_target_x": None,
        },
    )
    monkeypatch.setattr(
        ai_slider,
        "_perform_drag_with_focus_retry",
        lambda *_args, **_kwargs: events.append("drag") or 100.0,
    )
    monkeypatch.setattr(
        ai_slider,
        "_wait_for_captcha_success",
        lambda *_args, **_kwargs: events.append("verify") or "页面",
    )
    monkeypatch.setattr(ai_slider, "_write_captcha_attempt_metadata", lambda **_kwargs: None)

    result = ai_slider.solve_slider_captcha(
        object(),
        CaptchaSolverConfig(enabled=True, mode="ddddocr", attempts=1),
        image_dir=str(tmp_path),
        drag_guard=drag_guard,
    )

    assert result.ok is True
    assert events == ["screenshot", "plan", "guard_enter", "drag", "verify", "guard_exit"]


def test_solve_slider_captcha_focuses_once_when_background_page_hides_captcha_root(tmp_path, monkeypatch):
    from captcha_solvers import ai_slider

    events = []
    focused = False
    root = object()

    @contextmanager
    def drag_guard():
        nonlocal focused
        events.append("guard_enter")
        focused = True
        try:
            yield True
        finally:
            events.append("guard_exit")

    monkeypatch.setattr(ai_slider, "_find_captcha_root", lambda _page: root if focused else None)
    monkeypatch.setattr(ai_slider, "_captcha_solved", lambda _page: False)
    monkeypatch.setattr(ai_slider, "_wait_for_captcha_ready", lambda *_args, **_kwargs: True)
    monkeypatch.setattr(ai_slider, "_screenshot_locator", lambda *_args, **_kwargs: events.append("screenshot"))
    monkeypatch.setattr(
        ai_slider,
        "_build_drag_plan",
        lambda *_args, **_kwargs: {
            "source": "ddddocr",
            "distance": 100.0,
            "alternatives": [100.0],
            "calibrate_target_x": None,
        },
    )
    monkeypatch.setattr(ai_slider, "_perform_drag_with_focus_retry", lambda *_args, **_kwargs: 100.0)
    monkeypatch.setattr(ai_slider, "_wait_for_captcha_success", lambda *_args, **_kwargs: "页面")
    monkeypatch.setattr(ai_slider, "_write_captcha_attempt_metadata", lambda **_kwargs: None)

    result = ai_slider.solve_slider_captcha(
        object(),
        CaptchaSolverConfig(enabled=True, mode="ddddocr", attempts=1),
        image_dir=str(tmp_path),
        drag_guard=drag_guard,
    )

    assert result.ok is True
    assert events == ["guard_enter", "guard_exit", "screenshot", "guard_enter", "guard_exit"]


def test_find_captcha_root_accepts_nc_style_root():
    from captcha_solvers import ai_slider

    class Locator:
        def __init__(self, visible):
            self.first = self
            self.visible = visible

        def is_visible(self, timeout=1000):
            return self.visible

    class Page:
        def locator(self, selector):
            return Locator(selector == "[id*='nc_']")

    assert ai_slider._find_captcha_root(Page()) is not None


def test_ddddocr_matching_is_serialized_for_shared_model(monkeypatch):
    from captcha_solvers import ai_slider

    active = 0
    max_active = 0
    active_lock = threading.Lock()
    barrier = threading.Barrier(2)

    class Slide:
        def slide_match(self, *_args, **_kwargs):
            nonlocal active, max_active
            with active_lock:
                active += 1
                max_active = max(max_active, active)
            threading.Event().wait(0.05)
            with active_lock:
                active -= 1
            return object()

    monkeypatch.setattr(ai_slider, "_get_ddddocr_slide", lambda: Slide())
    monkeypatch.setattr(ai_slider, "_image_to_png_bytes", lambda _image: b"image")
    monkeypatch.setattr(ai_slider, "_extract_ddddocr_candidate", lambda *_args: (100.0, 1.0))

    def worker():
        barrier.wait(timeout=2)
        ai_slider._ddddocr_target_x(object(), object(), (0, 0, 1, 1))

    threads = [
        threading.Thread(target=worker)
        for _ in range(2)
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=3)

    assert all(not thread.is_alive() for thread in threads)
    assert max_active == 1


def test_solve_slider_captcha_passes_local_playwright_slider(tmp_path):
    class FixedDistanceClient:
        def ask_slider_distance(self, image_path):
            assert Path(image_path).exists()
            return 240

    html = """
    <!doctype html>
    <html>
    <head>
      <style>
        #waf_nc_block {
          position: relative;
          width: 320px;
          height: 160px;
          margin: 40px;
          background: #eee;
          border: 1px solid #999;
        }
        .btn_slide {
          position: absolute;
          left: 12px;
          bottom: 18px;
          width: 42px;
          height: 34px;
          background: #fff;
          border: 1px solid #333;
          cursor: pointer;
        }
        .target {
          position: absolute;
          right: 20px;
          top: 35px;
          width: 46px;
          height: 46px;
          background: #333;
        }
      </style>
    </head>
    <body>
      <div id="waf_nc_block">
        <div class="target"></div>
        <div class="btn_slide"></div>
      </div>
      <script>
        const root = document.querySelector('#waf_nc_block');
        const knob = document.querySelector('.btn_slide');
        let startX = 0;
        let dragging = false;
        knob.addEventListener('mousedown', (event) => {
          dragging = true;
          startX = event.clientX;
        });
        document.addEventListener('mousemove', (event) => {
          if (!dragging) return;
          const dx = Math.max(0, event.clientX - startX);
          knob.style.transform = `translateX(${dx}px)`;
        });
        document.addEventListener('mouseup', (event) => {
          if (!dragging) return;
          dragging = false;
          if (event.clientX - startX >= 220) {
            root.remove();
            document.body.dataset.captcha = 'ok';
          }
        });
      </script>
    </body>
    </html>
    """

    with sync_playwright() as p:
        try:
            browser = p.chromium.launch(headless=True)
        except Exception as exc:  # pragma: no cover - 取决于本地浏览器安装状态
            pytest.skip(f"Playwright Chromium 不可用: {exc}")
        try:
            page = browser.new_page(viewport={"width": 500, "height": 300})
            page.set_content(html)
            result = solve_slider_captcha(
                page,
                CaptchaSolverConfig(enabled=True, api_key="sk-test"),
                image_dir=str(tmp_path),
                client=FixedDistanceClient(),
            )

            assert result.ok is True
            assert page.evaluate("document.body.dataset.captcha") == "ok"
        finally:
            browser.close()



def test_read_page_body_text_retries_transient_null_body(monkeypatch):
    calls = {"evaluate": 0}
    sleeps = []

    class Page:
        def evaluate(self, script):
            assert "document.body ? document.body.innerText : ''" in script
            calls["evaluate"] += 1
            if calls["evaluate"] == 1:
                raise RuntimeError("Page.evaluate: document.body is null")
            return "pending activation verification email"

    now = {"value": 0.0}
    monkeypatch.setattr(qwenv4.time, "monotonic", lambda: now["value"])

    def fake_sleep(seconds, stop_event=None):
        sleeps.append(float(seconds))
        now["value"] += float(seconds)
        return True

    monkeypatch.setattr(qwenv4, "sleep_interruptible", fake_sleep)

    assert qwenv4.read_page_body_text(Page(), timeout=1.0, interval=0.2) == "pending activation verification email"
    assert calls["evaluate"] == 2
    assert sleeps == [pytest.approx(0.2)]


def test_register_qwen_treats_transient_null_body_after_slider_as_submitted(monkeypatch):
    from captcha_solvers.ai_slider import CaptchaSolverConfig, CaptchaSolverResult

    calls = {"body": 0}

    class Page:
        def evaluate(self, script, *args):
            if "document.body" in script:
                calls["body"] += 1
                if calls["body"] == 1:
                    raise RuntimeError("Page.evaluate: document.body is null")
                return "pending activation verification email"
            return ""

    class FakeSliderLock:
        def __enter__(self):
            return None

        def __exit__(self, exc_type, exc, tb):
            return False

    class FakeTopmost:
        def __enter__(self):
            return True

        def __exit__(self, exc_type, exc, tb):
            return False

    now = {"value": 0.0}
    monkeypatch.setattr(qwenv4.time, "monotonic", lambda: now["value"])
    monkeypatch.setattr(qwenv4, "sleep_interruptible", lambda seconds, stop_event=None: now.__setitem__("value", now["value"] + float(seconds)) or True)
    monkeypatch.setattr(qwenv4, "goto_register_page_with_retry", lambda _page: True)
    monkeypatch.setattr(qwenv4, "submit_registration_form_background", lambda *args, **kwargs: True)
    monkeypatch.setattr(qwenv4, "wait_for_captcha_or_submission_after_submit", lambda *_args, **_kwargs: "captcha")
    monkeypatch.setattr(qwenv4, "wait_for_registration_submission", lambda *_args, **_kwargs: False)
    monkeypatch.setattr(qwenv4, "detect_captcha", lambda _page: True)
    monkeypatch.setattr(qwenv4, "acquire_slider_lock", lambda **kwargs: FakeSliderLock())
    monkeypatch.setattr(qwenv4, "hold_page_topmost", lambda _page, label="": FakeTopmost(), raising=False)
    monkeypatch.setattr(qwenv4, "focus_page_for_slider", lambda *_args, **_kwargs: True)
    monkeypatch.setattr(qwenv4, "solve_slider_captcha", lambda *args, **kwargs: CaptchaSolverResult(ok=True, message="ok"))
    monkeypatch.setattr(qwenv4, "minimize_page_window", lambda *args, **kwargs: True, raising=False)

    assert qwenv4.register_qwen(
        Page(),
        "Test User",
        "test@example.com",
        "Password1!",
        captcha_solver_config=CaptchaSolverConfig(enabled=True, mode="ddddocr", attempts=3, fallback_manual=False),
    ) is True
    assert calls["body"] >= 2

def test_register_qwen_ai_failure_can_skip_manual(monkeypatch):
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

    monkeypatch.setattr(qwenv4.time, "sleep", lambda _seconds: None)
    monkeypatch.setattr(qwenv4, "detect_captcha", lambda _page: True)
    monkeypatch.setattr(
        qwenv4,
        "solve_slider_captcha",
        lambda *args, **kwargs: CaptchaSolverResult(ok=False, message="失败"),
    )

    assert qwenv4.register_qwen(
        DummyPage(),
        "测试用户",
        "test@example.com",
        "Password1!",
        captcha_solver_config=CaptchaSolverConfig(enabled=True, api_key="sk-test", fallback_manual=False),
    ) is False





def test_register_qwen_returns_false_when_ai_claims_ok_but_form_remains(monkeypatch):
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

        def evaluate(self, script):
            if "document.body.innerText" in script:
                return "创建账号\n请完成以下操作，验证您是真人\n日志ID: test"
            return ""

    monkeypatch.setattr(qwenv4.time, "sleep", lambda _seconds: None)
    monkeypatch.setattr(qwenv4, "detect_captcha", lambda _page: True)
    monkeypatch.setattr(
        qwenv4,
        "solve_slider_captcha",
        lambda *args, **kwargs: CaptchaSolverResult(ok=True, message="AI 滑块处理成功"),
    )

    assert qwenv4.register_qwen(
        DummyPage(),
        "测试用户",
        "test@example.com",
        "Password1!",
        captcha_solver_config=CaptchaSolverConfig(enabled=True, api_key="sk-test", fallback_manual=False),
    ) is False


def test_register_qwen_waits_longer_after_solver_success_for_slow_submission(monkeypatch):
    state = {"now": 0.0, "solver_called": False, "minimized": False}

    class DummyPage:
        def goto(self, *args, **kwargs):
            pass

        def wait_for_selector(self, *args, **kwargs):
            pass

        def evaluate(self, script, *args):
            if "document.body.innerText" in script:
                if state["solver_called"] and state["now"] >= 4.0:
                    return "pending activation verification email"
                return "Sign up to Qwen\nCreate account\n拖动滑块完成拼图"
            return ""

    def fake_sleep(seconds):
        state["now"] += float(seconds)
        return True

    @contextmanager
    def noop_lock(*_args, **_kwargs):
        yield

    @contextmanager
    def noop_topmost(*_args, **_kwargs):
        yield True

    def fake_solver(*_args, **_kwargs):
        state["solver_called"] = True
        return CaptchaSolverResult(ok=True, message="AI 滑块处理成功")

    monkeypatch.setattr(qwenv4.time, "monotonic", lambda: state["now"])
    monkeypatch.setattr(qwenv4, "sleep_interruptible", fake_sleep)
    monkeypatch.setattr(qwenv4.time, "sleep", lambda seconds: fake_sleep(seconds))
    monkeypatch.setattr(qwenv4, "submit_registration_form_background", lambda *_args, **_kwargs: True)
    monkeypatch.setattr(qwenv4, "detect_captcha", lambda _page: not state["solver_called"])
    monkeypatch.setattr(qwenv4, "focus_page_for_slider", lambda *_args, **_kwargs: True)
    monkeypatch.setattr(qwenv4, "acquire_slider_lock", noop_lock)
    monkeypatch.setattr(qwenv4, "hold_page_topmost", noop_topmost)
    monkeypatch.setattr(qwenv4, "minimize_page_window", lambda *_args, **_kwargs: state.__setitem__("minimized", True) or True)
    monkeypatch.setattr(qwenv4, "solve_slider_captcha", fake_solver)

    assert qwenv4.register_qwen(
        DummyPage(),
        "测试用户",
        "test@example.com",
        "Password1!",
        captcha_solver_config=CaptchaSolverConfig(enabled=True, api_key="sk-test", fallback_manual=False),
    ) is True
    assert state["minimized"] is True


def test_register_qwen_installs_force_verify_route_after_captcha_detected(monkeypatch):
    installed = {"verify_route": False}

    class DummyPage:
        def goto(self, *args, **kwargs):
            pass

        def wait_for_selector(self, *args, **kwargs):
            pass

        def evaluate(self, script, *args):
            if "document.body.innerText" in script:
                return "pending activation verification email"
            return ""

    monkeypatch.setenv("CAPTCHA_FORCE_VERIFY_SUCCESS", "1")
    monkeypatch.setattr(qwenv4.time, "sleep", lambda _seconds: None)
    monkeypatch.setattr(qwenv4, "submit_registration_form_background", lambda *_args, **_kwargs: True)
    monkeypatch.setattr(qwenv4, "detect_captcha", lambda _page: True)
    monkeypatch.setattr(qwenv4, "install_aliyun_verify_success_route", lambda _page: installed.__setitem__("verify_route", True))
    monkeypatch.setattr(qwenv4, "focus_page_for_slider", lambda *_args, **_kwargs: True)
    monkeypatch.setattr(qwenv4, "wait_for_registration_submission", lambda *_args, **_kwargs: True)
    monkeypatch.setattr(qwenv4, "minimize_page_window", lambda *_args, **_kwargs: True)

    @contextmanager
    def noop_lock(*_args, **_kwargs):
        yield

    @contextmanager
    def noop_topmost(*_args, **_kwargs):
        yield True

    monkeypatch.setattr(qwenv4, "acquire_slider_lock", noop_lock)
    monkeypatch.setattr(qwenv4, "hold_page_topmost", noop_topmost)
    monkeypatch.setattr(
        qwenv4,
        "solve_slider_captcha",
        lambda *args, **kwargs: CaptchaSolverResult(ok=True, message="AI 滑块处理成功"),
    )

    assert qwenv4.register_qwen(
        DummyPage(),
        "测试用户",
        "test@example.com",
        "Password1!",
        captcha_solver_config=CaptchaSolverConfig(enabled=True, api_key="sk-test", fallback_manual=False),
    ) is True
    assert installed["verify_route"] is True


def test_register_qwen_retries_transient_register_navigation_error(monkeypatch):
    calls = {"goto": 0, "submitted": 0}

    class DummyPage:
        def goto(self, *args, **kwargs):
            calls["goto"] += 1
            if calls["goto"] == 1:
                raise RuntimeError("Page.goto: NS_ERROR_NET_INTERRUPT")

        def evaluate(self, script, *args):
            if "document.body.innerText" in script:
                return "pending activation verification email"
            return ""

    def fake_submit(*_args, **_kwargs):
        calls["submitted"] += 1
        return True

    monkeypatch.setattr(qwenv4.time, "sleep", lambda _seconds: None)
    monkeypatch.setattr(qwenv4, "submit_registration_form_background", fake_submit)
    monkeypatch.setattr(qwenv4, "detect_captcha", lambda _page: False)

    assert qwenv4.register_qwen(
        DummyPage(),
        "测试用户",
        "test@example.com",
        "Password1!",
        captcha_solver_config=CaptchaSolverConfig(enabled=True, api_key="sk-test", fallback_manual=False),
    ) is True
    assert calls == {"goto": 2, "submitted": 1}


def test_parse_distance_ignores_large_log_id_and_uses_last_reasonable_number():
    text = "日志ID: 0a03e59617807991147102152e61d5\n请拖动滑块完成拼图\n建议移动 178"

    assert _parse_distance(text) == 178


def test_find_slider_handle_prefers_bottom_left_button_over_refresh():
    from captcha_solvers.ai_slider import _build_drag_plan

    class CandidateLocator:
        def __init__(self, box):
            self._box = box

        def is_visible(self, *args, **kwargs):
            return True

        def bounding_box(self):
            return self._box

    class LocatorList:
        def __init__(self, items):
            self.items = items

        @property
        def first(self):
            return self.items[0]

        def count(self):
            return len(self.items)

        def nth(self, index):
            return self.items[index]

    class Root:
        def __init__(self):
            self.items = [
                CandidateLocator({"x": 282, "y": 38, "width": 28, "height": 28}),
                CandidateLocator({"x": 25, "y": 135, "width": 42, "height": 34}),
            ]

        def bounding_box(self):
            return {"x": 10, "y": 20, "width": 320, "height": 180}

        def locator(self, _selector):
            return LocatorList(self.items)

    plan = _build_drag_plan(None, Root(), 180)

    assert 40 <= plan["start_x"] <= 55
    assert 145 <= plan["start_y"] <= 175


def test_solve_slider_captcha_returns_when_stop_event_is_set(tmp_path):
    event = threading.Event()
    event.set()

    result = solve_slider_captcha(
        FakePage(),
        CaptchaSolverConfig(enabled=True, api_key="sk-test"),
        image_dir=str(tmp_path),
        client=FakeDistanceClient(),
        stop_event=event,
    )

    assert result.ok is False
    assert "取消" in result.message



def test_aliyun_image_match_estimates_distance_from_data_urls(tmp_path):
    from captcha_solvers.ai_slider import _estimate_aliyun_slider_distance
    import base64
    from io import BytesIO

    bg = Image.new("RGBA", (300, 200), (80, 130, 90, 255))
    draw = ImageDraw.Draw(bg)
    for x in range(300):
        draw.line((x, 0, x, 199), fill=(50 + x // 3, 120 + x // 6, 80 + x // 7, 255))
    target_x = 178
    draw.rectangle((target_x, 95, target_x + 48, 145), fill=(210, 210, 210, 255))

    puzzle = Image.new("RGBA", (52, 200), (0, 0, 0, 0))
    piece = bg.crop((target_x, 95, target_x + 49, 145))
    puzzle.alpha_composite(piece, (2, 95))

    def data_url(img):
        buf = BytesIO()
        img.save(buf, format="PNG")
        return "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode("ascii")

    class Page:
        def evaluate(self, _script):
            return {
                "imgSrc": data_url(bg),
                "puzzleSrc": data_url(puzzle),
                "imgBox": {"x": 490, "y": 317, "width": 300, "height": 200},
                "puzzleBox": {"x": 490, "y": 317, "width": 52, "height": 200},
                "sliderBox": {"x": 490, "y": 525, "width": 40, "height": 40},
            }

    plan = _estimate_aliyun_slider_distance(Page())

    assert plan is not None
    assert abs(plan["distance"] - target_x) <= 8



def test_aliyun_image_match_returns_visual_target_for_calibrated_drag(tmp_path):
    from captcha_solvers.ai_slider import _estimate_aliyun_slider_distance
    import base64
    from io import BytesIO

    bg = Image.new("RGBA", (300, 200), (80, 130, 90, 255))
    draw = ImageDraw.Draw(bg)
    for x in range(300):
        draw.line((x, 0, x, 199), fill=(50 + x // 3, 120 + x // 6, 80 + x // 7, 255))
    target_x = 178
    draw.rectangle((target_x, 95, target_x + 48, 145), fill=(210, 210, 210, 255))

    puzzle = Image.new("RGBA", (52, 200), (0, 0, 0, 0))
    piece = bg.crop((target_x, 95, target_x + 49, 145))
    puzzle.alpha_composite(piece, (2, 95))

    def data_url(img):
        buf = BytesIO()
        img.save(buf, format="PNG")
        return "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode("ascii")

    class Page:
        def evaluate(self, _script):
            return {
                "imgSrc": data_url(bg),
                "puzzleSrc": data_url(puzzle),
                "imgBox": {"x": 490, "y": 317, "width": 300, "height": 200},
                "puzzleBox": {"x": 490, "y": 317, "width": 52, "height": 200},
                "sliderBox": {"x": 490, "y": 525, "width": 40, "height": 40},
            }

    plan = _estimate_aliyun_slider_distance(Page())

    assert plan is not None
    assert plan["calibrate_target_x"] == pytest.approx(target_x - 2, abs=4)
    assert plan["max_distance"] == pytest.approx(300, abs=1)


def test_perform_drag_closed_loop_stops_near_runtime_target(monkeypatch):
    from captcha_solvers.ai_slider import _perform_drag

    class Mouse:
        def __init__(self, page):
            self.page = page
            self.moves = []

        def move(self, x, y, steps=None):
            self.page.last_x = float(x)
            self.moves.append((float(x), float(y)))

        def down(self):
            self.moves.append(("down", 0.0))

        def up(self):
            self.moves.append(("up", 0.0))

    class Page:
        def __init__(self):
            self.last_x = 100.0
            self.mouse = Mouse(self)

        def evaluate(self, _script):
            left = max(0.0, self.last_x - 100.0)
            return {"sliderBoxX": left, "puzzleBoxX": left, "sliderLeft": left, "puzzleLeft": left}

    page = Page()
    monkeypatch.setattr("captcha_solvers.ai_slider.time.sleep", lambda _seconds: None)
    monkeypatch.setattr("captcha_solvers.ai_slider.random.randint", lambda _a, _b: 5)
    monkeypatch.setattr("captcha_solvers.ai_slider.random.uniform", lambda a, b: (a + b) / 2)

    distance = _perform_drag(page, {
        "start_x": 100,
        "start_y": 50,
        "distance": 210,
        "max_distance": 300,
        "calibrate_target_x": 195,
        "source": "图像匹配校准",
    })

    assert distance == pytest.approx(195, abs=8)
    assert page.mouse.moves[-1] == ("up", 0.0)


def test_wait_for_aliyun_captcha_ready_waits_past_loading(monkeypatch):
    from captcha_solvers.ai_slider import _wait_for_captcha_ready

    calls = {"count": 0, "slept": 0}

    class Page:
        def evaluate(self, _script):
            calls["count"] += 1
            if calls["count"] < 3:
                return {"ready": False, "reason": "图片加载中"}
            return {"ready": True, "reason": "已就绪"}

    monkeypatch.setattr("captcha_solvers.ai_slider.time.sleep", lambda seconds: calls.__setitem__("slept", calls["slept"] + seconds))

    assert _wait_for_captcha_ready(Page(), stop_event=None, timeout=2) is True
    assert calls["count"] == 3
    assert calls["slept"] > 0



def test_aliyun_white_gap_detection_prefers_right_target_over_left_piece():
    from captcha_solvers.ai_slider import _detect_white_gap_target_x

    bg = Image.new("RGBA", (300, 200), (80, 130, 90, 255))
    draw = ImageDraw.Draw(bg)
    for x in range(300):
        draw.line((x, 0, x, 199), fill=(40 + x // 4, 120 + x // 8, 70 + x // 9, 255))
    # 左侧拼图块也有亮色轮廓，但它靠近起点，不应被当成目标缺口。
    draw.rectangle((5, 60, 58, 118), fill=(235, 235, 220, 255))
    # 右侧白色缺口才是目标。
    draw.rectangle((238, 56, 286, 102), fill=(238, 238, 230, 255))

    target = _detect_white_gap_target_x(bg)

    assert target is not None
    assert 230 <= target <= 245



def test_distance_alternatives_are_bounded_and_ordered():
    from captcha_solvers.ai_slider import _distance_alternatives

    assert _distance_alternatives(275, 300) == [275, 265, 285, 257, 293]
    assert _distance_alternatives(298, 300) == [298, 288, 280]



def test_build_drag_plan_prefers_ai_distance_when_aliyun_image_estimate_is_suspicious(monkeypatch):
    from captcha_solvers import ai_slider
    from captcha_solvers.ai_slider import _build_drag_plan

    class Locator:
        def __init__(self, box):
            self._box = box

        def is_visible(self, *args, **kwargs):
            return True

        def bounding_box(self):
            return self._box

    class LocatorList:
        @property
        def first(self):
            return Locator({"x": 490, "y": 525, "width": 40, "height": 40})

        def count(self):
            return 1

        def nth(self, _index):
            return self.first

    class Root:
        def bounding_box(self):
            return {"x": 0, "y": 0, "width": 1280, "height": 800}

        def locator(self, _selector):
            return LocatorList()

    monkeypatch.setattr(ai_slider, "_estimate_aliyun_slider_distance", lambda _page: {
        "distance": 202.0,
        "max_distance": 300.0,
        "calibrate_target_x": 190.0,
    })

    plan = _build_drag_plan(object(), Root(), 233)

    assert plan["source"] == "AI+图像校验"
    assert plan["distance"] == pytest.approx(256.3, abs=0.5)



def test_parse_signed_adjustment_understands_direction_words():
    from captcha_solvers.ai_slider import _parse_signed_adjustment

    assert _parse_signed_adjustment("+12") == 12
    assert _parse_signed_adjustment("-8") == -8
    assert _parse_signed_adjustment("向左微调 10 像素") == -10
    assert _parse_signed_adjustment("向右 6px") == 6
    assert _parse_signed_adjustment("已经对齐") == 0


def test_ai_client_posts_correction_prompt(tmp_path):
    image = tmp_path / "hold.png"
    image.write_bytes(b"fake-png")
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["payload"] = json.loads(request.content.decode("utf-8"))
        return httpx.Response(200, json={"choices": [{"message": {"content": "-7"}}]})

    client = CaptchaAiClient(
        base_url="http://127.0.0.1:8000/v1",
        api_key="sk-test",
        model="gpt-5.5",
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )

    assert client.ask_slider_adjustment(str(image)) == -7
    assert "微调" in seen["payload"]["messages"][1]["content"][0]["text"]



def test_calculate_aliyun_local_adjustment_uses_current_puzzle_left():
    from captcha_solvers.ai_slider import _calculate_aliyun_local_adjustment

    class Page:
        def evaluate(self, _script):
            return {"sliderBoxX": 720, "puzzleBoxX": 700, "sliderLeft": 230, "puzzleLeft": 210}

    assert _calculate_aliyun_local_adjustment(Page(), 219) == pytest.approx(9 / 0.94, abs=0.2)



def test_target_alternatives_prioritize_right_template_when_white_matches_ocr():
    from captcha_solvers.ai_slider import _target_x_alternatives

    alternatives = _target_x_alternatives(
        chosen=207.0,
        image_width=300.0,
        ddddocr_x=206.5,
        white_gap_x=207.0,
        match_x=244.0,
    )

    assert alternatives[:2] == [207.0, 244.0]


def test_initial_failure_wait_default_is_short_for_fast_retry(monkeypatch):
    from captcha_solvers import ai_slider

    monkeypatch.delenv("CAPTCHA_INITIAL_FAILURE_WAIT_SECONDS", raising=False)

    assert ai_slider._captcha_initial_failure_wait_seconds() == pytest.approx(2.1)


def test_ddddocr_plan_uses_slide_match_result(monkeypatch):
    from captcha_solvers import ai_slider

    bg = Image.new("RGBA", (300, 200), (80, 130, 90, 255))
    puzzle = Image.new("RGBA", (52, 200), (0, 0, 0, 0))
    draw = ImageDraw.Draw(puzzle)
    draw.rectangle((2, 95, 50, 145), fill=(120, 150, 80, 255))

    class Page:
        def evaluate(self, _script):
            import base64
            from io import BytesIO
            def data_url(img):
                buf = BytesIO(); img.save(buf, format="PNG")
                return "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode("ascii")
            return {
                "imgSrc": data_url(bg),
                "puzzleSrc": data_url(puzzle),
                "imgBox": {"x": 490, "y": 317, "width": 300, "height": 200},
                "puzzleBox": {"x": 490, "y": 317, "width": 52, "height": 200},
                "sliderBox": {"x": 490, "y": 525, "width": 40, "height": 40},
            }

    class FakeSlide:
        def slide_match(self, target, background, simple_target=False):
            assert target and background
            assert simple_target is False
            return {"target": [206, 90, 258, 142]}

    monkeypatch.setattr(ai_slider, "_get_ddddocr_slide", lambda: FakeSlide())

    plan = ai_slider._estimate_aliyun_slider_distance(Page())

    assert plan is not None
    assert plan["source"] == "ddddocr"
    assert plan["calibrate_target_x"] == pytest.approx(179.5, abs=1)
    assert plan["distance"] == pytest.approx(179.5, abs=1)


def test_ddddocr_plan_falls_back_when_library_missing(monkeypatch):
    from captcha_solvers import ai_slider

    monkeypatch.setattr(ai_slider, "_get_ddddocr_slide", lambda: None)

    class Page:
        def evaluate(self, _script):
            return None

    assert ai_slider._estimate_aliyun_slider_distance(Page()) is None


def test_build_drag_plan_trusts_ddddocr_over_ai_distance(monkeypatch):
    from captcha_solvers import ai_slider

    class Slider:
        def bounding_box(self):
            return {"x": 30, "y": 150, "width": 40, "height": 32}

    class Root:
        def bounding_box(self):
            return {"x": 10, "y": 20, "width": 300, "height": 180}

    monkeypatch.setattr(ai_slider, "_find_slider_handle", lambda _root: Slider())
    monkeypatch.setattr(ai_slider, "_estimate_track_distance", lambda *_args: 300)
    monkeypatch.setattr(ai_slider, "_estimate_aliyun_slider_distance", lambda _page: {
        "distance": 217.0,
        "max_distance": 300.0,
        "calibrate_target_x": 204.0,
        "source": "ddddocr",
    })

    plan = ai_slider._build_drag_plan(object(), Root(), 90)

    assert plan["source"] == "ddddocr"
    assert plan["distance"] == pytest.approx(217.0)


def test_solve_slider_captcha_uses_ddddocr_without_ai_key(tmp_path, monkeypatch):
    from captcha_solvers import ai_slider

    class Root:
        pass

    monkeypatch.setattr(ai_slider, "_find_captcha_root", lambda _page: Root())
    monkeypatch.setattr(ai_slider, "_wait_for_captcha_ready", lambda *args, **kwargs: True)
    monkeypatch.setattr(ai_slider, "_screenshot_locator", lambda _root, path: Path(path).write_bytes(b"fake"))
    monkeypatch.setattr(ai_slider, "_build_drag_plan", lambda *_args: {
        "start_x": 10.0,
        "start_y": 20.0,
        "distance": 123.0,
        "max_distance": 300.0,
        "source": "ddddocr",
        "alternatives": [123.0],
    })
    monkeypatch.setattr(ai_slider, "_perform_drag", lambda _page, plan: float(plan["distance"]))
    monkeypatch.setattr(ai_slider, "_captcha_solved", lambda _page: True)
    monkeypatch.setattr(ai_slider.time, "sleep", lambda _seconds: None)

    result = solve_slider_captcha(
        object(),
        CaptchaSolverConfig(enabled=True, api_key=""),
        image_dir=str(tmp_path),
    )

    assert result.ok is True
    assert result.message == "AI 滑块处理成功"
    assert result.distance == 123


def test_ddddocr_mode_retries_three_times_without_ai_or_manual(tmp_path, monkeypatch):
    from captcha_solvers import ai_slider

    attempts = 0

    class Root:
        pass

    class ForbiddenClient:
        def ask_slider_distance(self, image_path):
            raise AssertionError("ddddocr 模式不应请求远程 AI")

    def fake_build_plan(*_args):
        nonlocal attempts
        attempts += 1
        return {
            "start_x": 10.0,
            "start_y": 20.0,
            "distance": 123.0,
            "max_distance": 300.0,
            "source": "AI",
            "alternatives": [123.0],
        }

    monkeypatch.setattr(ai_slider, "_find_captcha_root", lambda _page: Root())
    monkeypatch.setattr(ai_slider, "_wait_for_captcha_ready", lambda *args, **kwargs: True)
    monkeypatch.setattr(ai_slider, "_screenshot_locator", lambda _root, path: Path(path).write_bytes(b"fake"))
    monkeypatch.setattr(ai_slider, "_build_drag_plan", fake_build_plan)
    monkeypatch.setattr(ai_slider, "_try_refresh_captcha", lambda _root: None)
    monkeypatch.setattr(ai_slider, "_perform_drag", lambda _page, plan: float(plan["distance"]))
    monkeypatch.setattr(ai_slider, "_wait_for_captcha_success", lambda *args, **kwargs: "")
    monkeypatch.setattr(ai_slider.time, "sleep", lambda _seconds: None)

    result = solve_slider_captcha(
        object(),
        CaptchaSolverConfig(enabled=True, mode="ddddocr", attempts=3, api_key="", fallback_manual=False),
        image_dir=str(tmp_path),
        client=ForbiddenClient(),
    )

    assert result.ok is False
    assert result.attempts == 3
    assert attempts == 3


def test_ai_mode_requires_api_key_without_manual_fallback(tmp_path, monkeypatch):
    from captcha_solvers import ai_slider

    class Root:
        pass

    monkeypatch.setattr(ai_slider, "_find_captcha_root", lambda _page: Root())
    monkeypatch.setattr(ai_slider, "_wait_for_captcha_ready", lambda *args, **kwargs: True)
    monkeypatch.setattr(ai_slider, "_screenshot_locator", lambda _root, path: Path(path).write_bytes(b"fake"))

    result = solve_slider_captcha(
        object(),
        CaptchaSolverConfig(enabled=True, mode="ai", attempts=3, api_key="", fallback_manual=False),
        image_dir=str(tmp_path),
    )

    assert result.ok is False
    assert "未配置滑块 AI API 密钥" in result.message
    assert result.attempts == 0


def test_rejects_ddddocr_far_left_when_local_shape_points_right(monkeypatch):
    from captcha_solvers import ai_slider

    assert ai_slider._choose_aliyun_target_x(
        ddddocr_x=43.0,
        white_gap_x=147,
        match_x=211,
        image_width=300,
    ) == (211.0, "图像匹配校准")


def test_rejects_ddddocr_left_self_match_without_local_target(monkeypatch):
    from captcha_solvers import ai_slider

    assert ai_slider._choose_aliyun_target_x(
        ddddocr_x=43.0,
        white_gap_x=None,
        match_x=None,
        image_width=300,
    ) == (None, "图像匹配校准")


def test_ddddocr_plan_uses_direct_distance_without_ratio_or_calibration(monkeypatch):
    from captcha_solvers import ai_slider

    class Slider:
        def bounding_box(self):
            return {"x": 30, "y": 150, "width": 40, "height": 32}

    class Root:
        def bounding_box(self):
            return {"x": 10, "y": 20, "width": 300, "height": 180}

    monkeypatch.setattr(ai_slider, "_find_slider_handle", lambda _root: Slider())
    monkeypatch.setattr(ai_slider, "_estimate_track_distance", lambda *_args: 300)
    monkeypatch.setattr(ai_slider, "_estimate_aliyun_slider_distance", lambda _page: {
        "distance": 197.0,
        "max_distance": 300.0,
        "calibrate_target_x": None,
        "source": "ddddocr",
    })

    plan = ai_slider._build_drag_plan(object(), Root(), 0)

    assert plan["source"] == "ddddocr"
    assert plan["distance"] == pytest.approx(197.0)
    assert plan.get("calibrate_target_x") is None


def test_prefers_white_gap_when_available_against_ddddocr():
    from captcha_solvers import ai_slider

    assert ai_slider._choose_aliyun_target_x(
        ddddocr_x=262.0,
        white_gap_x=220,
        match_x=214,
        image_width=300,
    ) == (220.0, "图像匹配校准")




def test_prefers_ddddocr_when_white_gap_is_far_left_false_positive():
    from captcha_solvers import ai_slider

    assert ai_slider._choose_aliyun_target_x(
        ddddocr_x=194.0,
        white_gap_x=170,
        match_x=229,
        image_width=300,
    ) == (194.0, "ddddocr")



def test_prefers_ddddocr_when_white_and_template_are_left_of_ddddocr():
    from captcha_solvers import ai_slider

    assert ai_slider._choose_aliyun_target_x(
        ddddocr_x=183.0,
        white_gap_x=160,
        match_x=119,
        image_width=300,
    ) == (183.0, "ddddocr")


def test_prefers_ddddocr_when_left_local_candidates_cluster_nearby():
    """最新 Camoufox 日志中此类样本优先拖白色候选会连续失败。"""
    from captcha_solvers import ai_slider

    assert ai_slider._choose_aliyun_target_x(
        ddddocr_x=169.5,
        white_gap_x=146,
        match_x=133,
        image_width=296,
    ) == (169.5, "ddddocr")


def test_prefers_ddddocr_when_right_local_candidates_are_far_but_only_agree_with_each_other():
    """避免中右部 ddddocr 结果被远端亮色/模板簇覆盖，先尝试 ddddocr。"""
    from captcha_solvers import ai_slider

    assert ai_slider._choose_aliyun_target_x(
        ddddocr_x=190.5,
        white_gap_x=250,
        match_x=241,
        image_width=296,
    ) == (190.5, "ddddocr")


def test_force_aliyun_target_source_selects_named_candidate(monkeypatch):
    from captcha_solvers import ai_slider

    monkeypatch.setenv("CAPTCHA_TARGET_SOURCE", "ddddocr")
    assert ai_slider._choose_aliyun_target_x(
        ddddocr_x=236.0,
        white_gap_x=212,
        match_x=171,
        image_width=300,
    ) == (236.0, "ddddocr")

    monkeypatch.setenv("CAPTCHA_TARGET_SOURCE", "match")
    assert ai_slider._choose_aliyun_target_x(
        ddddocr_x=236.0,
        white_gap_x=212,
        match_x=171,
        image_width=300,
    ) == (171.0, "模板匹配强制")

    monkeypatch.setenv("CAPTCHA_TARGET_SOURCE", "white")
    assert ai_slider._choose_aliyun_target_x(
        ddddocr_x=236.0,
        white_gap_x=212,
        match_x=171,
        image_width=300,
    ) == (212.0, "亮色缺口强制")


def test_prefers_local_right_candidates_when_ddddocr_is_left_false_positive():
    from captcha_solvers import ai_slider

    target, source = ai_slider._choose_aliyun_target_x(
        ddddocr_x=155.5,
        white_gap_x=253,
        match_x=214,
        image_width=296,
    )

    assert target == pytest.approx(214)
    assert source == "图像匹配校准"




def test_prefers_template_when_ddddocr_is_near_left_edge_even_if_template_is_close():
    from captcha_solvers import ai_slider

    target, source = ai_slider._choose_aliyun_target_x(
        ddddocr_x=73.5,
        white_gap_x=None,
        match_x=100,
        image_width=296,
    )

    assert target == pytest.approx(100)
    assert source == "图像匹配校准"


def test_prefers_template_when_ddddocr_matches_left_slider_piece():
    from captcha_solvers import ai_slider

    target, source = ai_slider._choose_aliyun_target_x(
        ddddocr_x=81.5,
        white_gap_x=None,
        match_x=197,
        image_width=296,
    )

    assert target == pytest.approx(197)
    assert source == "图像匹配校准"


def test_keeps_ddddocr_when_only_template_conflicts_without_white_gap():
    """真实运行显示无 white_gap 时盲目切到模板会明显拉低一次通过率。"""

    from captcha_solvers import ai_slider

    target, source = ai_slider._choose_aliyun_target_x(
        ddddocr_x=215.5,
        white_gap_x=None,
        match_x=169,
        image_width=296,
    )

    assert target == pytest.approx(215.5)
    assert source == "ddddocr"


def test_keeps_high_ddddocr_when_template_is_far_left_without_white_gap():
    from captcha_solvers import ai_slider

    target, source = ai_slider._choose_aliyun_target_x(
        ddddocr_x=214.5,
        white_gap_x=None,
        match_x=94,
        image_width=296,
    )

    assert target == pytest.approx(214.5)
    assert source == "ddddocr"


def test_prefers_ddddocr_when_template_conflict_is_too_close_to_edge():
    """模板匹配落在极左边缘时更像误检，不应覆盖中右部 ddddocr。"""

    from captcha_solvers import ai_slider

    target, source = ai_slider._choose_aliyun_target_x(
        ddddocr_x=235.5,
        white_gap_x=None,
        match_x=10,
        image_width=296,
    )

    assert target == pytest.approx(235.5)
    assert source == "ddddocr"






def test_prefers_white_gap_for_left_mid_self_match_even_when_gap_is_slightly_farther():
    """真实 Camoufox 样本：ddddocr/template 聚在左中部，white_gap 右移约 44px 时也应优先缺口。"""
    from captcha_solvers import ai_slider

    target, source = ai_slider._choose_aliyun_target_x(
        ddddocr_x=144.5,
        white_gap_x=188,
        match_x=144,
        image_width=296,
    )

    assert target == pytest.approx(188)
    assert source == "图像匹配校准"


def test_prefers_white_gap_when_ddddocr_and_template_cluster_left_of_gap():
    """真实 Camoufox 样本：ddddocr/template 同在左侧时可能是拼图自匹配，应优先白色缺口。"""
    from captcha_solvers import ai_slider

    target, source = ai_slider._choose_aliyun_target_x(
        ddddocr_x=138.5,
        white_gap_x=174,
        match_x=139,
        image_width=296,
    )

    assert target == pytest.approx(174)
    assert source == "图像匹配校准"


def test_prefers_white_gap_when_ddddocr_is_near_left_edge_even_if_template_supports_it():
    """真实 Camoufox 样本：ddddocr 贴近左侧下限且模板也在左侧时，先试白色缺口可少浪费重试。"""
    from captcha_solvers import ai_slider

    target, source = ai_slider._choose_aliyun_target_x(
        ddddocr_x=74.5,
        white_gap_x=224,
        match_x=82,
        image_width=296,
    )

    assert target == pytest.approx(224)
    assert source == "图像匹配校准"


def test_prefers_ddddocr_when_template_supports_it_and_white_gap_is_far_right():
    from captcha_solvers import ai_slider

    target, source = ai_slider._choose_aliyun_target_x(
        ddddocr_x=197.5,
        white_gap_x=239,
        match_x=196,
        image_width=296,
    )

    assert target == pytest.approx(197.5)
    assert source == "ddddocr"


def test_prefers_white_gap_when_ddddocr_and_template_are_left_false_positive():
    from captcha_solvers import ai_slider

    target, source = ai_slider._choose_aliyun_target_x(
        ddddocr_x=196,
        white_gap_x=243,
        match_x=174,
        image_width=296,
    )

    assert target == pytest.approx(243)
    assert source == "图像匹配校准"


def test_calibrates_slider_distance_from_probe_ratio():
    from captcha_solvers import ai_slider

    plan = {"distance": 177.0, "max_distance": 300.0, "calibrate_target_x": 175.0}
    state = {"sliderLeft": 40.0, "puzzleLeft": 28.2}

    assert ai_slider._calibrate_distance_from_motion_probe(plan, state) == pytest.approx(248.2, abs=0.5)


def test_uses_white_gap_with_probe_calibration_when_ddddocr_missing():
    from captcha_solvers import ai_slider

    target, source = ai_slider._choose_aliyun_target_x(
        ddddocr_x=None,
        white_gap_x=142,
        match_x=66,
        image_width=300,
    )

    assert target == pytest.approx(142)
    assert source == "图像匹配校准"


def test_solve_slider_captcha_uses_local_image_plan_without_ai_key(tmp_path, monkeypatch):
    from captcha_solvers import ai_slider

    class Root:
        pass

    monkeypatch.setattr(ai_slider, "_find_captcha_root", lambda _page: Root())
    monkeypatch.setattr(ai_slider, "_wait_for_captcha_ready", lambda *args, **kwargs: True)
    monkeypatch.setattr(ai_slider, "_screenshot_locator", lambda _root, path: Path(path).write_bytes(b"fake"))
    monkeypatch.setattr(ai_slider, "_build_drag_plan", lambda *_args: {
        "start_x": 10.0,
        "start_y": 20.0,
        "distance": 150.0,
        "max_distance": 300.0,
        "source": "图像匹配校准",
        "alternatives": [150.0],
    })
    monkeypatch.setattr(ai_slider, "_perform_drag", lambda _page, plan: float(plan["distance"]))
    monkeypatch.setattr(ai_slider, "_captcha_solved", lambda _page: True)
    monkeypatch.setattr(ai_slider.time, "sleep", lambda _seconds: None)

    result = solve_slider_captcha(
        object(),
        CaptchaSolverConfig(enabled=True, api_key=""),
        image_dir=str(tmp_path),
    )

    assert result.ok is True
    assert result.distance == 150


def test_prefers_white_gap_over_smaller_ddddocr_when_close():
    from captcha_solvers import ai_slider

    assert ai_slider._choose_aliyun_target_x(
        ddddocr_x=210.0,
        white_gap_x=245,
        match_x=228,
        image_width=300,
    ) == (245.0, "图像匹配校准")


def test_local_adjustment_handles_aliyun_delta_just_over_old_threshold():
    from captcha_solvers.ai_slider import _calculate_aliyun_local_adjustment

    class Page:
        def evaluate(self, _script):
            return {"sliderBoxX": 699, "puzzleBoxX": 661.2, "sliderLeft": 209, "puzzleLeft": 171.2}

    adjustment = _calculate_aliyun_local_adjustment(Page(), 194)

    assert adjustment == pytest.approx(27.8, abs=1.5)


def test_local_adjustment_handles_large_underdrag_for_ddddocr_path():
    from captcha_solvers.ai_slider import _calculate_aliyun_local_adjustment

    class Page:
        def evaluate(self, _script):
            return {"sliderBoxX": 637, "puzzleBoxX": 578, "sliderLeft": 147, "puzzleLeft": 88}

    adjustment = _calculate_aliyun_local_adjustment(Page(), 146)

    assert adjustment == pytest.approx(96.9, abs=2.0)


def test_dampen_local_adjustment_reduces_large_correction():
    from captcha_solvers.ai_slider import _dampen_local_adjustment

    assert _dampen_local_adjustment(42.9) == pytest.approx(27.9, abs=0.2)
    assert _dampen_local_adjustment(9.0) == pytest.approx(9.0, abs=0.1)


def test_dampen_local_adjustment_halves_fast_quadratic_small_correction():
    from captcha_solvers.ai_slider import _dampen_local_adjustment

    assert _dampen_local_adjustment(-5.2, fast_quadratic=True) == pytest.approx(-2.6, abs=0.1)


def test_local_adjustment_handles_small_right_overshoot():
    from captcha_solvers.ai_slider import _calculate_aliyun_local_adjustment

    class Page:
        def evaluate(self, _script):
            return {"sliderBoxX": 714, "puzzleBoxX": 685.4, "sliderLeft": 224, "puzzleLeft": 195.4}

    adjustment = _calculate_aliyun_local_adjustment(Page(), 193)

    assert adjustment < 0
    assert adjustment == pytest.approx(-2.75, abs=0.35)


def test_wait_for_captcha_ready_treats_visit_verification_without_images_as_loading(monkeypatch):
    from captcha_solvers import ai_slider

    class Page:
        def evaluate(self, script):
            assert "访问验证" in script
            return {"ready": False, "aliyun": True, "reason": "Aliyun 验证码图片加载中"}

    now = {"value": 100.0}

    def fake_time():
        now["value"] += 0.05
        return now["value"]

    monkeypatch.setattr(ai_slider.time, "time", fake_time)
    monkeypatch.setattr(ai_slider, "_sleep", lambda _seconds, _stop_event=None: None)

    assert ai_slider._wait_for_captcha_ready(Page(), timeout=0.2) is False





def test_wait_for_captcha_ready_treats_access_verification_without_images_as_loading(monkeypatch):
    from captcha_solvers import ai_slider

    class Page:
        def evaluate(self, script):
            assert "Access Verification" in script
            return {"ready": False, "aliyun": True, "reason": "Aliyun 验证码图片加载中"}

    now = {"value": 200.0}

    def fake_time():
        now["value"] += 0.05
        return now["value"]

    monkeypatch.setattr(ai_slider.time, "time", fake_time)
    monkeypatch.setattr(ai_slider, "_sleep", lambda _seconds, _stop_event=None: None)

    assert ai_slider._wait_for_captcha_ready(Page(), timeout=0.2) is False


def test_detect_white_gap_handles_lower_brightness_gap_with_large_sky():
    from captcha_solvers.ai_slider import _detect_white_gap_target_x

    bg = Image.new("RGBA", (296, 200), (40, 120, 55, 255))
    draw = ImageDraw.Draw(bg)
    draw.rectangle((0, 2, 242, 86), fill=(184, 210, 190, 255))  # 大块低亮度天空，尺寸应被排除
    draw.rectangle((180, 38, 231, 90), fill=(40, 120, 55, 255))  # 暗色轮廓把缺口和天空分开
    draw.rectangle((183, 41, 228, 87), fill=(188, 204, 190, 255))  # 低亮度拼图缺口

    assert _detect_white_gap_target_x(bg) == 183


def test_detect_white_gap_keeps_blue_tinted_real_gap_out_of_sky():
    from captcha_solvers.ai_slider import _detect_white_gap_target_x

    bg = Image.new("RGBA", (296, 200), (60, 150, 210, 255))
    draw = ImageDraw.Draw(bg)
    # 大片蓝白天空/云层不应作为缺口返回。
    draw.rectangle((0, 0, 296, 92), fill=(210, 232, 246, 255))
    # 真实缺口也可能偏蓝白；旧逻辑会把这类颜色直接排除。
    draw.rectangle((221, 138, 268, 188), fill=(38, 115, 155, 255))
    draw.rectangle((224, 141, 265, 185), fill=(226, 241, 248, 255))

    assert _detect_white_gap_target_x(bg) == 224


def test_keeps_ddddocr_when_only_template_disagrees_without_white_gap():
    from captcha_solvers import ai_slider

    assert ai_slider._choose_aliyun_target_x(
        ddddocr_x=163.0,
        white_gap_x=None,
        match_x=125,
        image_width=296,
    ) == (163.0, "ddddocr")




def test_extract_ddddocr_candidate_converts_center_to_left_edge():
    from captcha_solvers.ai_slider import _extract_ddddocr_candidate

    candidate = _extract_ddddocr_candidate(
        {"target": [220, 100], "confidence": 0.8},
        (2, 104, 51, 154),
    )

    assert candidate is not None
    assert candidate[0] == pytest.approx(193.5)
    assert candidate[1] == pytest.approx(0.8)

def test_ddddocr_prefers_simple_target_when_standard_confidence_low(monkeypatch):
    from captcha_solvers import ai_slider

    calls = []

    class FakeSlide:
        def slide_match(self, target, background, simple_target=False):
            calls.append(simple_target)
            if simple_target:
                return {"target": [222, 100], "confidence": 0.30}
            return {"target": [106, 100], "confidence": 0.10}

    monkeypatch.setattr(ai_slider, "_get_ddddocr_slide", lambda: FakeSlide())
    bg = Image.new("RGBA", (296, 200), (0, 0, 0, 255))
    puzzle = Image.new("RGBA", (52, 200), (0, 0, 0, 0))
    ImageDraw.Draw(puzzle).rectangle((2, 104, 51, 154), fill=(100, 120, 140, 255))

    assert ai_slider._ddddocr_target_x(bg, puzzle, (2, 104, 51, 154)) == pytest.approx(195.5)
    assert calls == [False, True]


def test_prefers_white_gap_when_close_to_ddddocr():
    from captcha_solvers import ai_slider

    assert ai_slider._choose_aliyun_target_x(
        ddddocr_x=264.0,
        white_gap_x=241,
        match_x=156,
        image_width=296,
    ) == (241.0, "图像匹配校准")


def test_local_image_plan_uses_direct_target_distance(monkeypatch):
    from captcha_solvers import ai_slider

    bg = Image.new("RGBA", (296, 200), (0, 0, 0, 255))
    puzzle = Image.new("RGBA", (52, 200), (0, 0, 0, 0))

    class Page:
        def evaluate(self, _script):
            import base64
            from io import BytesIO
            def data_url(img):
                buf = BytesIO(); img.save(buf, format="PNG")
                return "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode("ascii")
            return {
                "imgSrc": data_url(bg),
                "puzzleSrc": data_url(puzzle),
                "imgBox": {"x": 490, "y": 317, "width": 296, "height": 200},
                "puzzleBox": {"x": 490, "y": 317, "width": 52, "height": 200},
                "sliderBox": {"x": 490, "y": 525, "width": 40, "height": 40},
            }

    monkeypatch.setattr(ai_slider, "_alpha_bbox", lambda _image: (2, 46, 51, 96))
    monkeypatch.setattr(ai_slider, "_ddddocr_target_x", lambda *_args: 267.0)
    monkeypatch.setattr(ai_slider, "_detect_white_gap_target_x", lambda _bg: 245)
    monkeypatch.setattr(ai_slider, "_match_puzzle_target_x", lambda *_args: 145)

    plan = ai_slider._estimate_aliyun_slider_distance(Page())

    assert plan["source"] == "图像匹配校准"
    assert plan["distance"] == pytest.approx(243, abs=1)


def test_prefers_white_gap_when_ddddocr_overestimates_by_medium_margin():
    from captcha_solvers import ai_slider

    assert ai_slider._choose_aliyun_target_x(
        ddddocr_x=252.0,
        white_gap_x=208,
        match_x=29,
        image_width=296,
    ) == (208.0, "图像匹配校准")


def test_prefers_white_gap_when_ddddocr_and_template_are_left_of_real_gap():
    from captcha_solvers import ai_slider

    target, source = ai_slider._choose_aliyun_target_x(
        ddddocr_x=113.5,
        white_gap_x=217,
        match_x=151,
        image_width=296,
    )

    assert target == pytest.approx(217)
    assert source == "图像匹配校准"


def test_white_gap_candidate_subtracts_puzzle_content_left(monkeypatch):
    from captcha_solvers import ai_slider

    bg = Image.new("RGBA", (296, 200), (0, 0, 0, 255))
    puzzle = Image.new("RGBA", (52, 200), (0, 0, 0, 0))

    class Page:
        def evaluate(self, _script):
            import base64
            from io import BytesIO
            def data_url(img):
                buf = BytesIO(); img.save(buf, format="PNG")
                return "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode("ascii")
            return {
                "imgSrc": data_url(bg),
                "puzzleSrc": data_url(puzzle),
                "imgBox": {"x": 490, "y": 317, "width": 296, "height": 200},
                "puzzleBox": {"x": 490, "y": 317, "width": 52, "height": 200},
                "sliderBox": {"x": 490, "y": 525, "width": 40, "height": 40},
            }

    monkeypatch.setattr(ai_slider, "_alpha_bbox", lambda _image: (2, 136, 51, 186))
    monkeypatch.setattr(ai_slider, "_ddddocr_target_x", lambda *_args: 187.0)
    monkeypatch.setattr(ai_slider, "_detect_white_gap_target_x", lambda _bg: 165)
    monkeypatch.setattr(ai_slider, "_match_puzzle_target_x", lambda *_args: 201)

    plan = ai_slider._estimate_aliyun_slider_distance(Page())

    assert plan["white_gap_target_x"] == pytest.approx(163)
    assert plan["distance"] == pytest.approx(163, abs=1)


def test_load_manual_trace_normalizes_recorded_points(tmp_path):
    from captcha_solvers.ai_slider import _load_manual_trace

    trace_path = tmp_path / "trace.json"
    trace_path.write_text(json.dumps({
        "distance": 200,
        "duration": 1000,
        "points": [
            {"t": 0, "x": 10, "y": 5},
            {"t": 250, "x": 80, "y": 6},
            {"t": 1000, "x": 210, "y": 4},
        ],
    }), encoding="utf-8")

    trace = _load_manual_trace(str(trace_path))

    assert trace is not None
    assert trace["distance"] == 200
    assert trace["duration"] == 1000
    assert trace["points"][0]["rx"] == 0
    assert trace["points"][-1]["rx"] == pytest.approx(1.0)


def test_replay_manual_trace_scales_points_to_current_distance(monkeypatch, tmp_path):
    from captcha_solvers.ai_slider import _load_manual_trace, _replay_manual_trace

    trace_path = tmp_path / "trace.json"
    trace_path.write_text(json.dumps({
        "distance": 200,
        "duration": 1000,
        "points": [
            {"t": 0, "x": 10, "y": 50},
            {"t": 500, "x": 110, "y": 52},
            {"t": 1000, "x": 210, "y": 49},
        ],
    }), encoding="utf-8")
    trace = _load_manual_trace(str(trace_path))
    mouse = FakeMouse()
    sleeps = []
    monkeypatch.setattr("captcha_solvers.ai_slider.time.sleep", lambda seconds: sleeps.append(seconds))

    _replay_manual_trace(mouse, start_x=100, start_y=70, target_distance=150, trace=trace)

    moves = [event for event in mouse.events if event[0] == "move"]
    assert moves[0] == ("move", 100, 70)
    assert moves[-1] == ("move", 250, 70)
    assert ("down",) not in mouse.events
    assert ("up",) not in mouse.events
    assert any(seconds > 0 for seconds in sleeps)


def test_dump_manual_trace_recording_writes_trace_and_verify_records(tmp_path):
    from captcha_solvers.ai_slider import dump_manual_trace_recording

    class Page:
        def evaluate(self, _script):
            return {
                "points": [
                    {"kind": "down", "t": 10, "x": 20, "y": 30},
                    {"kind": "move", "t": 90, "x": 120, "y": 32},
                    {"kind": "up", "t": 210, "x": 220, "y": 31},
                ],
                "duration": 200,
                "distance": 200,
                "url": "https://chat.qwen.ai/auth?mode=register",
            }

    path = dump_manual_trace_recording(
        Page(),
        [{"url": "https://captcha-open.local/", "post_data": "Action=VerifyCaptchaV2", "body": '{"VerifyResult":true}'}],
        image_dir=str(tmp_path),
    )

    assert path is not None
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    assert data["distance"] == 200
    assert data["verify_records"][0]["post_data"] == "Action=VerifyCaptchaV2"


def test_dump_manual_trace_recording_ignores_non_dict_page_result(tmp_path):
    from captcha_solvers.ai_slider import dump_manual_trace_recording

    class Page:
        def evaluate(self, _script):
            return "待激活"

    assert dump_manual_trace_recording(Page(), [], image_dir=str(tmp_path)) is None
    assert not (tmp_path / "aliyun_probe").exists()


def test_summarize_verify_record_extracts_captcha_fields():
    from captcha_solvers.ai_slider import _summarize_verify_record

    post_data = (
        "Action=VerifyCaptchaV2&SceneId=scene-a&CertifyId=cert-b&"
        "CaptchaVerifyParam=%7B%22sceneId%22%3A%22scene-a%22%2C%22certifyId%22%3A%22cert-b%22%2C"
        "%22deviceToken%22%3A%22dev%22%2C%22data%22%3A%22abcdef%22%7D"
    )
    body = '{"Result":{"VerifyCode":"F001","VerifyResult":false,"certifyId":"cert-b"}}'

    summary = _summarize_verify_record({"post_data": post_data, "body": body})

    assert summary["action"] == "VerifyCaptchaV2"
    assert summary["sceneId"] == "scene-a"
    assert summary["certifyId"] == "cert-b"
    assert summary["dataLength"] == 6
    assert summary["verifyCode"] == "F001"
    assert summary["verifyResult"] is False



def test_install_drag_event_debug_records_motion_state_snapshot():
    from captcha_solvers.ai_slider import _install_drag_event_debug

    class Page:
        def __init__(self):
            self.script = ""

        def evaluate(self, script):
            self.script = script

    page = Page()
    _install_drag_event_debug(page)

    assert "sliderLeft" in page.script
    assert "puzzleLeft" in page.script
    assert "getComputedStyle" in page.script


def test_dump_drag_event_debug_writes_event_summary(tmp_path, monkeypatch):
    from captcha_solvers.ai_slider import _dump_drag_event_debug

    class Page:
        def evaluate(self, _script):
            return [
                {"kind": "mousedown", "t": 10, "x": 20, "y": 30, "isTrusted": True, "buttons": 1},
                {"kind": "mousemove", "t": 40, "x": 80, "y": 31, "isTrusted": True, "buttons": 1},
                {"kind": "mouseup", "t": 210, "x": 220, "y": 30, "isTrusted": True, "buttons": 0},
            ]

    monkeypatch.setenv("CAPTCHA_DEBUG_DRAG_ARTIFACTS", "1")

    path = _dump_drag_event_debug(Page(), image_dir=str(tmp_path), label="[测试]")

    assert path is not None
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    assert data["summary"]["count"] == 3
    assert data["summary"]["durationMs"] == 200
    assert data["summary"]["distanceX"] == 200
    assert data["summary"]["allTrusted"] is True



def test_install_aliyun_verify_success_route_fulfills_verify_response():
    from captcha_solvers.ai_slider import install_aliyun_verify_success_route

    class Page:
        def __init__(self):
            self.pattern = None
            self.handler = None

        def route(self, pattern, handler):
            self.pattern = pattern
            self.handler = handler

    class Request:
        post_data = "Action=VerifyCaptchaV2&CertifyId=cert-1"

    class Route:
        request = Request()

        def __init__(self):
            self.fulfilled = None
            self.continued = False

        def fulfill(self, **kwargs):
            self.fulfilled = kwargs

        def continue_(self):
            self.continued = True

    page = Page()
    install_aliyun_verify_success_route(page)
    assert page.pattern == "**/*"

    route = Route()
    page.handler(route)

    assert route.fulfilled is not None
    assert route.fulfilled["status"] == 200
    body = json.loads(route.fulfilled["body"])
    assert body["Result"]["VerifyResult"] is True
    assert body["Result"]["VerifyCode"] == "T001"
    assert body["Result"]["certifyId"] == "cert-1"


def test_install_aliyun_callback_probe_uses_init_script():
    from captcha_solvers.ai_slider import install_aliyun_callback_probe

    class Page:
        def __init__(self):
            self.scripts = []

        def add_init_script(self, script):
            self.scripts.append(script)

    page = Page()
    install_aliyun_callback_probe(page)

    assert page.scripts
    assert "initAliyunCaptcha" in page.scripts[0]
    assert "captchaVerifyCallback" in page.scripts[0]



def test_install_aliyun_callback_probe_records_callback_sources_and_signup_requests():
    from captcha_solvers.ai_slider import install_aliyun_callback_probe

    class Page:
        def __init__(self):
            self.scripts = []

        def add_init_script(self, script):
            self.scripts.append(script)

    page = Page()
    install_aliyun_callback_probe(page)

    script = page.scripts[0]
    assert "String(original)" in script
    assert "new Error().stack" in script
    assert "__qwenSignupProbe" in script
    assert "window.fetch" in script
    assert "XMLHttpRequest.prototype.open" in script
    assert "u_asig" in script



def test_install_aliyun_callback_probe_wraps_get_instance_callback():
    from captcha_solvers.ai_slider import install_aliyun_callback_probe

    class Page:
        def __init__(self):
            self.scripts = []

        def add_init_script(self, script):
            self.scripts.append(script)

    page = Page()
    install_aliyun_callback_probe(page)

    script = page.scripts[0]
    assert "config.getInstance" in script
    assert "__captchaLastInstance" in script
    assert "getInstance" in script
    assert "config.securityToken" in script


def test_dump_captcha_dom_debug_includes_qwen_signup_probe(tmp_path, monkeypatch):
    from captcha_solvers import ai_slider

    class Page:
        def evaluate(self, script):
            assert "__qwenSignupProbe" in script
            return {
                "url": "https://chat.qwen.ai/auth?mode=register",
                "title": "Qwen",
                "callbackProbe": [],
                "signupProbe": [{"kind": "fetch", "urlSummary": {"u_asig": "sig"}}],
            }

    monkeypatch.chdir(tmp_path)
    ai_slider._dump_captcha_dom_debug(Page(), "[测试]")

    files = list((tmp_path / "images" / "aliyun_probe").glob("dom_debug_*.json"))
    assert files
    data = json.loads(files[0].read_text(encoding="utf-8"))
    assert data["signupProbe"][0]["urlSummary"]["u_asig"] == "sig"


def test_try_aliyun_callback_success_checks_child_frames():
    from captcha_solvers.ai_slider import _try_aliyun_callback_success

    class Frame:
        def __init__(self, result):
            self.result = result

        def evaluate(self, script, certify_id=None):
            assert "__captchaLastConfig" in script
            return self.result

    class Page:
        def __init__(self):
            self.frames = [Frame(False), Frame(True)]

        def evaluate(self, script, certify_id=None):
            return False

    assert _try_aliyun_callback_success(Page(), certify_id="cert-test") is True


def test_try_aliyun_callback_success_returns_true_when_page_reports_success():
    from captcha_solvers.ai_slider import _try_aliyun_callback_success

    class Page:
        def evaluate(self, script):
            assert "__captchaLastConfig" in script
            return True

    assert _try_aliyun_callback_success(Page(), certify_id="cert-test") is True



def test_try_aliyun_callback_success_builds_base64_sign_payload():
    from captcha_solvers.ai_slider import _try_aliyun_callback_success

    class Page:
        def evaluate(self, script, certify_id=None):
            assert "btoa(JSON.stringify" in script
            assert "securityToken" in script
            assert "instance.onBizSuccess(realCertifyId)" in script
            assert "config.success(signPayload)" in script
            assert "callbackBypassMode" in script
            assert certify_id == "cert-test"
            return True

    assert _try_aliyun_callback_success(Page(), certify_id="cert-test") is True


def test_try_aliyun_callback_success_defaults_to_auto_instance_first():
    from captcha_solvers.ai_slider import _try_aliyun_callback_success

    class Page:
        def evaluate(self, script, certify_id=None):
            assert "String(window.CAPTCHA_CALLBACK_BYPASS_MODE || 'auto')" in script
            assert "callbackBypassMode === 'auto'" in script
            assert script.index("instance.onBizSuccess(realCertifyId)") < script.index("config.success(signPayload)")
            assert certify_id == "cert-test"
            return True

    assert _try_aliyun_callback_success(Page(), certify_id="cert-test") is True


def test_perform_drag_uses_manual_trace_when_configured(monkeypatch, tmp_path):
    from captcha_solvers.ai_slider import _perform_drag

    trace_path = tmp_path / "trace.json"
    trace_path.write_text(json.dumps({
        "distance": 200,
        "duration": 1000,
        "points": [
            {"t": 0, "x": 10, "y": 50},
            {"t": 500, "x": 110, "y": 51},
            {"t": 1000, "x": 210, "y": 50},
        ],
    }), encoding="utf-8")

    class Page:
        def __init__(self):
            self.mouse = FakeMouse()

        def evaluate(self, _script):
            return {"sliderBoxX": 150, "puzzleBoxX": 150, "sliderLeft": 150, "puzzleLeft": 150}

    page = Page()
    monkeypatch.setenv("CAPTCHA_REPLAY_TRACE", str(trace_path))
    monkeypatch.setattr("captcha_solvers.ai_slider.time.sleep", lambda _seconds: None)

    distance = _perform_drag(page, {
        "start_x": 100,
        "start_y": 70,
        "distance": 150,
        "max_distance": 300,
        "source": "图像匹配校准",
    })

    assert distance == 150
    assert ("down",) in page.mouse.events
    assert ("up",) in page.mouse.events
    assert ("move", 250, 70) in page.mouse.events



def test_estimate_aliyun_nonlinear_slider_distance_uses_quadratic_mapping():
    from captcha_solvers.ai_slider import _estimate_aliyun_nonlinear_slider_distance

    assert _estimate_aliyun_nonlinear_slider_distance(target_x=152, max_distance=296) == pytest.approx(200, abs=3)
    assert _estimate_aliyun_nonlinear_slider_distance(target_x=214, max_distance=296) == pytest.approx(236, abs=3)


def test_resolve_ratio_human_distance_uses_stable_ratio_or_default():
    from captcha_solvers.ai_slider import _resolve_ratio_human_distance

    assert _resolve_ratio_human_distance(target_x=170, ratio=0.23, current_distance=170, max_distance=300) == pytest.approx(250, abs=0.5)
    assert _resolve_ratio_human_distance(target_x=170, ratio=0.68, current_distance=170, max_distance=300) == pytest.approx(250, abs=0.5)


def test_perform_drag_ratio_human_calibrates_distance_from_motion_ratio(monkeypatch):
    from captcha_solvers import ai_slider
    from captcha_solvers.ai_slider import _perform_drag

    class Mouse:
        def __init__(self):
            self.moves = []
            self.down_called = False
            self.up_called = False

        def move(self, x, y, steps=1):
            self.moves.append((float(x), float(y), int(steps)))

        def down(self):
            self.down_called = True

        def up(self):
            self.up_called = True

    class Page:
        def __init__(self):
            self.mouse = Mouse()

        def evaluate(self, *args, **kwargs):
            return []

    page = Page()
    calls = {}
    monkeypatch.setenv("CAPTCHA_DRAG_STRATEGY", "ratio_human")
    monkeypatch.setattr(ai_slider.time, "sleep", lambda _seconds: None)
    monkeypatch.setattr(ai_slider.random, "uniform", lambda a, b: (a + b) / 2)
    monkeypatch.setattr(ai_slider.random, "randint", lambda a, b: max(a, min(b, 4)))
    monkeypatch.setattr(ai_slider, "_measure_aliyun_motion_ratio", lambda _page: 0.66)

    def fake_human(mouse, start_x, start_y, distance, **kwargs):
        calls["distance"] = distance
        mouse.move(start_x + distance, start_y, steps=1)

    monkeypatch.setattr(ai_slider, "_drag_aliyun_human_like", fake_human)

    distance = _perform_drag(page, {
        "start_x": 100,
        "start_y": 70,
        "distance": 160,
        "max_distance": 300,
        "calibrate_target_x": 160,
        "source": "ddddocr",
    })

    assert distance == pytest.approx(242.4, abs=0.5)
    assert calls["distance"] == pytest.approx(242.4, abs=0.5)
    assert page.mouse.down_called is True
    assert page.mouse.up_called is True


def test_perform_drag_ratio_human_ignores_unreasonable_motion_ratio(monkeypatch):
    from captcha_solvers import ai_slider
    from captcha_solvers.ai_slider import _perform_drag

    class Mouse:
        def move(self, x, y, steps=1):
            pass
        def down(self):
            pass
        def up(self):
            pass

    class Page:
        mouse = Mouse()
        def evaluate(self, *args, **kwargs):
            return []

    calls = {}
    monkeypatch.setenv("CAPTCHA_DRAG_STRATEGY", "ratio_human")
    monkeypatch.setattr(ai_slider.time, "sleep", lambda _seconds: None)
    monkeypatch.setattr(ai_slider.random, "uniform", lambda a, b: (a + b) / 2)
    monkeypatch.setattr(ai_slider.random, "randint", lambda a, b: max(a, min(b, 4)))
    monkeypatch.setattr(ai_slider, "_measure_aliyun_motion_ratio", lambda _page: 0.23)

    def fake_human(mouse, start_x, start_y, distance, **kwargs):
        calls["distance"] = distance

    monkeypatch.setattr(ai_slider, "_drag_aliyun_human_like", fake_human)

    distance = _perform_drag(Page(), {
        "start_x": 100,
        "start_y": 70,
        "distance": 160,
        "max_distance": 300,
        "calibrate_target_x": 160,
        "source": "ddddocr",
    })

    assert distance == pytest.approx(235.3, abs=0.5)
    assert calls["distance"] == pytest.approx(235.3, abs=0.5)


def test_success_profile_drag_grips_slider_then_replays_recorded_offsets(monkeypatch):
    from captcha_solvers import ai_slider

    class Mouse:
        def __init__(self):
            self.moves = []

        def move(self, x, y, steps=1):
            self.moves.append((float(x), float(y), int(steps)))

    sleeps = []
    monkeypatch.setattr(ai_slider.time, "sleep", lambda seconds: sleeps.append(float(seconds)))
    monkeypatch.setattr(ai_slider.random, "uniform", lambda a, b: (a + b) / 2)

    mouse = Mouse()
    ai_slider._drag_aliyun_success_profile(mouse, 100.0, 70.0, 220.0)

    offsets = [x - 100.0 for x, _y, _steps in mouse.moves]
    assert 0 < offsets[0] <= 2.0
    assert any(offset == pytest.approx(220.0 * ai_slider.ALIYUN_SUCCESS_PROFILE_POINTS[0][1]) for offset in offsets)
    assert offsets[-1] == pytest.approx(220.0)
    assert max(offsets) > 220.0
    assert any(steps > 1 for _x, _y, steps in mouse.moves)
    assert len(sleeps) >= len(ai_slider.ALIYUN_SUCCESS_PROFILE_POINTS) - 1


def test_perform_drag_success_profile_strategy_applies_final_alignment(monkeypatch):
    from captcha_solvers import ai_slider
    from captcha_solvers.ai_slider import _perform_drag

    class Mouse:
        def move(self, x, y, steps=1):
            pass
        def down(self):
            pass
        def up(self):
            pass

    class Page:
        mouse = Mouse()
        def evaluate(self, *args, **kwargs):
            return []

    calls = {}
    monkeypatch.setenv("CAPTCHA_DRAG_STRATEGY", "success_profile")
    monkeypatch.delenv("CAPTCHA_SUCCESS_PROFILE_FINAL_ALIGNMENT", raising=False)
    monkeypatch.setattr(ai_slider.time, "sleep", lambda _seconds: None)

    def fake_profile(mouse, start_x, start_y, distance):
        calls["profile_distance"] = distance

    def fake_alignment(page, mouse, plan, start_x, start_y, distance, max_distance, target_x):
        calls["alignment"] = {"distance": distance, "target_x": target_x}
        return distance - 7.0

    monkeypatch.setattr(ai_slider, "_drag_aliyun_success_profile", fake_profile)
    monkeypatch.setattr(ai_slider, "_apply_final_alignment", fake_alignment)

    distance = _perform_drag(Page(), {
        "start_x": 100,
        "start_y": 70,
        "distance": 168,
        "max_distance": 296,
        "calibrate_target_x": 165.5,
        "source": "ddddocr",
    })

    assert calls["profile_distance"] == pytest.approx(calls["alignment"]["distance"])
    assert calls["alignment"]["target_x"] == pytest.approx(165.5)
    assert distance == pytest.approx(calls["profile_distance"] - 7.0)


def test_perform_drag_fast_quadratic_uses_quick_three_stage_drag(monkeypatch):
    from captcha_solvers import ai_slider
    from captcha_solvers.ai_slider import _perform_drag

    class Mouse:
        def move(self, x, y, steps=1):
            pass
        def down(self):
            pass
        def up(self):
            pass

    class Page:
        mouse = Mouse()
        def evaluate(self, *args, **kwargs):
            return []

    calls = {}
    monkeypatch.setenv("CAPTCHA_DRAG_STRATEGY", "fast_quadratic")
    monkeypatch.setattr(ai_slider.time, "sleep", lambda _seconds: None)

    def fake_fast(mouse, start_x, start_y, distance):
        calls["distance"] = distance

    monkeypatch.setattr(ai_slider, "_drag_aliyun_fast_three_stage", fake_fast)

    distance = _perform_drag(Page(), {
        "start_x": 100,
        "start_y": 70,
        "distance": 152,
        "max_distance": 296,
        "calibrate_target_x": 152,
        "source": "图像匹配校准",
    })

    assert distance == pytest.approx(199, abs=4)
    assert calls["distance"] == pytest.approx(distance)


def test_perform_drag_fast_quadratic_applies_final_alignment(monkeypatch):
    from captcha_solvers import ai_slider
    from captcha_solvers.ai_slider import _perform_drag

    class Mouse:
        def move(self, x, y, steps=1):
            pass
        def down(self):
            pass
        def up(self):
            pass

    class Page:
        mouse = Mouse()
        def evaluate(self, *args, **kwargs):
            return []

    calls = {}
    monkeypatch.setenv("CAPTCHA_DRAG_STRATEGY", "fast_quadratic")
    monkeypatch.setenv("CAPTCHA_FAST_QUADRATIC_FINAL_ALIGNMENT", "1")
    monkeypatch.setattr(ai_slider.time, "sleep", lambda _seconds: None)

    def fake_fast(mouse, start_x, start_y, distance):
        calls["fast_distance"] = distance

    def fake_alignment(page, mouse, plan, start_x, start_y, distance, max_distance, target_x):
        calls["alignment"] = {
            "distance": distance,
            "max_distance": max_distance,
            "target_x": target_x,
        }
        return distance - 5.0

    monkeypatch.setattr(ai_slider, "_drag_aliyun_fast_three_stage", fake_fast)
    monkeypatch.setattr(ai_slider, "_apply_final_alignment", fake_alignment)

    distance = _perform_drag(Page(), {
        "start_x": 100,
        "start_y": 70,
        "distance": 152,
        "max_distance": 296,
        "calibrate_target_x": 152,
        "source": "图像匹配校准",
    })

    assert calls["alignment"]["distance"] == pytest.approx(calls["fast_distance"])
    assert calls["alignment"]["target_x"] == 152
    assert distance == pytest.approx(calls["fast_distance"] - 5.0)


def test_perform_drag_fast_quadratic_skips_final_alignment_by_default(monkeypatch):
    from captcha_solvers import ai_slider
    from captcha_solvers.ai_slider import _perform_drag

    class Mouse:
        def move(self, x, y, steps=1):
            pass
        def down(self):
            pass
        def up(self):
            pass

    class Page:
        mouse = Mouse()
        def evaluate(self, *args, **kwargs):
            return []

    calls = {}
    monkeypatch.setenv("CAPTCHA_DRAG_STRATEGY", "fast_quadratic")
    monkeypatch.delenv("CAPTCHA_FAST_QUADRATIC_FINAL_ALIGNMENT", raising=False)
    monkeypatch.setattr(ai_slider.time, "sleep", lambda _seconds: None)
    monkeypatch.setattr(ai_slider, "_drag_aliyun_fast_three_stage", lambda *_args: None)

    def fake_alignment(*_args, **_kwargs):
        calls["alignment"] = True
        return 1.0

    monkeypatch.setattr(ai_slider, "_apply_final_alignment", fake_alignment)

    distance = _perform_drag(Page(), {
        "start_x": 100,
        "start_y": 70,
        "distance": 152,
        "max_distance": 296,
        "calibrate_target_x": 152,
        "source": "图像匹配校准",
    })

    assert calls.get("alignment") is None
    assert distance == pytest.approx(199, abs=4)


def test_fast_quadratic_uses_short_three_stage_profile(monkeypatch):
    from captcha_solvers import ai_slider

    class Mouse:
        def __init__(self):
            self.moves = []

        def move(self, x, y, steps=1):
            self.moves.append((float(x), float(y), int(steps)))

    sleeps = []
    monkeypatch.setattr(ai_slider.time, "sleep", lambda seconds: sleeps.append(float(seconds)))
    monkeypatch.setattr(ai_slider.random, "uniform", lambda a, b: (a + b) / 2)
    monkeypatch.setattr(ai_slider.random, "randint", lambda a, b: (a + b) // 2)

    mouse = Mouse()
    ai_slider._drag_aliyun_fast_three_stage(mouse, 100.0, 70.0, 220.0)

    offsets = [x - 100.0 for x, _y, _steps in mouse.moves]
    assert 9 <= len(offsets) <= 18
    assert offsets[-1] == pytest.approx(220.0)
    assert max(offsets) <= 220.0
    assert all(offsets[index] >= offsets[index - 1] for index in range(1, len(offsets)))
    assert 0.35 <= sum(sleeps) <= 1.2


def test_perform_drag_os_backend_aborts_when_down_event_misses_page(monkeypatch):
    from captcha_solvers import ai_slider
    from captcha_solvers.ai_slider import _WindowsOsMouseAdapter, _perform_drag

    class User32:
        def SetCursorPos(self, _x, _y):
            return 1

        def mouse_event(self, *_args):
            return 1

    class Page:
        def __init__(self):
            self.mouse = object()

        def evaluate(self, script, *args, **kwargs):
            if "__captchaDragEvents" in script:
                return []
            if "aliyunCaptcha-sliding-slider" in script:
                return {"sliderBoxX": 490, "puzzleBoxX": 490, "sliderLeft": 0, "puzzleLeft": 0}
            return []

    adapter = _WindowsOsMouseAdapter(
        User32(),
        origin_css_x=100,
        origin_css_y=70,
        origin_screen_x=100,
        origin_screen_y=70,
        scale_x=1,
        scale_y=1,
    )
    monkeypatch.setattr(ai_slider, "_create_os_mouse_adapter", lambda *args, **kwargs: adapter)
    monkeypatch.setattr(ai_slider, "ensure_page_topmost_foreground", lambda *args, **kwargs: True)
    monkeypatch.setattr(ai_slider.time, "sleep", lambda _seconds: None)

    with pytest.raises(RuntimeError, match="未命中"):
        _perform_drag(Page(), {
            "start_x": 100,
            "start_y": 70,
            "distance": 152,
            "max_distance": 296,
            "calibrate_target_x": 152,
            "source": "图像匹配校准",
            "label": "[测试]",
        })


def test_viewport_screen_metrics_prefers_real_hwnd_rect_over_dom_screen(monkeypatch):
    from captcha_solvers import ai_slider

    class Page:
        def evaluate(self, _script):
            return {
                # Camoufox/Firefox 指纹层可能让 window.screenX/Y 与真实窗口位置不一致。
                "offsetX": 1020,
                "offsetY": 840,
                "outerWidth": 1280,
                "outerHeight": 800,
                "innerWidth": 1240,
                "innerHeight": 720,
            }

    monkeypatch.setattr(
        ai_slider,
        "get_page_window_rect",
        lambda _page: {"left": 40, "top": 70, "right": 1320, "bottom": 870},
        raising=False,
    )

    metrics = ai_slider._get_viewport_screen_metrics(Page())

    assert metrics is not None
    # borderX=(1280-1240)/2=20; topChrome=800-720-20=60
    assert metrics["offsetX"] == pytest.approx(60)
    assert metrics["offsetY"] == pytest.approx(130)


def test_drag_events_out_of_bounds_detects_mouse_jump():
    from captcha_solvers.ai_slider import _drag_events_out_of_bounds

    class Page:
        def evaluate(self, _script):
            return [
                {"kind": "pointermove", "x": 510, "y": 538},
                {"kind": "mousemove", "x": 1343, "y": 974},
            ]

    message = _drag_events_out_of_bounds(Page(), start_x=510, start_y=538, max_distance=296)

    assert message is not None
    assert "x=1343.0" in message


def test_drag_events_out_of_bounds_allows_normal_slider_motion():
    from captcha_solvers.ai_slider import _drag_events_out_of_bounds

    class Page:
        def evaluate(self, _script):
            return [
                {"kind": "pointermove", "x": 510, "y": 538},
                {"kind": "mousemove", "x": 760, "y": 537},
            ]

    assert _drag_events_out_of_bounds(Page(), start_x=510, start_y=538, max_distance=296) is None


def test_target_x_alternatives_try_near_template_before_far_white_gap():
    from captcha_solvers.ai_slider import _target_x_alternatives

    alternatives = _target_x_alternatives(
        chosen=194.5,
        image_width=296,
        ddddocr_x=194.5,
        white_gap_x=240.0,
        match_x=200.0,
    )

    assert alternatives[:3] == pytest.approx([194.5, 200.0, 240.0])




def test_solve_slider_captcha_rechecks_disappeared_captcha_before_next_attempt(tmp_path, monkeypatch):
    from captcha_solvers import ai_slider

    monkeypatch.setenv("CAPTCHA_TARGET_RIGHT_BIAS", "0")

    class Root:
        pass

    calls = {"perform": 0, "solved": 0}
    monkeypatch.setattr(ai_slider, "_find_captcha_root", lambda _page: Root())
    monkeypatch.setattr(ai_slider, "_wait_for_captcha_ready", lambda *args, **kwargs: True)
    monkeypatch.setattr(ai_slider, "_screenshot_locator", lambda _root, path: Path(path).write_bytes(b"fake"))
    monkeypatch.setattr(ai_slider, "_wait_for_captcha_success", lambda *args, **kwargs: None)
    monkeypatch.setattr(ai_slider.time, "sleep", lambda _seconds: None)
    monkeypatch.setattr(ai_slider, "_sleep", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(ai_slider, "_build_drag_plan", lambda *_args: {
        "start_x": 10.0,
        "start_y": 20.0,
        "distance": 194.5,
        "max_distance": 296.0,
        "calibrate_target_x": 194.5,
        "target_scale": 1.0,
        "target_x_alternatives": [194.5, 200.0, 240.0],
        "source": "ddddocr",
    })

    def fake_perform(_page, plan):
        calls["perform"] += 1
        plan["hold_state"] = {
            "sliderLeft": float(plan["calibrate_target_x"]),
            "puzzleLeft": float(plan["calibrate_target_x"]),
            "sliderBoxX": 500.0,
            "puzzleBoxX": 500.0 + float(plan["calibrate_target_x"]),
        }
        return float(plan["distance"])

    def fake_solved(_page):
        calls["solved"] += 1
        return calls["solved"] >= 2

    monkeypatch.setattr(ai_slider, "_perform_drag", fake_perform)
    monkeypatch.setattr(ai_slider, "_captcha_solved", fake_solved)

    result = solve_slider_captcha(
        object(),
        CaptchaSolverConfig(enabled=True, attempts=3, mode="ddddocr"),
        image_dir=str(tmp_path),
    )

    assert result.ok is True
    assert result.attempts == 1
    assert calls["perform"] == 1


def test_solve_slider_captcha_rotates_target_x_alternatives_across_retries(tmp_path, monkeypatch):
    from captcha_solvers import ai_slider

    monkeypatch.setenv("CAPTCHA_TARGET_RIGHT_BIAS", "0")

    class Root:
        pass

    attempted_targets = []
    monkeypatch.setattr(ai_slider, "_find_captcha_root", lambda _page: Root())
    monkeypatch.setattr(ai_slider, "_wait_for_captcha_ready", lambda *args, **kwargs: True)
    monkeypatch.setattr(ai_slider, "_screenshot_locator", lambda _root, path: Path(path).write_bytes(b"fake"))
    monkeypatch.setattr(ai_slider, "_wait_for_captcha_success", lambda *args, **kwargs: None)
    monkeypatch.setattr(ai_slider.time, "sleep", lambda _seconds: None)
    monkeypatch.setattr(ai_slider, "_sleep", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(ai_slider, "_build_drag_plan", lambda *_args: {
        "start_x": 10.0,
        "start_y": 20.0,
        "distance": 194.5,
        "max_distance": 296.0,
        "calibrate_target_x": 194.5,
        "target_scale": 1.0,
        "target_x_alternatives": [194.5, 200.0, 240.0],
        "source": "ddddocr",
    })

    def fake_perform(_page, plan):
        attempted_targets.append(float(plan["calibrate_target_x"]))
        plan["hold_state"] = {
            "sliderLeft": float(plan["calibrate_target_x"]),
            "puzzleLeft": float(plan["calibrate_target_x"]),
            "sliderBoxX": 500.0,
            "puzzleBoxX": 500.0 + float(plan["calibrate_target_x"]),
        }
        return float(plan["distance"])

    monkeypatch.setattr(ai_slider, "_perform_drag", fake_perform)

    result = solve_slider_captcha(
        object(),
        CaptchaSolverConfig(enabled=True, attempts=3, mode="ddddocr"),
        image_dir=str(tmp_path),
    )

    assert result.ok is False
    assert attempted_targets == pytest.approx([194.5, 200.0, 240.0])


def test_solve_slider_captcha_rotates_original_targets_when_detection_jitters(tmp_path, monkeypatch):
    from captcha_solvers import ai_slider

    monkeypatch.setenv("CAPTCHA_TARGET_RIGHT_BIAS", "0")

    class Root:
        pass

    plans = [
        [194.5, 200.0, 240.0],
        [194.8, 201.0, 241.0],
        [194.2, 199.0, 239.0],
    ]
    attempted_targets = []
    build_calls = {"count": 0}
    monkeypatch.setattr(ai_slider, "_find_captcha_root", lambda _page: Root())
    monkeypatch.setattr(ai_slider, "_wait_for_captcha_ready", lambda *args, **kwargs: True)
    monkeypatch.setattr(ai_slider, "_screenshot_locator", lambda _root, path: Path(path).write_bytes(b"fake"))
    monkeypatch.setattr(ai_slider, "_wait_for_captcha_success", lambda *args, **kwargs: None)
    monkeypatch.setattr(ai_slider.time, "sleep", lambda _seconds: None)
    monkeypatch.setattr(ai_slider, "_sleep", lambda *_args, **_kwargs: None)

    def fake_build_drag_plan(*_args):
        index = min(build_calls["count"], len(plans) - 1)
        build_calls["count"] += 1
        alternatives = plans[index]
        return {
            "start_x": 10.0,
            "start_y": 20.0,
            "distance": alternatives[0],
            "max_distance": 296.0,
            "calibrate_target_x": alternatives[0],
            "target_scale": 1.0,
            "target_x_alternatives": alternatives,
            "source": "ddddocr",
            "ddddocr_target_x": alternatives[0],
            "white_gap_target_x": None,
            "match_target_x": alternatives[1],
        }

    monkeypatch.setattr(ai_slider, "_build_drag_plan", fake_build_drag_plan)

    def fake_perform(_page, plan):
        attempted_targets.append(float(plan["calibrate_target_x"]))
        plan["hold_state"] = {
            "sliderLeft": float(plan["calibrate_target_x"]),
            "puzzleLeft": float(plan["calibrate_target_x"]),
            "sliderBoxX": 500.0,
            "puzzleBoxX": 500.0 + float(plan["calibrate_target_x"]),
        }
        return float(plan["distance"])

    monkeypatch.setattr(ai_slider, "_perform_drag", fake_perform)

    result = solve_slider_captcha(
        object(),
        CaptchaSolverConfig(enabled=True, attempts=3, mode="ddddocr"),
        image_dir=str(tmp_path),
    )

    assert result.ok is False
    assert attempted_targets == pytest.approx([194.5, 200.0, 240.0])


def test_solve_slider_captcha_keeps_same_image_while_target_alternatives_remain(tmp_path, monkeypatch):
    from captcha_solvers import ai_slider

    monkeypatch.setenv("CAPTCHA_TARGET_RIGHT_BIAS", "0")

    class Root:
        pass

    attempted_targets = []
    refresh_calls = []
    monkeypatch.setattr(ai_slider, "_find_captcha_root", lambda _page: Root())
    monkeypatch.setattr(ai_slider, "_wait_for_captcha_ready", lambda *args, **kwargs: True)
    monkeypatch.setattr(ai_slider, "_screenshot_locator", lambda _root, path: Path(path).write_bytes(b"fake"))
    monkeypatch.setattr(ai_slider, "_wait_for_captcha_success", lambda *args, **kwargs: None)
    monkeypatch.setattr(ai_slider.time, "sleep", lambda _seconds: None)
    monkeypatch.setattr(ai_slider, "_sleep", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(ai_slider, "_try_refresh_captcha", lambda _root: refresh_calls.append("refresh"))
    monkeypatch.setattr(ai_slider, "_build_drag_plan", lambda *_args: {
        "start_x": 10.0,
        "start_y": 20.0,
        "distance": 194.5,
        "max_distance": 296.0,
        "calibrate_target_x": 194.5,
        "target_scale": 1.0,
        "target_x_alternatives": [194.5, 204.5],
        "source": "ddddocr",
    })

    def fake_perform(_page, plan):
        attempted_targets.append(float(plan["calibrate_target_x"]))
        plan["hold_state"] = {
            "sliderLeft": float(plan["calibrate_target_x"]),
            "puzzleLeft": float(plan["calibrate_target_x"]),
            "sliderBoxX": 500.0,
            "puzzleBoxX": 500.0 + float(plan["calibrate_target_x"]),
        }
        return float(plan["distance"])

    monkeypatch.setattr(ai_slider, "_perform_drag", fake_perform)

    result = solve_slider_captcha(
        object(),
        CaptchaSolverConfig(enabled=True, attempts=2, mode="ddddocr"),
        image_dir=str(tmp_path),
    )

    assert result.ok is False
    assert attempted_targets == pytest.approx([194.5, 204.5])
    assert refresh_calls == []


def test_solve_slider_captcha_retries_focus_miss_without_consuming_attempt(tmp_path, monkeypatch):
    from captcha_solvers import ai_slider

    monkeypatch.setenv("CAPTCHA_TARGET_RIGHT_BIAS", "0")

    class Root:
        pass

    drag_calls = []
    focus_calls = []
    monkeypatch.setattr(ai_slider, "_find_captcha_root", lambda _page: Root())
    monkeypatch.setattr(ai_slider, "_wait_for_captcha_ready", lambda *args, **kwargs: True)
    monkeypatch.setattr(ai_slider, "_screenshot_locator", lambda _root, path: Path(path).write_bytes(b"fake"))
    monkeypatch.setattr(ai_slider, "_wait_for_captcha_success", lambda *args, **kwargs: "页面")
    monkeypatch.setattr(ai_slider.time, "sleep", lambda _seconds: None)
    monkeypatch.setattr(ai_slider, "_sleep", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(ai_slider, "_build_drag_plan", lambda *_args: {
        "start_x": 10.0,
        "start_y": 20.0,
        "distance": 180.0,
        "max_distance": 296.0,
        "calibrate_target_x": 180.0,
        "target_scale": 1.0,
        "target_x_alternatives": [180.0],
        "source": "ddddocr",
        "label": "[测试]",
    })
    monkeypatch.setattr(ai_slider, "ensure_page_topmost_foreground", lambda *args, **kwargs: focus_calls.append(kwargs) or True)

    def fake_perform(_page, plan):
        drag_calls.append(float(plan["calibrate_target_x"]))
        if len(drag_calls) == 1:
            plan["focus_missed"] = True
            raise RuntimeError("OS 鼠标按下未命中滑块窗口")
        plan["hold_state"] = {
            "sliderLeft": 180.0,
            "puzzleLeft": 180.0,
            "sliderBoxX": 500.0,
            "puzzleBoxX": 680.0,
        }
        return float(plan["distance"])

    monkeypatch.setattr(ai_slider, "_perform_drag", fake_perform)

    result = solve_slider_captcha(
        object(),
        CaptchaSolverConfig(enabled=True, attempts=1, mode="ddddocr"),
        image_dir=str(tmp_path),
    )

    assert result.ok is True
    assert drag_calls == pytest.approx([180.0, 180.0])
    assert len(focus_calls) == 1


def test_solve_slider_captcha_counts_local_detection_failures(tmp_path, monkeypatch):
    from captcha_solvers import ai_slider

    class Root:
        pass

    build_calls = []
    refresh_calls = []
    monkeypatch.setattr(ai_slider, "_find_captcha_root", lambda _page: Root())
    monkeypatch.setattr(ai_slider, "_wait_for_captcha_ready", lambda *args, **kwargs: True)
    monkeypatch.setattr(ai_slider, "_screenshot_locator", lambda _root, path: Path(path).write_bytes(b"fake"))
    monkeypatch.setattr(ai_slider.time, "sleep", lambda _seconds: None)
    monkeypatch.setattr(ai_slider, "_sleep", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(ai_slider, "_try_refresh_captcha", lambda _root: refresh_calls.append("refresh"))

    def fake_build(*_args):
        build_calls.append("build")
        if len(build_calls) > 3:
            pytest.fail("本地识别失败不应超过配置尝试次数")
        return {"start_x": 10.0, "start_y": 20.0, "distance": 180.0, "max_distance": 296.0, "source": "unknown"}

    monkeypatch.setattr(ai_slider, "_build_drag_plan", fake_build)

    result = solve_slider_captcha(
        object(),
        CaptchaSolverConfig(enabled=True, attempts=3, mode="ddddocr"),
        image_dir=str(tmp_path),
    )

    assert result.ok is False
    assert "ddddocr 本地识别未能得到可用滑块缺口" in result.message
    assert len(build_calls) == 3
    assert len(refresh_calls) == 3

def test_solve_slider_captcha_focus_miss_does_not_consume_config_attempts(tmp_path, monkeypatch):
    from captcha_solvers import ai_slider

    monkeypatch.setenv("CAPTCHA_TARGET_RIGHT_BIAS", "0")

    class Root:
        pass

    drag_calls = []
    refresh_calls = []
    monkeypatch.setattr(ai_slider, "_find_captcha_root", lambda _page: Root())
    monkeypatch.setattr(ai_slider, "_wait_for_captcha_ready", lambda *args, **kwargs: True)
    monkeypatch.setattr(ai_slider, "_screenshot_locator", lambda _root, path: Path(path).write_bytes(b"fake"))
    monkeypatch.setattr(ai_slider, "_wait_for_captcha_success", lambda *args, **kwargs: "页面")
    monkeypatch.setattr(ai_slider.time, "sleep", lambda _seconds: None)
    monkeypatch.setattr(ai_slider, "_sleep", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(ai_slider, "_try_refresh_captcha", lambda _root: refresh_calls.append("refresh"))
    monkeypatch.setattr(ai_slider, "ensure_page_topmost_foreground", lambda *args, **kwargs: True)
    monkeypatch.setattr(ai_slider, "_build_drag_plan", lambda *_args: {
        "start_x": 10.0,
        "start_y": 20.0,
        "distance": 180.0,
        "max_distance": 296.0,
        "calibrate_target_x": 180.0,
        "target_scale": 1.0,
        "target_x_alternatives": [180.0],
        "source": "ddddocr",
        "label": "[测试]",
    })

    def fake_perform(_page, plan):
        drag_calls.append(float(plan["calibrate_target_x"]))
        if len(drag_calls) <= 2:
            plan["focus_missed"] = True
            raise RuntimeError("OS 鼠标按下未命中滑块窗口")
        plan["hold_state"] = {
            "sliderLeft": 180.0,
            "puzzleLeft": 180.0,
            "sliderBoxX": 500.0,
            "puzzleBoxX": 680.0,
        }
        return float(plan["distance"])

    monkeypatch.setattr(ai_slider, "_perform_drag", fake_perform)

    result = solve_slider_captcha(
        object(),
        CaptchaSolverConfig(enabled=True, attempts=1, mode="ddddocr"),
        image_dir=str(tmp_path),
    )

    assert result.ok is True
    assert drag_calls == pytest.approx([180.0, 180.0, 180.0])
    assert refresh_calls == []


def test_drag_track_anomaly_is_treated_as_focus_miss():
    from captcha_solvers import ai_slider

    assert ai_slider._is_focus_miss_drag_exception(
        RuntimeError("OS 鼠标轨迹异常: x=100.0, y=86.0, 允许范围 x=[470.0,900.0] y=[370.8,730.8]"),
        {},
    ) is True


def test_zero_motion_state_with_missing_puzzle_box_is_focus_miss():
    """真实日志样本：sliderLeft/puzzleLeft 都为 0 且 puzzleBoxX=0，应快速重试而不是等待判定失败。"""
    from captcha_solvers import ai_slider

    assert ai_slider._motion_state_looks_focus_missed({
        "sliderLeft": 0.0,
        "puzzleLeft": 0.0,
        "sliderBoxX": 490.0,
        "puzzleBoxX": 0.0,
    }) is True


def test_default_target_bias_compensates_observed_right_overshoot(monkeypatch):
    from captcha_solvers import ai_slider

    monkeypatch.delenv("CAPTCHA_TARGET_RIGHT_BIAS", raising=False)

    assert ai_slider._configured_target_right_bias() == -5.0
    assert ai_slider._apply_target_right_bias(200.0, 300.0) == 195.0

def test_solve_slider_captcha_applies_configured_target_right_bias(tmp_path, monkeypatch):
    from captcha_solvers import ai_slider

    class Root:
        pass

    attempted_targets = []
    monkeypatch.setenv("CAPTCHA_TARGET_RIGHT_BIAS", "6")
    monkeypatch.setattr(ai_slider, "_find_captcha_root", lambda _page: Root())
    monkeypatch.setattr(ai_slider, "_wait_for_captcha_ready", lambda *args, **kwargs: True)
    monkeypatch.setattr(ai_slider, "_screenshot_locator", lambda _root, path: Path(path).write_bytes(b"fake"))
    monkeypatch.setattr(ai_slider, "_wait_for_captcha_success", lambda *args, **kwargs: None)
    monkeypatch.setattr(ai_slider.time, "sleep", lambda _seconds: None)
    monkeypatch.setattr(ai_slider, "_sleep", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(ai_slider, "_build_drag_plan", lambda *_args: {
        "start_x": 10.0,
        "start_y": 20.0,
        "distance": 180.0,
        "max_distance": 296.0,
        "calibrate_target_x": 180.0,
        "target_scale": 1.0,
        "target_x_alternatives": [180.0],
        "ddddocr_target_x": 180.0,
        "white_gap_target_x": None,
        "match_target_x": None,
        "source": "ddddocr",
    })

    def fake_perform(_page, plan):
        attempted_targets.append(float(plan["calibrate_target_x"]))
        plan["hold_state"] = {
            "sliderLeft": 186.0,
            "puzzleLeft": 186.0,
            "sliderBoxX": 500.0,
            "puzzleBoxX": 686.0,
        }
        return float(plan["distance"])

    monkeypatch.setattr(ai_slider, "_perform_drag", fake_perform)

    solve_slider_captcha(
        object(),
        CaptchaSolverConfig(enabled=True, attempts=1, mode="ddddocr"),
        image_dir=str(tmp_path),
    )

    assert attempted_targets == pytest.approx([186.0])


def test_solve_slider_captcha_resets_target_choice_for_new_captcha_signature(tmp_path, monkeypatch):
    from captcha_solvers import ai_slider

    monkeypatch.setenv("CAPTCHA_TARGET_RIGHT_BIAS", "0")

    class Root:
        pass

    plans = [
        {
            "start_x": 10.0,
            "start_y": 20.0,
            "distance": 194.5,
            "max_distance": 296.0,
            "calibrate_target_x": 194.5,
            "target_scale": 1.0,
            "target_x_alternatives": [194.5, 200.0, 240.0],
            "ddddocr_target_x": 194.5,
            "white_gap_target_x": 240.0,
            "match_target_x": 200.0,
            "source": "ddddocr",
        },
        {
            "start_x": 10.0,
            "start_y": 20.0,
            "distance": 236.5,
            "max_distance": 296.0,
            "calibrate_target_x": 236.5,
            "target_scale": 1.0,
            "target_x_alternatives": [236.5, 196.0],
            "ddddocr_target_x": 236.5,
            "white_gap_target_x": None,
            "match_target_x": 196.0,
            "source": "ddddocr",
        },
    ]
    attempted_targets = []
    build_count = {"value": 0}
    monkeypatch.setattr(ai_slider, "_find_captcha_root", lambda _page: Root())
    monkeypatch.setattr(ai_slider, "_wait_for_captcha_ready", lambda *args, **kwargs: True)
    monkeypatch.setattr(ai_slider, "_screenshot_locator", lambda _root, path: Path(path).write_bytes(b"fake"))
    monkeypatch.setattr(ai_slider, "_wait_for_captcha_success", lambda *args, **kwargs: None)
    monkeypatch.setattr(ai_slider.time, "sleep", lambda _seconds: None)
    monkeypatch.setattr(ai_slider, "_sleep", lambda *_args, **_kwargs: None)

    def fake_build_drag_plan(*_args):
        index = min(build_count["value"], len(plans) - 1)
        build_count["value"] += 1
        return dict(plans[index])

    def fake_perform(_page, plan):
        attempted_targets.append(float(plan["calibrate_target_x"]))
        plan["hold_state"] = {
            "sliderLeft": float(plan["calibrate_target_x"]),
            "puzzleLeft": float(plan["calibrate_target_x"]),
            "sliderBoxX": 500.0,
            "puzzleBoxX": 500.0 + float(plan["calibrate_target_x"]),
        }
        return float(plan["distance"])

    monkeypatch.setattr(ai_slider, "_build_drag_plan", fake_build_drag_plan)
    monkeypatch.setattr(ai_slider, "_perform_drag", fake_perform)

    result = solve_slider_captcha(
        object(),
        CaptchaSolverConfig(enabled=True, attempts=2, mode="ddddocr"),
        image_dir=str(tmp_path),
    )

    assert result.ok is False
    assert attempted_targets == pytest.approx([194.5, 236.5])


def test_final_alignment_records_steps_until_puzzle_near_target(monkeypatch):
    from captcha_solvers.ai_slider import _apply_final_alignment

    class Mouse:
        def __init__(self):
            self.current = 120.0

        def move(self, x, _y, steps=1):
            self.current = float(x) - 100.0

    class Page:
        def __init__(self, mouse):
            self.mouse = mouse

        def evaluate(self, script, *args, **kwargs):
            if "aliyunCaptcha-sliding-slider" in script:
                puzzle_left = self.mouse.current * 0.72
                return {
                    "sliderBoxX": self.mouse.current,
                    "puzzleBoxX": puzzle_left,
                    "sliderLeft": self.mouse.current,
                    "puzzleLeft": puzzle_left,
                }
            return []

    monkeypatch.setattr("captcha_solvers.ai_slider.time.sleep", lambda _seconds: None)
    monkeypatch.setattr("captcha_solvers.ai_slider.random.randint", lambda _a, _b: 4)
    monkeypatch.setattr("captcha_solvers.ai_slider.random.uniform", lambda a, b: (a + b) / 2)
    mouse = Mouse()
    plan = {"fast_quadratic": True}

    final_distance = _apply_final_alignment(
        Page(mouse),
        mouse,
        plan,
        start_x=100,
        start_y=70,
        distance=120,
        max_distance=296,
        target_x=180,
    )

    assert final_distance > 120
    assert plan["alignment_steps"]
    final_delta = plan["alignment_steps"][-1]["delta"]
    assert 0.3 <= final_delta <= 2.5


def test_alignment_accepts_small_right_bias_to_avoid_over_correction(monkeypatch):
    from captcha_solvers.ai_slider import _alignment_delta_is_acceptable

    monkeypatch.delenv("CAPTCHA_ALIGNMENT_RIGHT_TOLERANCE", raising=False)

    assert _alignment_delta_is_acceptable(-1.9) is True
    assert _alignment_delta_is_acceptable(-3.1) is False


def test_drag_debug_artifacts_disabled_by_default(tmp_path, monkeypatch):
    from captcha_solvers import ai_slider

    class Root:
        def screenshot(self, path):
            Path(path).write_bytes(b"debug")

    class Page:
        def evaluate(self, _script):
            return [{"type": "mousemove"}]

    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("CAPTCHA_DEBUG_DRAG_ARTIFACTS", raising=False)

    assert ai_slider._capture_hold_screenshot(Page(), label="测试") is None
    assert ai_slider._dump_drag_event_debug(Page(), label="测试") is None
    assert not (tmp_path / "images" / "aliyun_probe").exists()


def test_drag_debug_artifacts_can_be_enabled(tmp_path, monkeypatch):
    from captcha_solvers import ai_slider

    class Page:
        def evaluate(self, script):
            if "closest" in script:
                return True
            return [{"type": "mousemove"}]

    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("CAPTCHA_DEBUG_DRAG_ARTIFACTS", "1")
    monkeypatch.setattr(ai_slider, "_find_captcha_root", lambda _page: object())
    monkeypatch.setattr(ai_slider, "_screenshot_locator", lambda _root, path: Path(path).write_bytes(b"debug"))

    assert ai_slider._capture_hold_screenshot(Page(), label="测试")
    assert ai_slider._dump_drag_event_debug(Page(), label="测试")
    assert (tmp_path / "images" / "aliyun_probe").exists()


def test_aliyun_dom_images_are_not_saved_by_default(tmp_path, monkeypatch):
    """生产路径默认只保存尝试元数据/截图，不无条件落盘背景图和拼图片。"""

    from captcha_solvers import ai_slider

    bg = Image.new("RGBA", (300, 180), (120, 130, 140, 255))
    puzzle = Image.new("RGBA", (50, 50), (200, 200, 200, 255))

    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("CAPTCHA_DEBUG_ALIYUN_IMAGES", raising=False)

    ai_slider._save_debug_aliyun_images("images/aliyun_probe/dom_test", bg, puzzle)

    assert not (tmp_path / "images" / "aliyun_probe").exists()


def test_release_backoff_moves_left_when_mouseup_would_settle_right(monkeypatch):
    from captcha_solvers import ai_slider

    states = [
        {"sliderLeft": 236.0, "puzzleLeft": 215.9, "sliderBoxX": 726.0, "puzzleBoxX": 705.9},
        {"sliderLeft": 234.0, "puzzleLeft": 212.4, "sliderBoxX": 724.0, "puzzleBoxX": 702.4},
    ]
    moves = []

    def fake_drag_segment(_mouse, _start_x, _start_y, from_offset, to_offset, *, steps, y_amplitude):
        moves.append((from_offset, to_offset, steps, y_amplitude))

    monkeypatch.setenv("CAPTCHA_ALIGNMENT_RELEASE_MIN_DELTA", "1.2")
    monkeypatch.setattr(ai_slider, "_drag_segment", fake_drag_segment)
    monkeypatch.setattr(ai_slider, "_get_aliyun_motion_state", lambda _page: states[-1])
    monkeypatch.setattr(ai_slider.time, "sleep", lambda _seconds: None)
    monkeypatch.setattr(ai_slider.random, "uniform", lambda a, b: (a + b) / 2)

    plan = {}
    new_distance, new_state = ai_slider._apply_release_backoff_if_needed(
        page=object(),
        mouse=object(),
        plan=plan,
        start_x=510.0,
        start_y=545.0,
        distance=236.0,
        max_distance=296.0,
        target_x=214.5,
        state=states[0],
    )

    assert new_distance == pytest.approx(234.0)
    assert new_state == states[-1]
    assert moves == [(236.0, 234.0, 3, 0.8)]
    assert plan["release_backoff"]["before_delta"] == pytest.approx(-1.4)
    assert plan["release_backoff"]["after_delta"] == pytest.approx(2.1)


def test_release_min_delta_defaults_to_no_forced_left_bias(monkeypatch):
    from captcha_solvers import ai_slider

    monkeypatch.delenv("CAPTCHA_ALIGNMENT_RELEASE_MIN_DELTA", raising=False)

    assert ai_slider._alignment_release_min_delta() == pytest.approx(-10.0)


def test_capture_hold_screenshot_writes_captcha_region(tmp_path, monkeypatch):
    from captcha_solvers import ai_slider

    class Root:
        pass

    monkeypatch.setattr(ai_slider, "_find_captcha_root", lambda _page: Root())
    monkeypatch.setattr(ai_slider, "_screenshot_locator", lambda _root, path: Path(path).write_bytes(b"png"))
    monkeypatch.setenv("CAPTCHA_DEBUG_DRAG_ARTIFACTS", "1")

    path = ai_slider._capture_hold_screenshot(object(), image_dir=str(tmp_path), label="[测试]")

    assert path is not None
    assert Path(path).read_bytes() == b"png"
    assert Path(path).name.startswith("hold_state_")


def test_perform_drag_can_disable_closed_loop_for_aliyun(monkeypatch):
    from captcha_solvers.ai_slider import _perform_drag

    class Page:
        def __init__(self):
            self.mouse = FakeMouse()

        def evaluate(self, _script):
            return {"sliderBoxX": 150, "puzzleBoxX": 150, "sliderLeft": 150, "puzzleLeft": 150}

    page = Page()
    calls = {"closed_loop": 0, "human": 0}
    monkeypatch.setenv("CAPTCHA_DRAG_STRATEGY", "human")
    monkeypatch.setattr("captcha_solvers.ai_slider.time.sleep", lambda _seconds: None)
    monkeypatch.setattr("captcha_solvers.ai_slider._drag_aliyun_closed_loop", lambda *args, **kwargs: calls.__setitem__("closed_loop", calls["closed_loop"] + 1) or 999)
    monkeypatch.setattr("captcha_solvers.ai_slider._drag_aliyun_human_like", lambda mouse, sx, sy, distance: calls.__setitem__("human", calls["human"] + 1) or mouse.move(sx + distance, sy))

    distance = _perform_drag(page, {
        "start_x": 100,
        "start_y": 70,
        "distance": 150,
        "max_distance": 300,
        "calibrate_target_x": 150,
        "source": "图像匹配校准",
    })

    assert distance == 150
    assert calls["human"] == 1
    assert calls["closed_loop"] == 0


def test_human_like_drag_includes_y_jitter_and_small_backtrack(monkeypatch):
    from captcha_solvers.ai_slider import _drag_aliyun_human_like

    mouse = FakeMouse()
    sleeps = []
    monkeypatch.setattr("captcha_solvers.ai_slider.time.sleep", lambda seconds: sleeps.append(seconds))

    _drag_aliyun_human_like(mouse, start_x=100, start_y=70, distance=180)

    moves = [(event[1], event[2]) for event in mouse.events if event[0] == "move"]
    xs = [x for x, _y in moves]
    ys = [y for _x, y in moves]
    assert max(ys) - min(ys) >= 3
    assert any(xs[index] < xs[index - 1] for index in range(1, len(xs)))


def test_solve_slider_captcha_waits_for_delayed_success_after_drag(tmp_path, monkeypatch):
    from captcha_solvers import ai_slider

    class Root:
        pass

    class Page:
        def __init__(self):
            self.mouse = type("Mouse", (), {"move": lambda *args, **kwargs: None, "down": lambda *args, **kwargs: None, "up": lambda *args, **kwargs: None})()

    solved_calls = {"count": 0}

    def fake_solved(_page):
        solved_calls["count"] += 1
        return solved_calls["count"] >= 4

    page = Page()
    monkeypatch.setattr(ai_slider, "_find_captcha_root", lambda _page: Root())
    monkeypatch.setattr(ai_slider, "_wait_for_captcha_ready", lambda *args, **kwargs: True)
    monkeypatch.setattr(ai_slider, "_screenshot_locator", lambda _root, path: Path(path).write_bytes(b"fake"))
    monkeypatch.setattr(ai_slider, "_build_drag_plan", lambda *_args: {
        "start_x": 10,
        "start_y": 10,
        "distance": 100,
        "max_distance": 300,
        "source": "图像匹配校准",
    })
    monkeypatch.setattr(ai_slider, "_perform_drag", lambda *_args: 100)
    monkeypatch.setattr(ai_slider, "_captcha_solved", fake_solved)
    monkeypatch.setattr(ai_slider, "_sleep", lambda _seconds, _stop_event=None: None)
    monkeypatch.setattr(ai_slider.time, "sleep", lambda _seconds: None)

    result = ai_slider.solve_slider_captcha(
        page,
        ai_slider.CaptchaSolverConfig(enabled=True, attempts=1),
        image_dir=str(tmp_path),
    )

    assert result.ok is True
    assert solved_calls["count"] >= 4


def test_wait_for_captcha_success_uses_short_initial_failure_window(monkeypatch):
    from captcha_solvers import ai_slider

    now = {"value": 0.0}
    sleeps = []
    monkeypatch.delenv("CAPTCHA_INITIAL_FAILURE_WAIT_SECONDS", raising=False)
    monkeypatch.setattr(ai_slider.time, "time", lambda: now["value"])
    monkeypatch.setattr(ai_slider, "_captcha_solved", lambda _page: False)
    monkeypatch.setattr(ai_slider, "_captcha_explicitly_failed", lambda _page: False)

    def fake_sleep(seconds, _stop_event=None):
        sleeps.append(float(seconds))
        now["value"] += float(seconds)

    monkeypatch.setattr(ai_slider, "_sleep", fake_sleep)

    assert ai_slider._wait_for_captcha_success(object(), timeout=2.8) is None
    assert now["value"] <= 2.3
    assert sleeps


def test_wait_for_captcha_success_extends_when_success_appears(monkeypatch):
    from captcha_solvers import ai_slider

    now = {"value": 0.0}
    monkeypatch.delenv("CAPTCHA_INITIAL_FAILURE_WAIT_SECONDS", raising=False)
    monkeypatch.setattr(ai_slider.time, "time", lambda: now["value"])
    monkeypatch.setattr(ai_slider, "_captcha_explicitly_failed", lambda _page: False)

    def fake_solved(_page):
        return now["value"] >= 1.1

    def fake_sleep(seconds, _stop_event=None):
        now["value"] += float(seconds)

    monkeypatch.setattr(ai_slider, "_captcha_solved", fake_solved)
    monkeypatch.setattr(ai_slider, "_sleep", fake_sleep)

    assert ai_slider._wait_for_captcha_success(object(), timeout=2.8) == "页面"
    assert now["value"] > 1.5


def test_solve_slider_captcha_rejects_verify_success_when_captcha_still_visible(tmp_path, monkeypatch):
    from captcha_solvers import ai_slider

    class Root:
        pass

    class Page:
        pass

    records = []
    sleeps = {"count": 0}

    def fake_sleep(_seconds, _stop_event=None):
        sleeps["count"] += 1
        if sleeps["count"] >= 2 and not records:
            records.append({"body": '{"Result":{"VerifyCode":"T001","VerifyResult":true,"certifyId":"cert-ok"}}'})

    monkeypatch.setenv("CAPTCHA_DEBUG_NETWORK", "1")
    monkeypatch.setattr(ai_slider, "_attach_captcha_network_debug", lambda _page, _prefix="": records)
    monkeypatch.setattr(ai_slider, "_find_captcha_root", lambda _page: Root())
    monkeypatch.setattr(ai_slider, "_wait_for_captcha_ready", lambda *args, **kwargs: True)
    monkeypatch.setattr(ai_slider, "_screenshot_locator", lambda _root, path: Path(path).write_bytes(b"fake"))
    monkeypatch.setattr(ai_slider, "_build_drag_plan", lambda *_args: {
        "start_x": 10,
        "start_y": 10,
        "distance": 100,
        "max_distance": 300,
        "source": "图像匹配校准",
    })
    monkeypatch.setattr(ai_slider, "_perform_drag", lambda *_args: 100)
    monkeypatch.setattr(ai_slider, "_captcha_solved", lambda _page: False)
    monkeypatch.setattr(ai_slider, "_sleep", fake_sleep)
    monkeypatch.setattr(ai_slider.time, "sleep", lambda _seconds: None)

    result = ai_slider.solve_slider_captcha(
        Page(),
        ai_slider.CaptchaSolverConfig(enabled=True, attempts=1),
        image_dir=str(tmp_path),
    )

    assert result.ok is False
    assert "滑块仍未通过" in result.message


def test_captcha_solved_rejects_access_verification_body_when_root_missing():
    from captcha_solvers import ai_slider

    class Locator:
        def __init__(self, text):
            self._text = text

        def inner_text(self, timeout=1000):
            return self._text

    class Page:
        def locator(self, selector):
            assert selector == "body"
            return Locator("Sign up to Qwen\nAccess Verification\nPlease complete the operation")

    assert ai_slider._captcha_solved(Page()) is False


def test_captcha_solved_rejects_pending_text_even_if_success_word_exists(monkeypatch):
    from captcha_solvers import ai_slider

    class Root:
        def inner_text(self, timeout=1000):
            return "success\nAccess Verification\nPlease complete the operation"

    class Page:
        pass

    monkeypatch.setattr(ai_slider, "_find_captcha_root", lambda _page: Root())

    assert ai_slider._captcha_solved(Page()) is False


def test_default_captcha_success_wait_covers_delayed_camoufox_success(monkeypatch):
    from captcha_solvers import ai_slider

    monkeypatch.delenv("CAPTCHA_SUCCESS_WAIT_SECONDS", raising=False)

    assert 2.6 <= ai_slider._captcha_success_wait_seconds() <= 3.0


def test_wait_for_captcha_success_uses_default_long_enough_for_slow_page_update(monkeypatch):
    from captcha_solvers import ai_slider

    monkeypatch.delenv("CAPTCHA_SUCCESS_WAIT_SECONDS", raising=False)
    now = {"value": 0.0}

    monkeypatch.setattr(ai_slider.time, "time", lambda: now["value"])

    def fake_sleep(seconds, stop_event=None):
        now["value"] += float(seconds)

    monkeypatch.setattr(ai_slider, "_sleep", fake_sleep)
    monkeypatch.setattr(ai_slider, "_captcha_solved", lambda _page: now["value"] >= 2.0)

    assert ai_slider._wait_for_captcha_success(object(), timeout=ai_slider._captcha_success_wait_seconds()) == "页面"


def test_captcha_success_wait_can_still_be_overridden(monkeypatch):
    from captcha_solvers import ai_slider

    monkeypatch.setenv("CAPTCHA_SUCCESS_WAIT_SECONDS", "9")

    assert ai_slider._captcha_success_wait_seconds() == 9.0


def test_wait_for_aliyun_captcha_ready_accepts_loaded_http_images(monkeypatch):
    from captcha_solvers.ai_slider import _wait_for_captcha_ready

    class Page:
        def evaluate(self, _script):
            return {
                "ready": True,
                "aliyun": True,
                "reason": "已就绪",
            }

    assert _wait_for_captcha_ready(Page(), stop_event=None, timeout=0.5) is True


def test_wait_for_aliyun_captcha_ready_script_allows_http_image_sources(monkeypatch):
    from captcha_solvers import ai_slider

    captured = {"script": ""}

    class Page:
        def evaluate(self, script):
            captured["script"] = script
            raise RuntimeError("stop")

    assert ai_slider._wait_for_captcha_ready(Page(), stop_event=None, timeout=0.5) is True
    assert "imageReady(img)" in captured["script"]
    assert "imageReady(puzzle)" in captured["script"]
    assert "naturalWidth" in captured["script"]
    assert "complete" in captured["script"]


def test_wait_for_aliyun_captcha_ready_script_rejects_gray_loading_placeholder(monkeypatch):
    from captcha_solvers import ai_slider

    captured = {"script": ""}

    class Page:
        def evaluate(self, script):
            captured["script"] = script
            raise RuntimeError("stop")

    assert ai_slider._wait_for_captcha_ready(Page(), stop_event=None, timeout=0.5) is True
    assert "imageHasVisualContent" in captured["script"]
    assert "visualContentReady" in captured["script"]
    assert "图片仍是加载占位" in captured["script"]


def test_image_loader_accepts_http_image_url(monkeypatch):
    from captcha_solvers import ai_slider
    from PIL import Image
    from io import BytesIO

    buf = BytesIO()
    Image.new("RGBA", (12, 8), (1, 2, 3, 255)).save(buf, format="PNG")

    class Response:
        content = buf.getvalue()
        def raise_for_status(self):
            return None

    monkeypatch.setattr(ai_slider.httpx, "get", lambda url, timeout=10, follow_redirects=True: Response())

    loaded = ai_slider._image_from_data_url("https://static-captcha-sgp.aliyuncs.com/example.png")

    assert loaded.size == (12, 8)
    assert loaded.mode == "RGBA"


def test_parse_args_ai_defaults_to_os_fast_quadratic(monkeypatch):
    monkeypatch.delenv("CAPTCHA_DRAG_BACKEND", raising=False)
    monkeypatch.delenv("CAPTCHA_DRAG_STRATEGY", raising=False)

    args = qwenv4.parse_args(["1", "--captcha-solver", "ai"])

    assert args.captcha_drag_backend == "os"
    assert args.captcha_drag_strategy == "fast_quadratic"

    qwenv4.build_captcha_solver_config(args)

    assert os.environ["CAPTCHA_DRAG_BACKEND"] == "os"
    assert os.environ["CAPTCHA_DRAG_STRATEGY"] == "fast_quadratic"



def test_register_qwen_prepares_solvers_concurrently_but_serializes_drag(monkeypatch):
    import threading

    active_prepare = 0
    max_prepare = 0
    active_drag = 0
    max_drag = 0
    active_lock = threading.Lock()
    first_preparing = threading.Event()
    release_first = threading.Event()
    front_calls = []

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
        def __init__(self, name):
            self.name = name

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

        def bring_to_front(self):
            front_calls.append(self.name)

        def evaluate(self, *args, **kwargs):
            return "待激活"

    class FakeTopmost:
        def __enter__(self):
            return True

        def __exit__(self, exc_type, exc, tb):
            return False

    def fake_solver(page, *args, **kwargs):
        nonlocal active_prepare, max_prepare, active_drag, max_drag
        with active_lock:
            active_prepare += 1
            max_prepare = max(max_prepare, active_prepare)
        if page.name == "a":
            first_preparing.set()
            release_first.wait(timeout=0.5)
        else:
            release_first.set()
        with active_lock:
            active_prepare -= 1
        with kwargs["drag_guard"]() as ready:
            assert ready is True
            with active_lock:
                active_drag += 1
                max_drag = max(max_drag, active_drag)
            threading.Event().wait(0.05)
            with active_lock:
                active_drag -= 1
        return CaptchaSolverResult(ok=True, message="ok")

    monkeypatch.setattr(qwenv4.time, "sleep", lambda _seconds: None)
    monkeypatch.setattr(qwenv4, "detect_captcha", lambda _page: True)
    monkeypatch.setattr(qwenv4, "hold_page_topmost", lambda _page, label="": FakeTopmost(), raising=False)
    monkeypatch.setattr(qwenv4, "solve_slider_captcha", fake_solver)

    results = []
    threads = [
        threading.Thread(
            target=lambda page_name=name: results.append(
                qwenv4.register_qwen(
                    DummyPage(page_name),
                    f"User {page_name}",
                    f"{page_name}@example.com",
                    "Password1!",
                    captcha_solver_config=CaptchaSolverConfig(enabled=True, fallback_manual=False),
                )
            )
        )
        for name in ("a", "b")
    ]
    threads[0].start()
    assert first_preparing.wait(timeout=2)
    threads[1].start()
    for thread in threads:
        thread.join(timeout=5)

    assert results == [True, True]
    assert max_prepare == 2
    assert max_drag == 1
    assert sorted(front_calls) == ["a", "b"]


def test_register_qwen_minimizes_slider_window_before_releasing_lock(monkeypatch):
    events = []
    captcha_visible = True

    class FakeSliderLock:
        def __enter__(self):
            events.append("lock_enter")
            return None

        def __exit__(self, exc_type, exc, tb):
            events.append("lock_exit")
            return False

    class FakeTopmost:
        def __enter__(self):
            events.append("topmost_enter")
            return True

        def __exit__(self, exc_type, exc, tb):
            events.append("topmost_exit")
            return False

    class Page:
        def goto(self, *args, **kwargs):
            pass

        def evaluate(self, *args, **kwargs):
            return "待激活"

    monkeypatch.setattr(qwenv4.time, "sleep", lambda _seconds: None)
    monkeypatch.setattr(qwenv4, "submit_registration_form_background", lambda *args, **kwargs: True)
    monkeypatch.setattr(qwenv4, "detect_captcha", lambda _page: captcha_visible)
    monkeypatch.setattr(qwenv4, "acquire_slider_lock", lambda **_kwargs: FakeSliderLock())
    monkeypatch.setattr(qwenv4, "hold_page_topmost", lambda _page, label="": FakeTopmost(), raising=False)
    monkeypatch.setattr(qwenv4, "focus_page_for_slider", lambda _page, label="": events.append("focus") or True)
    monkeypatch.setattr(qwenv4, "wait_for_registration_submission", lambda *args, **kwargs: events.append("wait_submit") or True)
    monkeypatch.setattr(qwenv4, "minimize_page_window", lambda _page, label="": events.append("minimize") or True, raising=False)
    def fake_solver(*args, **kwargs):
        nonlocal captcha_visible
        with kwargs["drag_guard"]() as ready:
            assert ready is True
            events.append("drag")
            captcha_visible = False
        return CaptchaSolverResult(ok=True, message="ok")

    monkeypatch.setattr(qwenv4, "solve_slider_captcha", fake_solver)

    assert qwenv4.register_qwen(
        Page(),
        "Test User",
        "test@example.com",
        "Password1!",
        captcha_solver_config=CaptchaSolverConfig(enabled=True, fallback_manual=False),
        label="[账号 1/2]",
    ) is True

    assert events == ["lock_enter", "topmost_enter", "focus", "drag", "topmost_exit", "minimize", "lock_exit", "wait_submit"]


def test_register_qwen_releases_slider_lock_before_waiting_for_post_captcha_submission(monkeypatch):
    """验证码通过后的业务提交等待不需要前台焦点，应在释放滑块锁后后台执行。"""

    events = []
    captcha_visible = True

    class FakeSliderLock:
        def __enter__(self):
            events.append("lock_enter")
            return None

        def __exit__(self, exc_type, exc, tb):
            events.append("lock_exit")
            return False

    class FakeTopmost:
        def __enter__(self):
            events.append("topmost_enter")
            return True

        def __exit__(self, exc_type, exc, tb):
            events.append("topmost_exit")
            return False

    class Page:
        def goto(self, *args, **kwargs):
            pass

        def evaluate(self, *args, **kwargs):
            return "pending activation"

    monkeypatch.setattr(qwenv4.time, "sleep", lambda _seconds: None)
    monkeypatch.setattr(qwenv4, "submit_registration_form_background", lambda *args, **kwargs: True)
    monkeypatch.setattr(qwenv4, "detect_captcha", lambda _page: captcha_visible)
    monkeypatch.setattr(qwenv4, "acquire_slider_lock", lambda **_kwargs: FakeSliderLock())
    monkeypatch.setattr(qwenv4, "hold_page_topmost", lambda _page, label="": FakeTopmost(), raising=False)
    monkeypatch.setattr(qwenv4, "focus_page_for_slider", lambda _page, label="": events.append("focus") or True)
    def fake_solver(*args, **kwargs):
        nonlocal captcha_visible
        with kwargs["drag_guard"]() as ready:
            assert ready is True
            events.append("drag")
            captcha_visible = False
        return CaptchaSolverResult(ok=True, message="ok")

    monkeypatch.setattr(qwenv4, "solve_slider_captcha", fake_solver)
    monkeypatch.setattr(qwenv4, "minimize_page_window", lambda _page, **_kwargs: events.append("minimize") or True, raising=False)
    monkeypatch.setattr(qwenv4, "wait_for_registration_submission", lambda *args, **kwargs: events.append("wait_submit") or True)

    assert qwenv4.register_qwen(
        Page(),
        "Test User",
        "test@example.com",
        "Password1!",
        captcha_solver_config=CaptchaSolverConfig(enabled=True, fallback_manual=False),
        label="[账号 1/2]",
    ) is True

    assert events.index("wait_submit") > events.index("lock_exit")


def test_register_qwen_minimizes_slider_window_on_solver_failure(monkeypatch):
    events = []

    class FakeSliderLock:
        def __enter__(self):
            events.append("lock_enter")
            return None

        def __exit__(self, exc_type, exc, tb):
            events.append("lock_exit")
            return False

    class FakeTopmost:
        def __enter__(self):
            events.append("topmost_enter")
            return True

        def __exit__(self, exc_type, exc, tb):
            events.append("topmost_exit")
            return False

    class Page:
        def goto(self, *args, **kwargs):
            pass

        def evaluate(self, *args, **kwargs):
            return "请完成以下操作"

    monkeypatch.setattr(qwenv4.time, "sleep", lambda _seconds: None)
    monkeypatch.setattr(qwenv4, "submit_registration_form_background", lambda *args, **kwargs: True)
    monkeypatch.setattr(qwenv4, "detect_captcha", lambda _page: True)
    monkeypatch.setattr(qwenv4, "acquire_slider_lock", lambda **_kwargs: FakeSliderLock())
    monkeypatch.setattr(qwenv4, "hold_page_topmost", lambda _page, label="": FakeTopmost(), raising=False)
    monkeypatch.setattr(qwenv4, "focus_page_for_slider", lambda _page, label="": events.append("focus") or True)
    monkeypatch.setattr(qwenv4, "minimize_page_window", lambda _page, label="": events.append("minimize") or True, raising=False)
    def fake_solver(*args, **kwargs):
        with kwargs["drag_guard"]() as ready:
            assert ready is True
            events.append("drag")
        return CaptchaSolverResult(ok=False, message="fail")

    monkeypatch.setattr(qwenv4, "solve_slider_captcha", fake_solver)

    assert qwenv4.register_qwen(
        Page(),
        "Test User",
        "test@example.com",
        "Password1!",
        captcha_solver_config=CaptchaSolverConfig(enabled=True, fallback_manual=False),
        label="[账号 1/2]",
    ) is False

    assert events == [
        "lock_enter",
        "topmost_enter",
        "focus",
        "drag",
        "topmost_exit",
        "lock_exit",
        "lock_enter",
        "minimize",
        "lock_exit",
    ]


def test_register_qwen_does_not_minimize_when_no_captcha(monkeypatch):
    calls = []

    class Page:
        def goto(self, *args, **kwargs):
            pass

        def evaluate(self, *args, **kwargs):
            return "待激活"

    monkeypatch.setattr(qwenv4.time, "sleep", lambda _seconds: None)
    monkeypatch.setattr(qwenv4, "submit_registration_form_background", lambda *args, **kwargs: True)
    monkeypatch.setattr(qwenv4, "detect_captcha", lambda _page: False)
    monkeypatch.setattr(qwenv4, "minimize_page_window", lambda _page, label="": calls.append(label) or True, raising=False)

    assert qwenv4.register_qwen(
        Page(),
        "Test User",
        "test@example.com",
        "Password1!",
        captcha_solver_config=CaptchaSolverConfig(enabled=True, fallback_manual=False),
        label="[账号 1/2]",
    ) is True

    assert calls == []


def test_submit_registration_form_uses_background_dom_events(monkeypatch):
    scripts = []
    waits = []

    class Page:
        def wait_for_selector(self, selector, timeout=None):
            waits.append((selector, timeout))

        def evaluate(self, script, payload=None):
            scripts.append((script, payload))
            return True

    assert qwenv4.submit_registration_form_background(
        Page(),
        name="Test User",
        email="test@example.com",
        password="Password1!",
    ) is True

    combined_script = "\n".join(script for script, _payload in scripts)
    assert waits == [('input[name="username"]', 2500)]
    assert "HTMLInputElement.prototype" in combined_script
    assert "input[name=\"username\"]" in combined_script
    assert "input[name=\"email\"]" in combined_script
    assert "input[name=\"password\"]" in combined_script
    assert "input[name=\"checkPassword\"]" in combined_script
    assert "new MouseEvent('click'" in combined_script
    assert scripts[-1][1] == {
        "name": "Test User",
        "email": "test@example.com",
        "password": "Password1!",
    }




def test_register_qwen_reopens_register_page_when_initial_form_submit_times_out(monkeypatch):
    """Camoufox 偶发注册页未加载出表单；初始提交超时应重进注册页再提交一次，避免直接丢账号。"""
    import qwenv4

    calls = {"goto": 0, "submit": 0}

    class Page:
        def inner_text(self, selector):
            return "待激活"

    def fake_goto(_page):
        calls["goto"] += 1
        return True

    def fake_submit(*_args, **_kwargs):
        calls["submit"] += 1
        if calls["submit"] == 1:
            raise TimeoutError('Page.wait_for_selector: Timeout 2500ms exceeded')
        return True

    monkeypatch.setattr(qwenv4.time, "sleep", lambda _seconds: None)
    monkeypatch.setattr(qwenv4, "sleep_interruptible", lambda seconds: True)
    monkeypatch.setattr(qwenv4, "goto_register_page_with_retry", fake_goto)
    monkeypatch.setattr(qwenv4, "submit_registration_form_background", fake_submit)
    monkeypatch.setattr(qwenv4, "wait_for_captcha_or_submission_after_submit", lambda *_args, **_kwargs: "submitted")
    monkeypatch.setattr(qwenv4, "detect_captcha", lambda _page: False)

    assert qwenv4.register_qwen(Page(), "Test User", "test@example.com", "Password1!") is True
    assert calls["goto"] == 2
    assert calls["submit"] == 2


def test_register_qwen_uses_background_submit_without_front_clicks(monkeypatch):
    calls = []

    class ForbiddenLocator:
        def scroll_into_view_if_needed(self):
            raise AssertionError("不应滚动前台按钮")

        def click(self):
            raise AssertionError("不应使用前台点击提交")

    class Page:
        def goto(self, *args, **kwargs):
            calls.append("goto")

        def wait_for_selector(self, *args, **kwargs):
            raise AssertionError("register_qwen 应委托后台提交 helper 等待表单")

        def fill(self, *args, **kwargs):
            raise AssertionError("不应使用 page.fill")

        def check(self, *args, **kwargs):
            raise AssertionError("不应使用 page.check")

        def locator(self, *args, **kwargs):
            return ForbiddenLocator()

        def evaluate(self, *args, **kwargs):
            return "待激活"

    monkeypatch.setattr(qwenv4.time, "sleep", lambda _seconds: None)
    monkeypatch.setattr(qwenv4, "detect_captcha", lambda _page: False)
    monkeypatch.setattr(qwenv4, "submit_registration_form_background", lambda *args, **kwargs: calls.append("background_submit") or True)

    assert qwenv4.register_qwen(Page(), "Test User", "test@example.com", "Password1!") is True
    assert calls == ["goto", "background_submit"]


def test_register_qwen_timeout_with_empty_body_is_failure(monkeypatch):
    import qwenv4

    class Page:
        def goto(self, *args, **kwargs):
            raise qwenv4.PlaywrightTimeout("Page.goto: Timeout 60000ms exceeded.")

        def evaluate(self, _script):
            return ""

    assert qwenv4.register_qwen(Page(), "Test User", "test@example.com", "Password1!") is False


def test_focus_page_for_slider_retries_until_visible_and_focused(monkeypatch):
    sleeps = []

    class Page:
        def __init__(self):
            self.front_calls = 0
            self.evaluate_calls = 0

        def bring_to_front(self):
            self.front_calls += 1

        def evaluate(self, script):
            self.evaluate_calls += 1
            if script == "() => window.focus()":
                return None
            return self.evaluate_calls >= 4

    page = Page()
    monkeypatch.setattr(qwenv4.time, "sleep", lambda seconds: sleeps.append(seconds))

    assert qwenv4.focus_page_for_slider(page, label="[测试]", attempts=3, delay=0.01) is True
    assert page.front_calls >= 2
    assert sleeps == [0.01]


def test_focus_page_for_slider_logs_state_when_focus_fails(monkeypatch, capsys):
    class Page:
        url = "https://chat.qwen.ai/auth?mode=register"

        def bring_to_front(self):
            pass

        def evaluate(self, script):
            if script == "() => window.focus()":
                return None
            if script == "() => document.visibilityState === 'visible' && document.hasFocus()":
                return False
            if "document.visibilityState" in script:
                return {"visibilityState": "visible", "hasFocus": False, "url": self.url}
            return None

    monkeypatch.setattr(qwenv4.time, "sleep", lambda _seconds: None)

    assert qwenv4.focus_page_for_slider(Page(), label="[测试]", attempts=1, delay=0.01) is False
    output = capsys.readouterr().out
    assert "visibilityState=visible" in output
    assert "hasFocus=False" in output
    assert "/auth" in output


def test_register_qwen_allows_preparation_but_aborts_drag_when_os_focus_check_fails(monkeypatch):
    solver_called = False
    drag_called = False

    class DummyLocator:
        def scroll_into_view_if_needed(self):
            pass

        def click(self):
            pass

    class Page:
        def goto(self, *args, **kwargs):
            pass

        def evaluate(self, *args, **kwargs):
            return "create account"

    def fake_solver(*args, **kwargs):
        nonlocal drag_called, solver_called
        solver_called = True
        with kwargs["drag_guard"]() as ready:
            if ready:
                drag_called = True
        return CaptchaSolverResult(ok=False, message="焦点失败")

    monkeypatch.setenv("CAPTCHA_DRAG_BACKEND", "os")
    monkeypatch.setattr(qwenv4.time, "sleep", lambda _seconds: None)
    monkeypatch.setattr(qwenv4, "submit_registration_form_background", lambda *args, **kwargs: True)
    monkeypatch.setattr(qwenv4, "detect_captcha", lambda _page: True)
    monkeypatch.setattr(qwenv4, "focus_page_for_slider", lambda *args, **kwargs: False)
    monkeypatch.setattr(qwenv4, "solve_slider_captcha", fake_solver)

    assert qwenv4.register_qwen(
        Page(),
        "Test User",
        "test@example.com",
        "Password1!",
        captcha_solver_config=CaptchaSolverConfig(enabled=True, fallback_manual=False),
    ) is False
    assert solver_called is True
    assert drag_called is False


def test_manual_solver_waits_with_configured_timeout(monkeypatch):
    waits = []

    class Page:
        def goto(self, *args, **kwargs):
            pass

        def evaluate(self, *args, **kwargs):
            return "待激活"

    class FakeTopmost:
        def __enter__(self):
            return True

        def __exit__(self, exc_type, exc, tb):
            return False

    monkeypatch.setattr(qwenv4.time, "sleep", lambda _seconds: None)
    monkeypatch.setattr(qwenv4, "submit_registration_form_background", lambda *args, **kwargs: True)
    monkeypatch.setattr(qwenv4, "detect_captcha", lambda _page: True)
    monkeypatch.setattr(qwenv4, "hold_page_topmost", lambda _page, label="": FakeTopmost(), raising=False)
    monkeypatch.setattr(qwenv4, "focus_page_for_slider", lambda *args, **kwargs: True)
    monkeypatch.setattr(qwenv4, "attach_manual_trace_recorder", lambda *args, **kwargs: [])
    monkeypatch.setattr(qwenv4, "dump_manual_trace_recording", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        qwenv4,
        "wait_for_captcha_completion",
        lambda _page, _email, _password, _name, timeout: waits.append(timeout) or True,
    )
    monkeypatch.setattr(
        qwenv4,
        "solve_slider_captcha",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("人工模式不应调用自动 solver")),
    )

    assert qwenv4.register_qwen(
        Page(),
        "Test User",
        "test@example.com",
        "Password1!",
        captcha_solver_config=CaptchaSolverConfig(enabled=False, mode="manual"),
        captcha_timeout=77,
    ) is True
    assert waits == [77]



def test_captcha_solved_rejects_body_pending_even_when_root_says_success(monkeypatch):
    from captcha_solvers import ai_slider

    class Root:
        def inner_text(self, timeout=1000):
            return "success"

    class Locator:
        def inner_text(self, timeout=1000):
            return "Sign up to Qwen\nAccess Verification\nPlease complete the operation"

    class Page:
        def locator(self, selector):
            assert selector == "body"
            return Locator()

    monkeypatch.setattr(ai_slider, "_find_captcha_root", lambda _page: Root())

    assert ai_slider._captcha_solved(Page()) is False


def test_install_aliyun_verify_success_route_matches_verify_action_in_url_and_logs(capsys):
    from captcha_solvers.ai_slider import install_aliyun_verify_success_route

    class Page:
        def __init__(self):
            self.handler = None

        def route(self, pattern, handler):
            self.pattern = pattern
            self.handler = handler

    class Request:
        url = "https://captcha-open.aliyuncs.com/?Action=VerifyCaptchaV2&CertifyId=cert-url"
        post_data = ""

    class Route:
        request = Request()

        def __init__(self):
            self.fulfilled = None
            self.continued = False

        def fulfill(self, **kwargs):
            self.fulfilled = kwargs

        def continue_(self):
            self.continued = True

    page = Page()
    install_aliyun_verify_success_route(page)
    route = Route()
    page.handler(route)

    assert route.continued is False
    assert route.fulfilled is not None
    body = json.loads(route.fulfilled["body"])
    assert body["Result"]["VerifyResult"] is True
    assert body["Result"]["VerifyCode"] == "T001"
    assert body["Result"]["certifyId"] == "cert-url"
    output = capsys.readouterr().out
    assert "Verify 响应替换已命中" in output



def test_wait_for_captcha_success_requires_stable_success(monkeypatch):
    from captcha_solvers import ai_slider

    states = iter([True, False, False, False])
    monkeypatch.setattr(ai_slider, "_captcha_solved", lambda _page: next(states, False))
    monkeypatch.setattr(ai_slider, "_sleep", lambda _seconds, _stop_event=None: None)

    assert ai_slider._wait_for_captcha_success(object(), timeout=0.01) is None







def test_register_qwen_polls_captcha_without_unconditional_five_second_sleep(monkeypatch):
    import qwenv4
    from captcha_solvers.ai_slider import CaptchaSolverConfig, CaptchaSolverResult

    sleeps = []
    events = []

    class Page:
        def evaluate(self, _script):
            return "pending activation"

    class FakeSliderLock:
        def __enter__(self):
            events.append("lock_enter")
            return None

        def __exit__(self, exc_type, exc, tb):
            events.append("lock_exit")
            return False

    class FakeTopmost:
        def __enter__(self):
            return True

        def __exit__(self, exc_type, exc, tb):
            return False

    monkeypatch.setattr(qwenv4, "goto_register_page_with_retry", lambda _page: True)
    monkeypatch.setattr(qwenv4, "submit_registration_form_background", lambda *args, **kwargs: True)
    monkeypatch.setattr(qwenv4.time, "sleep", lambda seconds: sleeps.append(float(seconds)))
    monkeypatch.setattr(qwenv4, "sleep_interruptible", lambda seconds, stop_event=None: sleeps.append(float(seconds)))
    monkeypatch.setattr(qwenv4, "detect_captcha", lambda _page: True)
    monkeypatch.setattr(qwenv4, "acquire_slider_lock", lambda **kwargs: FakeSliderLock())
    monkeypatch.setattr(qwenv4, "hold_page_topmost", lambda _page, label="": FakeTopmost(), raising=False)
    monkeypatch.setattr(qwenv4, "focus_page_for_slider", lambda *_args, **_kwargs: True)
    monkeypatch.setattr(qwenv4, "solve_slider_captcha", lambda *args, **kwargs: CaptchaSolverResult(ok=True, message="ok"))
    monkeypatch.setattr(qwenv4, "wait_for_registration_submission", lambda *args, **kwargs: True)
    monkeypatch.setattr(qwenv4, "minimize_page_window", lambda *args, **kwargs: True, raising=False)

    assert qwenv4.register_qwen(
        Page(),
        "Test User",
        "test@example.com",
        "Password1!",
        captcha_solver_config=CaptchaSolverConfig(enabled=True, mode="ddddocr", attempts=3, fallback_manual=False),
    ) is True
    assert 5.0 not in sleeps



def test_register_qwen_uses_short_post_captcha_wait_for_ddddocr(monkeypatch):
    import qwenv4
    from captcha_solvers.ai_slider import CaptchaSolverConfig, CaptchaSolverResult

    seen = {}

    class Page:
        def evaluate(self, _script):
            return "pending activation verification email"

    class FakeSliderLock:
        def __enter__(self):
            return None

        def __exit__(self, exc_type, exc, tb):
            return False

    class FakeTopmost:
        def __enter__(self):
            return True

        def __exit__(self, exc_type, exc, tb):
            return False

    monkeypatch.setattr(qwenv4, "goto_register_page_with_retry", lambda _page: True)
    monkeypatch.setattr(qwenv4, "submit_registration_form_background", lambda *args, **kwargs: True)
    monkeypatch.setattr(qwenv4, "wait_for_captcha_or_submission_after_submit", lambda *_args, **_kwargs: "captcha")
    monkeypatch.setattr(qwenv4, "detect_captcha", lambda _page: True)
    monkeypatch.setattr(qwenv4, "acquire_slider_lock", lambda **kwargs: FakeSliderLock())
    monkeypatch.setattr(qwenv4, "hold_page_topmost", lambda _page, label="": FakeTopmost(), raising=False)
    monkeypatch.setattr(qwenv4, "focus_page_for_slider", lambda *_args, **_kwargs: True)
    monkeypatch.setattr(qwenv4, "solve_slider_captcha", lambda *args, **kwargs: CaptchaSolverResult(ok=True, message="ok"))
    monkeypatch.setattr(qwenv4, "minimize_page_window", lambda *args, **kwargs: True, raising=False)

    def fake_wait(_page, timeout=3.0, interval=0.25):
        seen["timeout"] = timeout
        return True

    monkeypatch.setattr(qwenv4, "wait_for_registration_submission", fake_wait)

    assert qwenv4.register_qwen(
        Page(),
        "Test User",
        "test@example.com",
        "Password1!",
        captcha_solver_config=CaptchaSolverConfig(enabled=True, mode="ddddocr", attempts=3, fallback_manual=False),
    ) is True
    assert seen["timeout"] <= 2.0


def test_register_qwen_resubmits_after_slider_success_when_form_returns_request_aborted(monkeypatch):
    import qwenv4
    from captcha_solvers.ai_slider import CaptchaSolverConfig, CaptchaSolverResult

    calls = {"submit": 0, "body": 0, "post_wait": 0, "solver": 0}

    class Page:
        def evaluate(self, _script, *args):
            calls["body"] += 1
            if calls["submit"] < 2:
                return "Sign up to Qwen\nCreate Account\nRequest aborted"
            return "pending activation verification email"

    class FakeSliderLock:
        def __enter__(self):
            return None

        def __exit__(self, exc_type, exc, tb):
            return False

    class FakeTopmost:
        def __enter__(self):
            return True

        def __exit__(self, exc_type, exc, tb):
            return False

    def fake_submit(*args, **kwargs):
        calls["submit"] += 1
        return True

    def fake_post_wait(*args, **kwargs):
        calls["post_wait"] += 1
        return False

    def fake_solver(*args, **kwargs):
        calls["solver"] += 1
        return CaptchaSolverResult(ok=True, message="ok")

    monkeypatch.setattr(qwenv4, "goto_register_page_with_retry", lambda _page: True)
    monkeypatch.setattr(qwenv4, "submit_registration_form_background", fake_submit)
    monkeypatch.setattr(qwenv4, "wait_for_captcha_or_submission_after_submit", lambda *_args, **_kwargs: "captcha" if calls["submit"] == 1 else "submitted")
    monkeypatch.setattr(qwenv4, "wait_for_registration_submission", fake_post_wait)
    monkeypatch.setattr(qwenv4, "detect_captcha", lambda _page: calls["submit"] == 1)
    monkeypatch.setattr(qwenv4, "acquire_slider_lock", lambda **kwargs: FakeSliderLock())
    monkeypatch.setattr(qwenv4, "hold_page_topmost", lambda _page, label="": FakeTopmost(), raising=False)
    monkeypatch.setattr(qwenv4, "focus_page_for_slider", lambda *_args, **_kwargs: True)
    monkeypatch.setattr(qwenv4, "solve_slider_captcha", fake_solver)
    monkeypatch.setattr(qwenv4, "minimize_page_window", lambda *args, **kwargs: True, raising=False)

    assert qwenv4.register_qwen(
        Page(),
        "Test User",
        "test@example.com",
        "Password1!",
        captcha_solver_config=CaptchaSolverConfig(enabled=True, mode="ddddocr", attempts=3, fallback_manual=False),
    ) is True
    assert calls["solver"] == 1
    assert calls["submit"] == 2






def test_submit_registration_form_uses_short_initial_field_wait(monkeypatch):
    import qwenv4

    seen = {"timeout": None, "evaluate": False}

    class Page:
        def wait_for_selector(self, selector, timeout=0):
            assert selector == 'input[name="username"]'
            seen["timeout"] = timeout

        def evaluate(self, script, payload):
            seen["evaluate"] = True
            assert payload["email"] == "test@example.com"
            return True

    assert qwenv4.submit_registration_form_background(
        Page(),
        name="Test User",
        email="test@example.com",
        password="Password1!",
    ) is True
    assert seen["evaluate"] is True
    assert seen["timeout"] <= 3000


def test_register_qwen_skips_slow_resubmit_when_form_field_disappeared(monkeypatch):
    import qwenv4
    from captcha_solvers.ai_slider import CaptchaSolverConfig, CaptchaSolverResult

    calls = {"submit": 0, "body": 0, "post_wait": 0, "solver": 0, "form_visible": 0}

    class Page:
        def evaluate(self, _script, *args):
            calls["body"] += 1
            # 首次滑块后读到旧表单文案/Request aborted，但实际表单字段已经消失。
            if calls["body"] <= 1:
                return "Sign up to Qwen\nCreate Account\nRequest aborted"
            return "pending activation verification email"

        def locator(self, selector):
            assert selector == 'input[name="username"]'
            return self

        @property
        def first(self):
            return self

        def is_visible(self, timeout=0):
            calls["form_visible"] += 1
            assert timeout <= 500
            return False

    class FakeSliderLock:
        def __enter__(self):
            return None

        def __exit__(self, exc_type, exc, tb):
            return False

    class FakeTopmost:
        hwnd = 123

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def __bool__(self):
            return True

    def fake_submit(*args, **kwargs):
        calls["submit"] += 1
        return True

    def fake_post_wait(*args, **kwargs):
        calls["post_wait"] += 1
        return False

    def fake_solver(*args, **kwargs):
        calls["solver"] += 1
        return CaptchaSolverResult(ok=True, message="ok")

    monkeypatch.setattr(qwenv4, "goto_register_page_with_retry", lambda _page: True)
    monkeypatch.setattr(qwenv4, "submit_registration_form_background", fake_submit)
    monkeypatch.setattr(qwenv4, "wait_for_captcha_or_submission_after_submit", lambda *_args, **_kwargs: "captcha" if calls["submit"] == 1 else "submitted")
    monkeypatch.setattr(qwenv4, "wait_for_registration_submission", fake_post_wait)
    monkeypatch.setattr(qwenv4, "detect_captcha", lambda _page: calls["submit"] == 1)
    monkeypatch.setattr(qwenv4, "acquire_slider_lock", lambda **kwargs: FakeSliderLock())
    monkeypatch.setattr(qwenv4, "hold_page_topmost", lambda _page, label="": FakeTopmost(), raising=False)
    monkeypatch.setattr(qwenv4, "focus_page_for_slider", lambda *_args, **_kwargs: True)
    monkeypatch.setattr(qwenv4, "solve_slider_captcha", fake_solver)
    monkeypatch.setattr(qwenv4, "minimize_page_window", lambda *args, **kwargs: True, raising=False)

    assert qwenv4.register_qwen(
        Page(),
        "Test User",
        "test@example.com",
        "Password1!",
        captcha_solver_config=CaptchaSolverConfig(enabled=True, mode="ddddocr", attempts=3, fallback_manual=False),
    ) is True
    assert calls["solver"] == 1
    assert calls["form_visible"] == 1
    assert calls["submit"] == 1






def test_register_qwen_returns_after_fast_post_captcha_submission(monkeypatch):
    """滑块通过后若短轮询已确认提交成功，不应继续读旧 body 或触发慢速重提交。"""
    import qwenv4
    from captcha_solvers.ai_slider import CaptchaSolverConfig, CaptchaSolverResult

    calls = {"submit": 0, "post_wait": 0, "body": 0}

    class Page:
        def evaluate(self, *_args, **_kwargs):
            calls["body"] += 1
            return "Sign up to Qwen Create Account"

    class FakeSliderLock:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

    class FakeTopmost:
        hwnd = 123

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def __bool__(self):
            return True

    def fake_submit(*_args, **_kwargs):
        calls["submit"] += 1
        return True

    def fake_wait_for_registration_submission(*_args, **_kwargs):
        calls["post_wait"] += 1
        return True

    monkeypatch.setattr(qwenv4, "goto_register_page_with_retry", lambda _page: True)
    monkeypatch.setattr(qwenv4, "submit_registration_form_background", fake_submit)
    monkeypatch.setattr(qwenv4, "wait_for_captcha_or_submission_after_submit", lambda *_args, **_kwargs: "captcha")
    monkeypatch.setattr(qwenv4, "wait_for_registration_submission", fake_wait_for_registration_submission)
    monkeypatch.setattr(qwenv4, "detect_captcha", lambda _page: calls["submit"] == 1)
    monkeypatch.setattr(qwenv4, "acquire_slider_lock", lambda **kwargs: FakeSliderLock())
    monkeypatch.setattr(qwenv4, "hold_page_topmost", lambda _page, label="": FakeTopmost(), raising=False)
    monkeypatch.setattr(qwenv4, "focus_page_for_slider", lambda *_args, **_kwargs: True)
    monkeypatch.setattr(qwenv4, "solve_slider_captcha", lambda *_args, **_kwargs: CaptchaSolverResult(ok=True, message="ok"))
    monkeypatch.setattr(qwenv4, "minimize_page_window", lambda *_args, **_kwargs: True, raising=False)

    assert qwenv4.register_qwen(
        Page(),
        "Test User",
        "test@example.com",
        "Password1!",
        captcha_solver_config=CaptchaSolverConfig(enabled=True, mode="ddddocr", attempts=3, fallback_manual=False),
    ) is True
    assert calls["post_wait"] == 1
    assert calls["submit"] == 1
    assert calls["body"] == 0


def test_register_qwen_treats_post_captcha_resubmit_missing_form_as_submitted(monkeypatch):
    """真实 Camoufox 日志：滑块已过后旧 body 仍含注册文案，但表单已消失；重提交超时不应让账号失败。"""
    import qwenv4
    from captcha_solvers.ai_slider import CaptchaSolverConfig, CaptchaSolverResult

    calls = {"submit": 0, "body": 0, "post_wait": 0}

    class Page:
        pass

    class FakeSliderLock:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

    class FakeTopmost:
        ok = True
        hwnd = 123

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

    def fake_submit(*_args, **_kwargs):
        calls["submit"] += 1
        if calls["submit"] >= 2:
            raise TimeoutError('Page.wait_for_selector: Timeout 2500ms exceeded')
        return True

    def fake_read_body(*_args, **_kwargs):
        calls["body"] += 1
        if calls["body"] == 1:
            return "create account"
        return "pending activation"

    def fake_wait_for_registration_submission(*_args, **_kwargs):
        calls["post_wait"] += 1
        return False

    monkeypatch.setattr(qwenv4.time, "sleep", lambda _seconds: None)
    monkeypatch.setattr(qwenv4, "sleep_interruptible", lambda *_args, **_kwargs: True)
    monkeypatch.setattr(qwenv4, "wait_for_captcha_completion", lambda *_args, **_kwargs: True)
    monkeypatch.setattr(qwenv4, "goto_register_page_with_retry", lambda _page: True)
    monkeypatch.setattr(qwenv4, "submit_registration_form_background", fake_submit)
    monkeypatch.setattr(qwenv4, "wait_for_captcha_or_submission_after_submit", lambda *_args, **_kwargs: "captcha")
    monkeypatch.setattr(qwenv4, "wait_for_registration_submission", fake_wait_for_registration_submission)
    monkeypatch.setattr(qwenv4, "detect_captcha", lambda _page: calls["submit"] == 1)
    monkeypatch.setattr(qwenv4, "acquire_slider_lock", lambda **kwargs: FakeSliderLock())
    monkeypatch.setattr(qwenv4, "hold_page_topmost", lambda _page, label="": FakeTopmost(), raising=False)
    monkeypatch.setattr(qwenv4, "focus_page_for_slider", lambda *_args, **_kwargs: True)
    monkeypatch.setattr(qwenv4, "solve_slider_captcha", lambda *_args, **_kwargs: CaptchaSolverResult(ok=True, message="ok"))
    monkeypatch.setattr(qwenv4, "minimize_page_window", lambda *_args, **_kwargs: True, raising=False)
    monkeypatch.setattr(qwenv4, "read_page_body_text", fake_read_body)
    monkeypatch.setattr(qwenv4, "registration_form_input_visible", lambda *_args, **_kwargs: True)

    assert qwenv4.register_qwen(
        Page(),
        "Test User",
        "test@example.com",
        "Password1!",
        captcha_solver_config=CaptchaSolverConfig(mode="ddddocr", attempts=3, fallback_manual=False),
    ) is True
    assert calls["submit"] == 2
    assert calls["post_wait"] >= 1


def test_register_qwen_retries_background_submit_when_form_still_visible(monkeypatch):
    import qwenv4

    calls = {"submit": 0, "detect": 0}
    sleeps = []

    class Page:
        def evaluate(self, _script):
            if calls["submit"] < 2:
                return "Sign up to Qwen Create Account"
            return "pending activation"

    def fake_submit(*args, **kwargs):
        calls["submit"] += 1
        return True

    monkeypatch.setattr(qwenv4, "goto_register_page_with_retry", lambda _page: True)
    monkeypatch.setattr(qwenv4, "submit_registration_form_background", fake_submit)
    monkeypatch.setattr(qwenv4, "detect_captcha", lambda _page: False)
    monkeypatch.setattr(qwenv4, "sleep_interruptible", lambda seconds, stop_event=None: sleeps.append(float(seconds)))
    monkeypatch.setattr(qwenv4.time, "sleep", lambda seconds: sleeps.append(float(seconds)))

    assert qwenv4.register_qwen(Page(), "Test User", "test@example.com", "Password1!") is True
    assert calls["submit"] == 2
    assert 5.0 not in sleeps






def test_release_hold_range_default_is_short_for_fast_slider(monkeypatch):
    from captcha_solvers import ai_slider

    monkeypatch.delenv("CAPTCHA_RELEASE_HOLD_MIN_SECONDS", raising=False)
    monkeypatch.delenv("CAPTCHA_RELEASE_HOLD_MAX_SECONDS", raising=False)

    low, high = ai_slider._release_hold_seconds_range()

    assert 0.23 <= low <= 0.32
    assert 0.38 <= high <= 0.48



def test_success_poll_timing_defaults_are_fast_but_stable(monkeypatch):
    from captcha_solvers import ai_slider

    monkeypatch.delenv("CAPTCHA_SUCCESS_STABLE_SECONDS", raising=False)
    monkeypatch.delenv("CAPTCHA_SUCCESS_POLL_SECONDS", raising=False)

    assert 0.55 <= ai_slider._captcha_success_stable_seconds() <= 0.7
    assert 0.20 <= ai_slider._captcha_success_poll_seconds() <= 0.30


def test_success_poll_timing_can_be_overridden(monkeypatch):
    from captcha_solvers import ai_slider

    monkeypatch.setenv("CAPTCHA_SUCCESS_STABLE_SECONDS", "1.1")
    monkeypatch.setenv("CAPTCHA_SUCCESS_POLL_SECONDS", "0.4")

    assert ai_slider._captcha_success_stable_seconds() == 1.1
    assert ai_slider._captcha_success_poll_seconds() == 0.4


def test_release_hold_range_can_be_overridden(monkeypatch):
    from captcha_solvers import ai_slider

    monkeypatch.setenv("CAPTCHA_RELEASE_HOLD_MIN_SECONDS", "0.6")
    monkeypatch.setenv("CAPTCHA_RELEASE_HOLD_MAX_SECONDS", "0.9")

    assert ai_slider._release_hold_seconds_range() == (0.6, 0.9)



def test_solve_slider_captcha_treats_detached_screenshot_after_disappear_as_success(monkeypatch, tmp_path):
    from captcha_solvers import ai_slider
    from captcha_solvers.ai_slider import CaptchaSolverConfig, solve_slider_captcha

    class Root:
        pass

    states = {"find_calls": 0}

    def fake_find_root(_page):
        states["find_calls"] += 1
        if states["find_calls"] == 1:
            return Root()
        return None

    monkeypatch.setattr(ai_slider, "_find_captcha_root", fake_find_root)
    monkeypatch.setattr(ai_slider, "_wait_for_captcha_ready", lambda *args, **kwargs: True)
    monkeypatch.setattr(ai_slider, "_screenshot_locator", lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("Locator.screenshot: Element is not attached to the DOM")))
    monkeypatch.setattr(ai_slider, "_page_body_has_pending_captcha_text", lambda _page: False)
    monkeypatch.setattr(ai_slider.time, "sleep", lambda _seconds: None)
    monkeypatch.setattr(ai_slider, "_sleep", lambda *_args, **_kwargs: None)

    result = solve_slider_captcha(
        object(),
        CaptchaSolverConfig(enabled=True, mode="ddddocr", attempts=3, fallback_manual=False),
        image_dir=str(tmp_path),
    )

    assert result.ok is True
    assert "验证码已消失" in result.message


def test_solve_slider_captcha_rechecks_disappeared_captcha_after_success_wait_timeout(monkeypatch, tmp_path):
    from captcha_solvers import ai_slider
    from captcha_solvers.ai_slider import CaptchaSolverConfig, solve_slider_captcha

    class Root:
        pass

    solved_checks = {"count": 0}
    metadata = []

    monkeypatch.setattr(ai_slider, "_find_captcha_root", lambda _page: Root())
    monkeypatch.setattr(ai_slider, "_wait_for_captcha_ready", lambda *args, **kwargs: True)
    monkeypatch.setattr(ai_slider, "_screenshot_locator", lambda _root, path: Path(path).write_bytes(b"fake"))
    monkeypatch.setattr(ai_slider, "_build_drag_plan", lambda *_args: {
        "start_x": 10.0,
        "start_y": 20.0,
        "distance": 150.0,
        "max_distance": 300.0,
        "source": "ddddocr",
        "alternatives": [150.0],
    })
    monkeypatch.setattr(ai_slider, "_perform_drag_with_focus_retry", lambda *_args, **_kwargs: 150.0)
    monkeypatch.setattr(ai_slider, "_wait_for_captcha_success", lambda *args, **kwargs: None)

    def fake_solved(_page):
        solved_checks["count"] += 1
        return True

    monkeypatch.setattr(ai_slider, "_captcha_solved", fake_solved)
    monkeypatch.setattr(ai_slider, "_write_captcha_attempt_metadata", lambda **kwargs: metadata.append(kwargs))

    result = solve_slider_captcha(
        object(),
        CaptchaSolverConfig(enabled=True, mode="ddddocr", attempts=3, api_key="", fallback_manual=False),
        image_dir=str(tmp_path),
    )

    assert result.ok is True
    assert "验证码已消失" in result.message
    assert metadata[-1]["outcome"] == "success"
    assert solved_checks["count"] >= 1


def test_wait_for_captcha_success_returns_quickly_on_explicit_failure(monkeypatch):
    from captcha_solvers import ai_slider

    class DummyRoot:
        def inner_text(self, timeout=1000):
            return "验证失败，请重试"

    class DummyPage:
        pass

    sleep_calls = []
    monkeypatch.setattr(ai_slider, "_find_captcha_root", lambda _page: DummyRoot())
    monkeypatch.setattr(ai_slider, "_page_body_has_pending_captcha_text", lambda _page: False)
    monkeypatch.setattr(ai_slider, "_sleep", lambda seconds, stop_event: sleep_calls.append(seconds))

    result = ai_slider._wait_for_captcha_success(DummyPage(), timeout=3.5)

    assert result is None
    assert sleep_calls == []


def test_default_captcha_success_wait_is_shorter_for_fast_retry(monkeypatch):
    from captcha_solvers import ai_slider

    monkeypatch.delenv("CAPTCHA_SUCCESS_WAIT_SECONDS", raising=False)

    assert 2.6 <= ai_slider._captcha_success_wait_seconds() <= 3.0


def test_write_captcha_attempt_metadata_creates_json_sidecar(tmp_path):
    from captcha_solvers import ai_slider

    screenshot = tmp_path / "captcha_ai_sample.png"
    screenshot.write_bytes(b"fakepng")
    plan = {
        "source": "ddddocr",
        "distance": 190,
        "selected_target_x": 195.0,
        "raw_selected_target_x": 200.0,
        "target_right_bias": -5.0,
        "selected_target_index": 1,
        "ddddocr_target_x": 200.0,
        "white_gap_target_x": 198.0,
        "match_target_x": 202.0,
        "hold_state": {"puzzleLeft": 193.0, "sliderLeft": 193.0},
    }

    path = ai_slider._write_captcha_attempt_metadata(
        screenshot_path=str(screenshot),
        label="[账号 1/1]",
        attempt=2,
        mode="ddddocr",
        plan=plan,
        outcome="failure",
        message="滑块仍未通过",
        success_source=None,
    )

    assert path == str(screenshot.with_suffix(".json"))
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    assert data["label"] == "[账号 1/1]"
    assert data["attempt"] == 2
    assert data["mode"] == "ddddocr"
    assert data["outcome"] == "failure"
    assert data["message"] == "滑块仍未通过"
    assert data["plan"]["selected_target_x"] == 195.0
    assert data["alignment"]["delta"] == 2.0






