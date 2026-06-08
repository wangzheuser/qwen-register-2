def test_ensure_page_foreground_uses_win32_title_marker(monkeypatch):
    from captcha_solvers import window_focus

    class Page:
        def __init__(self):
            self.front_calls = 0
            self.focus_calls = 0
            self.markers = []

        def bring_to_front(self):
            self.front_calls += 1

        def evaluate(self, script, arg=None):
            if script == "() => window.focus()":
                self.focus_calls += 1
                return None
            if "window.__qwenSliderFocusMarker" in script:
                self.markers.append(str(arg))
                return True
            if "document.visibilityState" in script:
                return {"visibilityState": "visible", "hasFocus": True, "url": "https://chat.qwen.ai/auth"}
            return True

    calls = []
    monkeypatch.setattr(window_focus.os, "name", "nt", raising=False)
    monkeypatch.setattr(window_focus, "_find_hwnd_by_title_marker", lambda marker: calls.append(("find", marker)) or 1234)
    monkeypatch.setattr(window_focus, "_activate_hwnd", lambda hwnd: calls.append(("activate", hwnd)) or True)
    monkeypatch.setattr(window_focus, "_foreground_matches", lambda hwnd: calls.append(("match", hwnd)) or True)
    monkeypatch.setattr(window_focus.time, "sleep", lambda _seconds: None)

    page = Page()

    assert window_focus.ensure_page_foreground(page, label="[测试]", attempts=1) is True
    assert page.front_calls >= 1
    assert page.focus_calls >= 1
    assert page.markers and page.markers[0].startswith("qwen-slider-")
    assert calls == [
        ("find", page.markers[0]),
        ("activate", 1234),
        ("match", 1234),
    ]


def test_foreground_matches_accepts_child_window_root(monkeypatch):
    from captcha_solvers import window_focus

    monkeypatch.setattr(window_focus.os, "name", "nt", raising=False)
    monkeypatch.setattr(window_focus, "_get_foreground_hwnd", lambda: 200)
    monkeypatch.setattr(window_focus, "_get_root_hwnd", lambda hwnd: 100 if hwnd in {100, 200} else hwnd)

    assert window_focus._foreground_matches(100) is True
