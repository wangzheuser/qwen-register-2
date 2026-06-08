"""Windows 前台窗口激活与校验工具。

Playwright 的 ``page.bring_to_front()`` 和页面内 ``document.hasFocus()``
只能证明浏览器/DOM 侧认为页面可见，不能证明 Windows 当前前台窗口就是这个
浏览器窗口。OS 鼠标拖动滑块前必须做系统级确认，否则并发窗口可能抢走焦点。
"""

from __future__ import annotations

import ctypes
import os
import time
import uuid
from dataclasses import dataclass
from typing import Any, Optional


SW_RESTORE = 9
GA_ROOT = 2
HWND_TOPMOST = -1
HWND_NOTOPMOST = -2
SWP_NOMOVE = 0x0002
SWP_NOSIZE = 0x0001
SWP_SHOWWINDOW = 0x0040


@dataclass(frozen=True)
class ForegroundFocusResult:
    ok: bool
    hwnd: Optional[int] = None
    foreground_hwnd: Optional[int] = None
    message: str = ""


def ensure_page_foreground(
    page: Any,
    *,
    label: str = "",
    attempts: int = 3,
    delay: float = 0.25,
) -> bool:
    """尽力把 Playwright 页面对应的浏览器窗口切到 Windows 前台。

    Windows 下会给页面标题设置唯一 marker，并通过 EnumWindows 找到包含该
    marker 的顶层窗口，随后强制激活并校验 GetForegroundWindow。非 Windows
    或 Win32 调用失败时，回退为 Playwright/DOM 焦点确认。
    """

    prefix = f"{label} " if label else ""
    marker = f"qwen-slider-{uuid.uuid4().hex}"
    _try_playwright_focus(page)
    marker_set = _set_page_focus_marker(page, marker)

    total_attempts = max(1, int(attempts))
    last_result = ForegroundFocusResult(ok=False, message="尚未尝试")
    try:
        for attempt in range(1, total_attempts + 1):
            _try_playwright_focus(page)
            _set_page_focus_marker(page, marker)
            if os.name != "nt":
                if _dom_focus_ok(page):
                    return True
                last_result = ForegroundFocusResult(ok=False, message="DOM 焦点确认失败")
            else:
                _wait_for_title_marker(page, marker, timeout=0.8)
                hwnd = _find_hwnd_for_page(page, marker)
                if hwnd:
                    _activate_hwnd(hwnd)
                    time.sleep(min(max(delay, 0.0), 0.3))
                    foreground_hwnd = _get_foreground_hwnd()
                    ok = _foreground_matches(hwnd)
                    last_result = ForegroundFocusResult(
                        ok=ok,
                        hwnd=int(hwnd),
                        foreground_hwnd=foreground_hwnd,
                        message="OS 前台窗口匹配" if ok else "OS 前台窗口不匹配",
                    )
                    if ok:
                        return True
                else:
                    if not marker_set and _dom_focus_ok(page):
                        return True
                    last_result = ForegroundFocusResult(ok=False, message="未找到带滑块标记的浏览器窗口")

            if attempt < total_attempts:
                print(
                    f"  ⚠️ {prefix}滑块窗口 OS 前台确认失败 ({attempt}/{total_attempts}): "
                    f"{last_result.message} hwnd={last_result.hwnd} foreground={last_result.foreground_hwnd}",
                    flush=True,
                )
                time.sleep(max(0.0, delay))

        print(
            f"  ❌ {prefix}滑块窗口 OS 前台确认失败: "
            f"{last_result.message} hwnd={last_result.hwnd} foreground={last_result.foreground_hwnd}",
            flush=True,
        )
        return False
    finally:
        _restore_page_focus_marker(page)


def _try_playwright_focus(page: Any) -> None:
    try:
        page.bring_to_front()
    except Exception:
        pass
    try:
        page.evaluate("() => window.focus()")
    except Exception:
        pass


