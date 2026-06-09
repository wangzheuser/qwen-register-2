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
    monkeypatch.setattr(window_focus, "_activate_hwnd", lambda hwnd, **_kwargs: calls.append(("activate", hwnd)) or True)
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


def test_minimize_page_window_uses_marker_hwnd_without_bringing_to_front(monkeypatch):
    from captcha_solvers import window_focus

    class User32:
        def __init__(self):
            self.show_calls = []
            self.pos_calls = []

        def ShowWindow(self, hwnd, command):
            self.show_calls.append((hwnd, command))
            return 1

        def SetWindowPos(self, hwnd, insert_after, x, y, cx, cy, flags):
            self.pos_calls.append((hwnd, insert_after, flags))
            return 1

    class Page:
        def __init__(self):
            self.front_calls = 0
            self.markers = []
            self.restored = 0

        def bring_to_front(self):
            self.front_calls += 1

        def evaluate(self, script, arg=None):
            if "window.__qwenSliderFocusMarker" in script:
                self.markers.append(str(arg))
                return True
            if arg is None and "window.__qwenSliderOriginalTitle" in script:
                self.restored += 1
                return None
            return True

    user32 = User32()
    marker_calls = []
    monkeypatch.setattr(window_focus.os, "name", "nt", raising=False)
    monkeypatch.setattr(window_focus, "_get_user32", lambda: user32)
    monkeypatch.setattr(window_focus, "_find_hwnd_for_page", lambda _page, marker, **_kwargs: marker_calls.append(marker) or 4321)

    page = Page()

    assert window_focus.minimize_page_window(page, label="[测试]") is True
    assert page.front_calls == 0
    assert page.markers and page.markers[0].startswith("qwen-minimize-")
    assert marker_calls == [page.markers[0]]
    assert user32.pos_calls == [(4321, window_focus.HWND_NOTOPMOST, window_focus.SWP_NOMOVE | window_focus.SWP_NOSIZE)]
    assert user32.show_calls == [(4321, window_focus.SW_MINIMIZE)]
    assert page.restored == 1


def test_minimize_page_window_returns_false_when_hwnd_missing(monkeypatch):
    from captcha_solvers import window_focus

    class Page:
        def evaluate(self, script, arg=None):
            return True

    monkeypatch.setattr(window_focus.os, "name", "nt", raising=False)
    monkeypatch.setattr(window_focus, "_find_hwnd_for_page", lambda _page, _marker, **_kwargs: None)

    assert window_focus.minimize_page_window(Page(), label="[测试]") is False


def test_hold_page_topmost_keeps_topmost_until_context_exit(monkeypatch):
    from captcha_solvers import window_focus

    class User32:
        def __init__(self):
            self.show_calls = []
            self.pos_calls = []

        def ShowWindow(self, hwnd, command):
            self.show_calls.append((hwnd, command))
            return 1

        def SetWindowPos(self, hwnd, insert_after, x, y, cx, cy, flags):
            self.pos_calls.append((hwnd, insert_after, flags))
            return 1

        def BringWindowToTop(self, hwnd):
            return 1

        def GetForegroundWindow(self):
            return 4321

        def GetWindowThreadProcessId(self, hwnd, pid):
            return 99

        def AttachThreadInput(self, current_thread, other_thread, attach):
            return 1

        def SetActiveWindow(self, hwnd):
            return 1

        def SetFocus(self, hwnd):
            return 1

        def SetForegroundWindow(self, hwnd):
            return 1

    class Page:
        def __init__(self):
            self.front_calls = 0
            self.markers = []
            self.restored = 0

        def bring_to_front(self):
            self.front_calls += 1

        def evaluate(self, script, arg=None):
            if arg is None and "window.__qwenSliderOriginalTitle" in script:
                self.restored += 1
                return None
            if "window.__qwenSliderFocusMarker" in script:
                self.markers.append(str(arg))
                return True
            if script == "() => window.focus()":
                return None
            return True

    user32 = User32()
    monkeypatch.setattr(window_focus.os, "name", "nt", raising=False)
    monkeypatch.setattr(window_focus, "_get_user32", lambda: user32)
    monkeypatch.setattr(window_focus, "_find_hwnd_for_page", lambda _page, _marker, **_kwargs: 4321)
    monkeypatch.setattr(window_focus, "_foreground_matches", lambda hwnd: hwnd == 4321)

    page = Page()

    with window_focus.hold_page_topmost(page, label="[测试]") as ok:
        assert ok is True
        assert (4321, window_focus.HWND_TOPMOST, window_focus.SWP_NOMOVE | window_focus.SWP_NOSIZE | window_focus.SWP_SHOWWINDOW) in user32.pos_calls
        assert all(call[1] != window_focus.HWND_NOTOPMOST for call in user32.pos_calls)

    assert user32.pos_calls[-1] == (4321, window_focus.HWND_NOTOPMOST, window_focus.SWP_NOMOVE | window_focus.SWP_NOSIZE)
    assert page.restored == 1


def test_configure_user32_api_sets_set_window_pos_hwnd_prototype():
    from captcha_solvers import window_focus

    class Func:
        pass

    class User32:
        def __init__(self):
            self.SetWindowPos = Func()

    user32 = User32()

    window_focus._configure_user32_api(user32)

    assert user32.SetWindowPos.argtypes[0] is window_focus.wintypes.HWND
    assert user32.SetWindowPos.argtypes[1] is window_focus.wintypes.HWND
    assert user32.SetWindowPos.restype is window_focus.wintypes.BOOL


def test_find_hwnd_by_window_metrics_rejects_ambiguous_candidates(monkeypatch):
    from captcha_solvers import window_focus

    class User32:
        def IsWindowVisible(self, hwnd):
            return 1

        def GetWindowRect(self, hwnd, rect_ptr):
            rect = rect_ptr._obj
            rect.left = 0
            rect.top = 0
            rect.right = 1280
            rect.bottom = 800
            return 1

        def EnumWindows(self, callback, lparam):
            callback(1001, lparam)
            callback(1002, lparam)
            return 1

    monkeypatch.setattr(window_focus.os, "name", "nt", raising=False)
    monkeypatch.setattr(window_focus, "_get_user32", lambda: User32())

    assert window_focus._find_hwnd_by_window_metrics({
        "screenX": 0,
        "screenY": 0,
        "outerWidth": 1280,
        "outerHeight": 800,
    }) is None


def test_find_hwnd_by_window_metrics_accepts_unique_candidate(monkeypatch):
    from captcha_solvers import window_focus

    class User32:
        def IsWindowVisible(self, hwnd):
            return 1

        def GetWindowRect(self, hwnd, rect_ptr):
            rect = rect_ptr._obj
            if hwnd == 1001:
                rect.left = 0
                rect.top = 0
                rect.right = 1280
                rect.bottom = 800
            else:
                rect.left = 240
                rect.top = 160
                rect.right = 1520
                rect.bottom = 960
            return 1

        def EnumWindows(self, callback, lparam):
            callback(1001, lparam)
            callback(1002, lparam)
            return 1

    monkeypatch.setattr(window_focus.os, "name", "nt", raising=False)
    monkeypatch.setattr(window_focus, "_get_user32", lambda: User32())

    assert window_focus._find_hwnd_by_window_metrics({
        "screenX": 0,
        "screenY": 0,
        "outerWidth": 1280,
        "outerHeight": 800,
    }) == 1001
