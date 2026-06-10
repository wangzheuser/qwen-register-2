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
DEFAULT_SLIDER_FILE_LOCK_TIMEOUT = 240.0


def _foreground_intent_lock_path(lock_path: str | Path) -> Path:
    """跨进程前台意图锁路径。

    该锁用于在真正持有前台文件锁前声明“将要使用前台”。slider/window
    都需要持有它，从而避免一个进程已经准备拖滑块时，另一个进程新建窗口抢前台。
    """
    return Path(f"{lock_path}.intent")


class SliderLockTimeout(RuntimeError):
    """等待滑块锁被取消或超时。"""


@dataclass
class _ForegroundRequest:
    request_id: int
    kind: str
    label: str
    queued_at: float
    reserved_at: float = 0.0
    acquired_at: float = 0.0
    acquired_logged: bool = False

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
        announce_acquired: bool = True,
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
                    request.reserved_at = time.monotonic()
                    self._owner = request
                    if announce_acquired:
                        self.announce_acquired(request)
                    return request
                self._condition.wait(timeout=poll_interval)

    def announce_acquired(self, request: _ForegroundRequest) -> None:
        with self._condition:
            if self._owner is not request or request.acquired_logged:
                return
            request.acquired_at = time.monotonic()
            request.acquired_logged = True
            waited = request.acquired_at - request.queued_at
            print(
                f"  🔐 {self._prefix(request)}已获得前台焦点锁 "
                f"type={request.kind} waited={_format_seconds(waited)} "
                f"queue_slider={len(self._slider_queue)} queue_window={len(self._window_queue)}",
                flush=True,
            )

    def release(self, request: _ForegroundRequest) -> None:
        with self._condition:
            if self._owner is not request:
                return
            if request.acquired_logged:
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


def _acquire_foreground_file_lock(
    *,
    kind: str,
    label: str,
    lock_path: str | Path,
    stop_event: Optional[object],
    poll_interval: float,
    file_lock_timeout: float,
    flags: Optional[object] = None,
):
    """获取跨进程前台文件锁；portalocker 不可用时返回空锁。"""
    if portalocker is None:
        return None

    if flags is None:
        flags = portalocker.LockFlags.EXCLUSIVE
    file_lock = portalocker.Lock(str(lock_path), timeout=0, flags=flags)
    wait_started = time.monotonic()
    last_wait_log = wait_started
    while True:
        if _cancelled(stop_event):
            raise SliderLockTimeout(f"等待前台焦点文件锁 type={kind} 时收到停止请求")
        try:
            file_lock.acquire()
            return file_lock
        except portalocker.exceptions.LockException:
            elapsed = time.monotonic() - wait_started
            if elapsed >= file_lock_timeout:
                raise SliderLockTimeout(
                    f"等待前台焦点文件锁 type={kind} 超时 "
                    f"({elapsed:.1f}s/{file_lock_timeout:.1f}s)"
                )
            now = time.monotonic()
            if now - last_wait_log >= 5.0:
                print(
                    f"  ⏳ {label + ' ' if label else ''}等待前台焦点文件锁 "
                    f"type={kind} elapsed={elapsed:.1f}s...",
                    flush=True,
                )
                last_wait_log = now
            time.sleep(poll_interval)


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
    intent_lock = None
    file_lock = None
    request = None
    try:
        # 先进入进程内 FIFO 队列，避免同一进程内多个 slider waiter 在文件锁
        # 层面反向抢占；但此时只是“预约”前台，不打印“已获得”。真正拿到
        # 跨进程文件锁后再 announce，避免 Camoufox 多进程日志误导。
        request = _COORDINATOR.acquire(
            "slider",
            label=label,
            stop_event=stop_event,
            poll_interval=poll_interval,
            announce_acquired=False,
        )
        intent_lock = _acquire_foreground_file_lock(
            kind="slider-intent",
            label=label,
            lock_path=_foreground_intent_lock_path(lock_path),
            stop_event=stop_event,
            poll_interval=poll_interval,
            file_lock_timeout=file_lock_timeout,
            flags=(portalocker.LockFlags.EXCLUSIVE | portalocker.LockFlags.NON_BLOCKING) if portalocker is not None else None,
        )
        file_lock = _acquire_foreground_file_lock(
            kind="slider",
            label=label,
            lock_path=lock_path,
            stop_event=stop_event,
            poll_interval=poll_interval,
            file_lock_timeout=file_lock_timeout,
            flags=(portalocker.LockFlags.EXCLUSIVE | portalocker.LockFlags.NON_BLOCKING) if portalocker is not None else None,
        )
        _COORDINATOR.announce_acquired(request)
        yield
    finally:
        if request is not None:
            _COORDINATOR.release(request)
        if file_lock is not None:
            try:
                file_lock.release()
            except Exception:
                pass
        if intent_lock is not None:
            try:
                intent_lock.release()
            except Exception:
                pass


@contextmanager
def acquire_foreground_window_lock(
    label: str = "",
    *,
    stop_event: Optional[object] = None,
    lock_path: str | Path = DEFAULT_SLIDER_LOCK_PATH,
    poll_interval: float = 0.2,
    file_lock_timeout: float = DEFAULT_SLIDER_FILE_LOCK_TIMEOUT,
) -> Iterator[None]:
    """串行化短前台窗口操作；滑块声明前台意图时 window 需避让。

    重要：Camoufox 启动/new_page 偶发卡顿，window 不能长期持有任何跨进程
    文件锁，否则会把后续 slider 拖死。这里仅在进入前探测 slider intent 是否空闲，
    探测成功后立即释放文件锁；实际滑块拖动前仍会强制置顶并复检焦点。
    """
    request = None
    try:
        request = _COORDINATOR.acquire(
            "window",
            label=label,
            stop_event=stop_event,
            poll_interval=poll_interval,
            announce_acquired=False,
        )
        intent_probe = _acquire_foreground_file_lock(
            kind="window-intent",
            label=label,
            lock_path=_foreground_intent_lock_path(lock_path),
            stop_event=stop_event,
            poll_interval=poll_interval,
            file_lock_timeout=file_lock_timeout,
            flags=(portalocker.LockFlags.EXCLUSIVE | portalocker.LockFlags.NON_BLOCKING) if portalocker is not None else None,
        )
        if intent_probe is not None:
            try:
                intent_probe.release()
            except Exception:
                pass
        _COORDINATOR.announce_acquired(request)
        yield
    finally:
        if request is not None:
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