def _set_page_focus_marker(page: Any, marker: str) -> bool:
    try:
        return bool(page.evaluate(
            """
            marker => {
              window.__qwenSliderFocusMarker = marker;
              if (!window.__qwenSliderOriginalTitle) {
                window.__qwenSliderOriginalTitle = document.title || '';
              }
              const original = window.__qwenSliderOriginalTitle || document.title || 'Qwen';
              document.title = `${marker} ${original}`;
              return true;
            }
            """,
            marker,
        ))
    except Exception:
        return False


def _restore_page_focus_marker(page: Any) -> None:
    try:
        page.evaluate(
            """
            () => {
              if (window.__qwenSliderOriginalTitle !== undefined) {
                document.title = window.__qwenSliderOriginalTitle || '';
                delete window.__qwenSliderFocusMarker;
              }
            }
            """
        )
    except Exception:
        pass


def _wait_for_title_marker(page: Any, marker: str, *, timeout: float = 0.8) -> bool:
    deadline = time.monotonic() + max(0.0, timeout)
    while time.monotonic() <= deadline:
        try:
            title = page.evaluate("() => document.title || ''")
            if marker in str(title):
                return True
        except Exception:
            return False
        time.sleep(0.05)
    return False


def _dom_focus_ok(page: Any) -> bool:
    try:
        state = page.evaluate(
            "() => ({ visibilityState: document.visibilityState, hasFocus: document.hasFocus(), url: location.href })"
        )
        if isinstance(state, dict):
            return bool(state.get("visibilityState") == "visible" and state.get("hasFocus"))
        return bool(state)
    except Exception:
        return False


def _get_user32() -> Any:
    return ctypes.windll.user32


def _find_hwnd_by_title_marker(marker: str) -> Optional[int]:
    if os.name != "nt":
        return None
    user32 = _get_user32()
    found: list[int] = []

    enum_proc_type = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)

    def callback(hwnd: int, _lparam: int) -> bool:
        try:
            if not user32.IsWindowVisible(hwnd):
                return True
            length = user32.GetWindowTextLengthW(hwnd)
            if length <= 0:
                return True
            buffer = ctypes.create_unicode_buffer(length + 1)
            user32.GetWindowTextW(hwnd, buffer, length + 1)
            title = buffer.value or ""
            if marker in title:
                found.append(int(hwnd))
                return False
        except Exception:
            return True
        return True

    user32.EnumWindows(enum_proc_type(callback), 0)
    return found[0] if found else None


def _find_hwnd_for_page(page: Any, marker: str) -> Optional[int]:
    hwnd = _find_hwnd_by_title_marker(marker)
    if hwnd:
        return hwnd
    metrics = _get_page_window_metrics(page)
    if not metrics:
        return None
    return _find_hwnd_by_window_metrics(metrics)


def _get_page_window_metrics(page: Any) -> Optional[dict[str, float]]:
    try:
        metrics = page.evaluate(
            """
            () => ({
              screenX: window.screenX,
              screenY: window.screenY,
              outerWidth: window.outerWidth,
              outerHeight: window.outerHeight,
              innerWidth: window.innerWidth,
              innerHeight: window.innerHeight,
              title: document.title || '',
              visibilityState: document.visibilityState,
              hasFocus: document.hasFocus(),
            })
            """
        )
        if not isinstance(metrics, dict):
            return None
        outer_width = float(metrics.get("outerWidth") or 0)
        outer_height = float(metrics.get("outerHeight") or 0)
        if outer_width < 100 or outer_height < 100:
            return None
        return {
            "screenX": float(metrics.get("screenX") or 0),
            "screenY": float(metrics.get("screenY") or 0),
            "outerWidth": outer_width,
            "outerHeight": outer_height,
            "hasFocus": 1.0 if metrics.get("hasFocus") else 0.0,
        }
    except Exception:
        return None


