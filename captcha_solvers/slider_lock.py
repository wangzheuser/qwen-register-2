"""滑块验证码和前台窗口操作的全局串行协调器。"""

from __future__ import annotations

import itertools
import threading
import time
from collections import deque
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Deque, Iterator, Optional

try:  # pragma: no cover - portalocker 缺失时回退进程内锁。
    import portalocker  # type: ignore
except Exception:  # pragma: no cover
    portalocker = None  # type: ignore


DEFAULT_SLIDER_LOCK_PATH = Path(".slider-captcha.lock")
DEFAULT_SLIDER_FILE_LOCK_TIMEOUT = 20.0


class SliderLockTimeout(RuntimeError):
    """等待滑块锁被取消或超时。"""


@dataclass
class _ForegroundRequest:
    request_id: int
    kind: str
    label: str
    queued_at: float
    acquired_at: float = 0.0

    @property
    def display_label(self) -> str:
        return self.label or f"request-{self.request_id}"


def _cancelled(stop_event: Optional[object]) -> bool:
    return bool(stop_event is not None and getattr(stop_event, "is_set", lambda: False)())


def _format_seconds(value: float) -> str:
    return f"{max(0.0, value):.3f}s"


class ForegroundCoordinator:
    """进程内前台敏感操作协调器。

    slider 高优先级且 FIFO；window 低优先级，只在没有等待 slider 时执行。
    """

    def __init__(self) -> None:
        self._condition = threading.Condition()
        self._ids = itertools.count(1)
        self._owner: Optional[_ForegroundRequest] = None
        self._slider_queue: Deque[_ForegroundRequest] = deque()
        self._window_queue: Deque[_ForegroundRequest] = deque()

    def acquire(
        self,
        kind: str,
        label: str = "",
        *,
        stop_event: Optional[object] = None,
        poll_interval: float = 0.2,
    ) -> _ForegroundRequest:
        if kind not in {"slider", "window"}:
            raise ValueError(f"未知前台锁类型: {kind}")

        request = _ForegroundRequest(
            request_id=next(self._ids),
            kind=kind,
            label=label,
            queued_at=time.monotonic(),
        )
        with self._condition:
            queue = self._queue_for(kind)
            queue.append(request)
            print(
                f"  ⏳ {self._prefix(request)}等待前台焦点锁 "
                f"type={kind} owner={self._owner_text()} "
                f"queue_slider={len(self._slider_queue)} queue_window={len(self._window_queue)}...",
                flush=True,
            )
            while True:
                if _cancelled(stop_event):
                    self._remove_request(request)
                    self._condition.notify_all()
                    raise SliderLockTimeout(f"等待前台焦点锁 type={kind} 时收到停止请求")
                if self._can_acquire(request):
                    self._queue_for(kind).popleft()
                    request.acquired_at = time.monotonic()
                    self._owner = request
                    waited = request.acquired_at - request.queued_at
                    print(
                        f"  🔐 {self._prefix(request)}已获得前台焦点锁 "
                        f"type={kind} waited={_format_seconds(waited)} "
                        f"queue_slider={len(self._slider_queue)} queue_window={len(self._window_queue)}",
                        flush=True,
                    )
                    return request
                self._condition.wait(timeout=poll_interval)

    def release(self, request: _ForegroundRequest) -> None:
        with self._condition:
            if self._owner is not request:
                return
            held = time.monotonic() - request.acquired_at if request.acquired_at else 0.0
            print(
                f"  🔓 {self._prefix(request)}释放前台焦点锁 "
                f"type={request.kind} held={_format_seconds(held)} "
                f"queue_slider={len(self._slider_queue)} queue_window={len(self._window_queue)}",
                flush=True,
            )
            self._owner = None
            self._condition.notify_all()

    def _can_acquire(self, request: _ForegroundRequest) -> bool:
        if self._owner is not None:
            return False
        if request.kind == "slider":
            return bool(self._slider_queue and self._slider_queue[0] is request)
        return (
            not self._slider_queue
            and bool(self._window_queue)
            and self._window_queue[0] is request
        )

    def _queue_for(self, kind: str) -> Deque[_ForegroundRequest]:
        return self._slider_queue if kind == "slider" else self._window_queue

    def _remove_request(self, request: _ForegroundRequest) -> None:
        queue = self._queue_for(request.kind)
        try:
            queue.remove(request)
        except ValueError:
            pass

    def _owner_text(self) -> str:
        if self._owner is None:
            return "无"
        return f"{self._owner.display_label}/{self._owner.kind}"

    @staticmethod
    def _prefix(request: _ForegroundRequest) -> str:
        return f"{request.label} " if request.label else ""


