"""Windows 前台窗口激活与校验工具。

Playwright 的 ``page.bring_to_front()`` 和页面内 ``document.hasFocus()``
只能证明浏览器/DOM 侧认为页面可见，不能证明 Windows 当前前台窗口就是这个
浏览器窗口。OS 鼠标拖动滑块前必须做系统级确认，否则并发窗口可能抢走焦点。
"""

from __future__ import annotations

import ctypes
from ctypes import wintypes
import os
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any, Iterator, Optional


SW_RESTORE = 9
SW_MINIMIZE = 6
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


@dataclass(frozen=True)
class TopmostLease:
    ok: bool
    hwnd: Optional[int] = None

    def __bool__(self) -> bool:
        return self.ok


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

    return _ensure_page_foreground(
        page,
        label=label,
        attempts=attempts,
        delay=delay,
        keep_topmost=False,
        marker_prefix="qwen-slider",
    )


def ensure_page_topmost_foreground(
    page: Any,
    *,
    label: str = "",
    attempts: int = 3,
    delay: float = 0.25,
) -> bool:
    """确认页面位于 Windows 前台，并保持顶层浏览器窗口置顶。"""
    return _ensure_page_foreground(
        page,
        label=label,
        attempts=attempts,
        delay=delay,
        keep_topmost=True,
        marker_prefix="qwen-topmost",
    )


@contextmanager
def hold_page_topmost(page: Any, *, label: str = "") -> Iterator[TopmostLease]:
    """滑块阶段置顶租约：进入时强制置顶，退出时取消置顶。"""
    prefix = f"{label} " if label else ""
    hwnd: Optional[int] = None

    if os.name != "nt":
        _try_playwright_focus(page)
        ok = _dom_focus_ok(page)
        if ok:
            print(f"  ✅ {prefix}滑块窗口前台置顶确认成功（非 Windows 降级）", flush=True)
        else:
            print(f"  ❌ {prefix}滑块窗口前台置顶确认失败（非 Windows 降级）", flush=True)
        yield TopmostLease(ok=ok, hwnd=None)
        return

    marker = f"qwen-topmost-hold-{uuid.uuid4().hex}"
    try:
        _try_playwright_focus(page)
        _set_page_focus_marker(page, marker)
        _wait_for_title_marker(page, marker, timeout=0.8)
        hwnd = _find_hwnd_for_page(page, marker, allow_metrics_fallback=True)
        if not hwnd:
            print(f"  ❌ {prefix}滑块窗口前台置顶确认失败：未找到浏览器窗口", flush=True)
            yield TopmostLease(ok=False, hwnd=None)
            return

        ok = False
        attempts = max(1, int(os.getenv("CAPTCHA_TOPMOST_HOLD_ATTEMPTS", "5") or "5"))
        for attempt in range(1, attempts + 1):
            _try_playwright_focus(page)
            _activate_hwnd(int(hwnd), keep_topmost=True)
            time.sleep(0.12 if attempt < attempts else 0.2)
            ok = _foreground_matches(int(hwnd))
            if ok:
                break
            if attempt < attempts:
                print(
                    f"  ⚠️ {prefix}滑块窗口前台置顶确认失败 "
                    f"({attempt}/{attempts}) hwnd={int(hwnd)} foreground={_get_foreground_hwnd()}，正在重试",
                    flush=True,
                )
        if ok:
            print(f"  ✅ {prefix}滑块窗口已强制置顶 hwnd={int(hwnd)}", flush=True)
            print(f"  ✅ {prefix}滑块窗口前台置顶确认成功", flush=True)
        else:
            print(
                f"  ❌ {prefix}滑块窗口前台置顶确认失败 "
                f"hwnd={int(hwnd)} foreground={_get_foreground_hwnd()}",
                flush=True,
            )
        yield TopmostLease(ok=ok, hwnd=int(hwnd))
    finally:
        if hwnd:
            print(f"  🪟 {prefix}滑块阶段结束，正在取消置顶", flush=True)
            if _clear_hwnd_topmost(int(hwnd)):
                print(f"  ✅ {prefix}滑块窗口置顶已取消", flush=True)
            else:
                print(f"  ⚠️ {prefix}滑块窗口置顶取消失败/跳过", flush=True)
        _restore_page_focus_marker(page)


def _ensure_page_foreground(
    page: Any,
    *,
    label: str,
    attempts: int,
    delay: float,
    keep_topmost: bool,
    marker_prefix: str,
) -> bool:
    prefix = f"{label} " if label else ""
    marker = f"{marker_prefix}-{uuid.uuid4().hex}"
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
                hwnd = _find_hwnd_for_page(page, marker, allow_metrics_fallback=True)
                if hwnd:
                    _activate_hwnd(hwnd, keep_topmost=keep_topmost)
                    time.sleep(min(max(delay, 0.0), 0.3))
                    foreground_hwnd = _get_foreground_hwnd()
                    ok = _foreground_matches(hwnd)
                    last_result = ForegroundFocusResult(
                        ok=ok,
                        hwnd=int(hwnd),
                        foreground_hwnd=foreground_hwnd,
                        message=(
                            "OS 前台置顶窗口匹配"
                            if ok and keep_topmost
                            else "OS 前台窗口匹配"
                            if ok
                            else "OS 前台窗口不匹配"
                        ),
                    )
                    if ok:
                        return True
                else:
                    if not marker_set and _dom_focus_ok(page):
                        return True
                    last_result = ForegroundFocusResult(
                        ok=False,
                        message=(
                            "未找到带滑块标记且可唯一匹配的浏览器窗口"
                            if keep_topmost
                            else "未找到带滑块标记的浏览器窗口"
                        ),
                    )

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