def _find_hwnd_by_window_metrics(metrics: dict[str, float]) -> Optional[int]:
    if os.name != "nt":
        return None
    user32 = _get_user32()
    candidates: list[tuple[float, int]] = []
    enum_proc_type = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)

    class RECT(ctypes.Structure):
        _fields_ = [
            ("left", ctypes.c_long),
            ("top", ctypes.c_long),
            ("right", ctypes.c_long),
            ("bottom", ctypes.c_long),
        ]

    expected_x = float(metrics["screenX"])
    expected_y = float(metrics["screenY"])
    expected_w = float(metrics["outerWidth"])
    expected_h = float(metrics["outerHeight"])

    def callback(hwnd: int, _lparam: int) -> bool:
        try:
            if not user32.IsWindowVisible(hwnd):
                return True
            rect = RECT()
            if not user32.GetWindowRect(hwnd, ctypes.byref(rect)):
                return True
            width = float(rect.right - rect.left)
            height = float(rect.bottom - rect.top)
            if width < 100 or height < 100:
                return True
            score = (
                abs(float(rect.left) - expected_x)
                + abs(float(rect.top) - expected_y)
                + abs(width - expected_w) * 0.25
                + abs(height - expected_h) * 0.25
            )
            if score <= 80.0:
                candidates.append((score, int(hwnd)))
        except Exception:
            return True
        return True

    user32.EnumWindows(enum_proc_type(callback), 0)
    if not candidates:
        return None
    return min(candidates, key=lambda item: item[0])[1]


def _activate_hwnd(hwnd: int) -> bool:
    if os.name != "nt" or not hwnd:
        return False
    user32 = _get_user32()
    kernel32 = ctypes.windll.kernel32
    try:
        user32.ShowWindow(hwnd, SW_RESTORE)
    except Exception:
        pass
    try:
        user32.SetWindowPos(hwnd, HWND_TOPMOST, 0, 0, 0, 0, SWP_NOMOVE | SWP_NOSIZE | SWP_SHOWWINDOW)
        user32.SetWindowPos(hwnd, HWND_NOTOPMOST, 0, 0, 0, 0, SWP_NOMOVE | SWP_NOSIZE | SWP_SHOWWINDOW)
    except Exception:
        pass
    try:
        user32.BringWindowToTop(hwnd)
    except Exception:
        pass
    attached: list[tuple[int, int]] = []
    try:
        current_thread = int(kernel32.GetCurrentThreadId())
        target_thread = int(user32.GetWindowThreadProcessId(hwnd, None))
        foreground = user32.GetForegroundWindow()
        foreground_thread = int(user32.GetWindowThreadProcessId(foreground, None)) if foreground else 0
        for other_thread in {target_thread, foreground_thread}:
            if other_thread and other_thread != current_thread:
                if user32.AttachThreadInput(current_thread, other_thread, True):
                    attached.append((current_thread, other_thread))
        try:
            user32.SetActiveWindow(hwnd)
        except Exception:
            pass
        try:
            user32.SetFocus(hwnd)
        except Exception:
            pass
    except Exception:
        pass
    try:
        return bool(user32.SetForegroundWindow(hwnd))
    except Exception:
        return False
    finally:
        for current_thread, other_thread in reversed(attached):
            try:
                user32.AttachThreadInput(current_thread, other_thread, False)
            except Exception:
                pass


def _get_foreground_hwnd() -> Optional[int]:
    if os.name != "nt":
        return None
    try:
        hwnd = _get_user32().GetForegroundWindow()
        return int(hwnd) if hwnd else None
    except Exception:
        return None


def _foreground_matches(hwnd: int) -> bool:
    foreground = _get_foreground_hwnd()
    if not (hwnd and foreground):
        return False
    target_root = _get_root_hwnd(int(hwnd))
    foreground_root = _get_root_hwnd(int(foreground))
    return bool(target_root and foreground_root and int(target_root) == int(foreground_root))


def _get_root_hwnd(hwnd: int) -> Optional[int]:
    if os.name != "nt" or not hwnd:
        return int(hwnd) if hwnd else None
    try:
        root = _get_user32().GetAncestor(hwnd, GA_ROOT)
        return int(root) if root else int(hwnd)
    except Exception:
        return int(hwnd)