_COORDINATOR = ForegroundCoordinator()


@contextmanager
def acquire_slider_lock(
    label: str = "",
    *,
    stop_event: Optional[object] = None,
    lock_path: str | Path = DEFAULT_SLIDER_LOCK_PATH,
    poll_interval: float = 0.2,
    file_lock_timeout: float = DEFAULT_SLIDER_FILE_LOCK_TIMEOUT,
) -> Iterator[None]:
    """串行化完整滑块处理流程，保护 OS 鼠标、焦点和验证码状态。"""
    request = _COORDINATOR.acquire(
        "slider",
        label=label,
        stop_event=stop_event,
        poll_interval=poll_interval,
    )
    file_lock = None
    file_lock_acquired = False
    try:
        if portalocker is not None:
            file_lock = portalocker.Lock(str(lock_path), timeout=0)
            wait_started = time.monotonic()
            last_wait_log = wait_started
            while True:
                if _cancelled(stop_event):
                    raise SliderLockTimeout("等待前台焦点文件锁 type=slider 时收到停止请求")
                try:
                    file_lock.acquire()
                    file_lock_acquired = True
                    break
                except portalocker.exceptions.LockException:
                    elapsed = time.monotonic() - wait_started
                    if elapsed >= file_lock_timeout:
                        raise SliderLockTimeout(
                            f"等待前台焦点文件锁 type=slider 超时 "
                            f"({elapsed:.1f}s/{file_lock_timeout:.1f}s)"
                        )
                    now = time.monotonic()
                    if now - last_wait_log >= 5.0:
                        print(
                            f"  ⏳ {label + ' ' if label else ''}等待前台焦点文件锁 "
                            f"type=slider elapsed={elapsed:.1f}s...",
                            flush=True,
                        )
                        last_wait_log = now
                    time.sleep(poll_interval)
        yield
    finally:
        if file_lock is not None and file_lock_acquired:
            try:
                file_lock.release()
            except Exception:
                pass
        _COORDINATOR.release(request)


@contextmanager
def acquire_foreground_window_lock(
    label: str = "",
    *,
    stop_event: Optional[object] = None,
    lock_path: str | Path = DEFAULT_SLIDER_LOCK_PATH,
    poll_interval: float = 0.2,
) -> Iterator[None]:
    """串行化短前台窗口操作；有滑块等待时不能插队。"""
    _ = lock_path  # 兼容旧调用签名；window 锁不使用跨进程文件锁。
    request = _COORDINATOR.acquire(
        "window",
        label=label,
        stop_event=stop_event,
        poll_interval=poll_interval,
    )
    try:
        yield
    finally:
        _COORDINATOR.release(request)


@contextmanager
def acquire_foreground_lock(
    label: str = "",
    *,
    stop_event: Optional[object] = None,
    lock_path: str | Path = DEFAULT_SLIDER_LOCK_PATH,
    poll_interval: float = 0.2,
) -> Iterator[None]:
    """兼容旧名称；等同于低优先级 window 前台锁。"""
    with acquire_foreground_window_lock(
        label=label,
        stop_event=stop_event,
        lock_path=lock_path,
        poll_interval=poll_interval,
    ):
        yield