def minimize_page_window(page: Any, *, label: str = "", hwnd: Optional[int] = None) -> bool:
    """最小化 Playwright 页面对应的顶层浏览器窗口。

    该函数用于滑块阶段结束后释放 Windows 前台资源。它不会调用
    ``page.bring_to_front()``，避免在释放焦点前再次抢占前台。
    """
    prefix = f"{label} " if label else ""
    if os.name != "nt":
        print(f"  ⚠️ {prefix}滑块窗口最小化跳过：非 Windows 环境", flush=True)
        return False

    if hwnd:
        try:
            _clear_hwnd_topmost(int(hwnd))
            ok = bool(_get_user32().ShowWindow(int(hwnd), SW_MINIMIZE))
        except Exception as exc:
            print(f"  ⚠️ {prefix}滑块窗口最小化失败/跳过: {exc}", flush=True)
            return False
        if ok:
            print(f"  ✅ {prefix}滑块窗口已最小化", flush=True)
            return True
        print(f"  ⚠️ {prefix}滑块窗口最小化失败/跳过：ShowWindow 返回失败", flush=True)
        return False

    marker = f"qwen-minimize-{uuid.uuid4().hex}"
    try:
        _set_page_focus_marker(page, marker)
        _wait_for_title_marker(page, marker, timeout=0.5)
        hwnd = _find_hwnd_for_page(page, marker, allow_metrics_fallback=True)
        if not hwnd:
            print(f"  ⚠️ {prefix}滑块窗口最小化失败/跳过：未找到浏览器窗口", flush=True)
            return False

        try:
            _clear_hwnd_topmost(int(hwnd))
            ok = bool(_get_user32().ShowWindow(int(hwnd), SW_MINIMIZE))
        except Exception as exc:
            print(f"  ⚠️ {prefix}滑块窗口最小化失败/跳过: {exc}", flush=True)
            return False

        if ok:
            print(f"  ✅ {prefix}滑块窗口已最小化", flush=True)
            return True
        print(f"  ⚠️ {prefix}滑块窗口最小化失败/跳过：ShowWindow 返回失败", flush=True)
        return False
    except Exception as exc:
        print(f"  ⚠️ {prefix}滑块窗口最小化失败/跳过: {exc}", flush=True)
        return False
    finally:
        _restore_page_focus_marker_after_minimize(page)


def get_page_window_rect(page: Any, *, marker_prefix: str = "qwen-rect") -> Optional[dict[str, int]]:
    """返回页面所属顶层浏览器窗口的 Win32 矩形。

    Camoufox/Firefox 的 ``window.screenX/Y`` 可能受到指纹层影响，不能总是
    作为真实 OS 鼠标坐标换算依据。这里通过标题 marker 定位 HWND，并读取
    Windows 真实窗口矩形，供 OS 鼠标拖动链路使用。
    """
    if os.name != "nt":
        return None
    marker = f"{marker_prefix}-{uuid.uuid4().hex}"
    try:
        _set_page_focus_marker(page, marker)
        _wait_for_title_marker(page, marker, timeout=0.5)
        hwnd = _find_hwnd_for_page(page, marker, allow_metrics_fallback=True)
        if not hwnd:
            return None
        rect = _get_hwnd_rect(int(hwnd))
        if not rect:
            return None
        return {
            "left": int(rect[0]),
            "top": int(rect[1]),
            "right": int(rect[2]),
            "bottom": int(rect[3]),
            "hwnd": int(hwnd),
        }
    except Exception:
        return None
    finally:
        _restore_page_focus_marker(page)


def _restore_page_focus_marker_after_minimize(page: Any) -> None:
    try:
        page.evaluate(
            """
            () => {
              if (window.__qwenSliderOriginalTitle !== undefined) {
                document.title = window.__qwenSliderOriginalTitle || '';
                delete window["__qwenSliderFocusMarker"];
                delete window["__qwenSliderOriginalTitle"];
              }
            }
            """
        )
    except Exception:
        pass


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
    user32 = ctypes.windll.user32
    _configure_user32_api(user32)
    return user32


def _configure_user32_api(user32: Any) -> None:
    """为常用 Win32 API 配置 ctypes 原型，避免 64 位 HWND 被当作 c_int 传参。

    如果未声明 ``argtypes``，ctypes 会按 C ``int`` 处理 Python 整数参数。
    这会导致 ``SetWindowPos(hwnd, HWND_NOTOPMOST, ...)`` 在 64 位 Windows 上
    偶发返回 ``ERROR_INVALID_WINDOW_HANDLE(1400)``，表现为滑块结束后取消置顶
    失败。对真实 WinDLL 配置原型；测试中的轻量 fake 不支持属性设置时直接跳过。
    """

    prototypes = {
        "SetWindowPos": (
            [wintypes.HWND, wintypes.HWND, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_uint],
            wintypes.BOOL,
        ),
        "ShowWindow": ([wintypes.HWND, ctypes.c_int], wintypes.BOOL),
        "BringWindowToTop": ([wintypes.HWND], wintypes.BOOL),
        "SetForegroundWindow": ([wintypes.HWND], wintypes.BOOL),
        "GetForegroundWindow": ([], wintypes.HWND),
        "GetAncestor": ([wintypes.HWND, ctypes.c_uint], wintypes.HWND),
        "GetWindowThreadProcessId": ([wintypes.HWND, ctypes.c_void_p], wintypes.DWORD),
        "AttachThreadInput": ([wintypes.DWORD, wintypes.DWORD, wintypes.BOOL], wintypes.BOOL),
        "SetActiveWindow": ([wintypes.HWND], wintypes.HWND),
        "SetFocus": ([wintypes.HWND], wintypes.HWND),
        "IsWindowVisible": ([wintypes.HWND], wintypes.BOOL),
        "GetWindowTextLengthW": ([wintypes.HWND], ctypes.c_int),
        "GetWindowTextW": ([wintypes.HWND, wintypes.LPWSTR, ctypes.c_int], ctypes.c_int),
        "GetWindowRect": ([wintypes.HWND, ctypes.c_void_p], wintypes.BOOL),
        "EnumWindows": ([ctypes.c_void_p, wintypes.LPARAM], wintypes.BOOL),
    }
    for name, (argtypes, restype) in prototypes.items():
        try:
            func = getattr(user32, name)
            func.argtypes = argtypes
            func.restype = restype
        except Exception:
            pass


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


def _find_hwnd_for_page(page: Any, marker: str, *, allow_metrics_fallback: bool = True) -> Optional[int]:
    hwnd = _find_hwnd_by_title_marker(marker)
    if hwnd:
        return hwnd
    if not allow_metrics_fallback:
        return None
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
    candidates.sort(key=lambda item: item[0])
    if len(candidates) > 1 and abs(candidates[1][0] - candidates[0][0]) <= 20.0:
        # 多个窗口位置/尺寸几乎相同，说明 metrics fallback 无法唯一定位页面。
        return None
    return candidates[0][1]


def _get_hwnd_rect(hwnd: int) -> Optional[tuple[int, int, int, int]]:
    if os.name != "nt" or not hwnd:
        return None

    class RECT(ctypes.Structure):
        _fields_ = [
            ("left", ctypes.c_long),
            ("top", ctypes.c_long),
            ("right", ctypes.c_long),
            ("bottom", ctypes.c_long),
        ]

    try:
        rect = RECT()
        if not _get_user32().GetWindowRect(int(hwnd), ctypes.byref(rect)):
            return None
        if rect.right - rect.left < 100 or rect.bottom - rect.top < 100:
            return None
        return int(rect.left), int(rect.top), int(rect.right), int(rect.bottom)
    except Exception:
        return None


def _set_hwnd_topmost(hwnd: int) -> bool:
    if os.name != "nt" or not hwnd:
        return False
    try:
        return bool(_get_user32().SetWindowPos(
            int(hwnd),
            HWND_TOPMOST,
            0,
            0,
            0,
            0,
            SWP_NOMOVE | SWP_NOSIZE | SWP_SHOWWINDOW,
        ))
    except Exception:
        return False


def _clear_hwnd_topmost(hwnd: int) -> bool:
    if os.name != "nt" or not hwnd:
        return False
    flags = SWP_NOMOVE | SWP_NOSIZE
    if _set_window_pos(int(hwnd), HWND_NOTOPMOST, flags):
        return True
    # 某些窗口在恢复/激活状态变化中会拒绝第一次 NOTOPMOST；加 SHOWWINDOW 再试一次。
    return _set_window_pos(int(hwnd), HWND_NOTOPMOST, flags | SWP_SHOWWINDOW)


def _set_window_pos(hwnd: int, insert_after: int, flags: int) -> bool:
    if os.name != "nt" or not hwnd:
        return False
    try:
        _reset_last_error()
        return bool(_get_user32().SetWindowPos(
            int(hwnd),
            int(insert_after),
            0,
            0,
            0,
            0,
            int(flags),
        ))
    except Exception:
        return False


def _last_error() -> int:
    try:
        return int(ctypes.windll.kernel32.GetLastError())
    except Exception:
        return 0


def _reset_last_error() -> None:
    try:
        ctypes.windll.kernel32.SetLastError(0)
    except Exception:
        pass


def _activate_hwnd(hwnd: int, *, keep_topmost: bool = False) -> bool:
    if os.name != "nt" or not hwnd:
        return False
    user32 = _get_user32()
    kernel32 = ctypes.windll.kernel32
    try:
        user32.ShowWindow(hwnd, SW_RESTORE)
    except Exception:
        pass
    if keep_topmost:
        _set_hwnd_topmost(int(hwnd))
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
