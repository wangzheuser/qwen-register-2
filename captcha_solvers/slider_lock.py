"""滑块验证码和前台窗口操作的全局串行协调器。"""

from __future__ import annotations

import itertools
import os
import re
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

    该锁只用于 slider 阶段，在真正持有前台文件锁前声明“将要使用前台”。
    window 启动/新建页不再等待该文件锁；Camoufox new_page 偶发卡顿时若
    window 也等待/持有跨进程锁，会拖慢整个滑块流水线。父进程启动槽仍可
    用该文件探测并节流新 worker，真正拖动则由 slider 文件锁兜底。
    """
    return Path(f"{lock_path}.intent")


def _slider_file_queue_path(lock_path: str | Path) -> Path:
    """跨进程 slider FIFO 队列目录。"""
    return Path(f"{lock_path}.queue")


def _sanitize_queue_label(label: str) -> str:
    text = re.sub(r"[^0-9A-Za-z_.-]+", "_", str(label or "slider"))
    return text[:48] or "slider"


def _pid_is_running(pid: int) -> bool:
    if pid == os.getpid():
        return True
    if os.name == "nt":
        import ctypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        open_process = kernel32.OpenProcess
        open_process.argtypes = [ctypes.c_uint32, ctypes.c_int, ctypes.c_uint32]
        open_process.restype = ctypes.c_void_p
        close_handle = kernel32.CloseHandle
        close_handle.argtypes = [ctypes.c_void_p]
        handle = open_process(0x1000, False, pid)
        if handle:
            close_handle(handle)
            return True
        return ctypes.get_last_error() == 5
    try:
        os.kill(pid, 0)
        return True
    except PermissionError:
        return True
    except ProcessLookupError:
        return False


def _remove_dead_slider_tickets(queue_dir: Path, current_ticket: Path) -> None:
    for queued_ticket in queue_dir.glob("*.ticket"):
        if queued_ticket == current_ticket:
            continue
        try:
            owner_pid = int(queued_ticket.name.split("-", 3)[1])
        except (IndexError, ValueError):
            continue
        if not _pid_is_running(owner_pid):
            queued_ticket.unlink(missing_ok=True)


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
def _acquire_slider_file_queue_turn(
    *,
    label: str,
    lock_path: str | Path,
    stop_event: Optional[object],
    poll_interval: float,
    file_lock_timeout: float,
) -> Iterator[None]:
    """用目录票据实现跨进程 slider FIFO，避免后来的进程插队文件锁。

    portalocker 的非阻塞轮询不保证公平性；并发 Camoufox worker 中会出现
    早等待者长期卡在 intent/file lock，而后来的账号先拿到滑块锁。
    这里用原子创建票据文件 + 文件名排序作为轻量 FIFO。
    """
    queue_dir = _slider_file_queue_path(lock_path)
    queue_dir.mkdir(parents=True, exist_ok=True)
    now_ns = time.time_ns()
    ticket = queue_dir / f"{now_ns:020d}-{os.getpid()}-{threading.get_ident()}-{_sanitize_queue_label(label)}.ticket"
    try:
        ticket.write_text(str(time.monotonic()), encoding="utf-8")
        wait_started = time.monotonic()
        last_wait_log = wait_started
        while True:
            if _cancelled(stop_event):
                raise SliderLockTimeout("等待跨进程滑块队列时收到停止请求")
            try:
                _remove_dead_slider_tickets(queue_dir, ticket)
                tickets = sorted(queue_dir.glob("*.ticket"), key=lambda path: path.name)
            except Exception:
                tickets = [ticket]
            if tickets and tickets[0].name == ticket.name:
                yield
                return
            elapsed = time.monotonic() - wait_started
            if elapsed >= file_lock_timeout:
                raise SliderLockTimeout(
                    f"等待跨进程滑块队列超时 ({elapsed:.1f}s/{file_lock_timeout:.1f}s)"
                )
            now = time.monotonic()
            if now - last_wait_log >= 5.0:
                print(
                    f"  ⏳ {label + ' ' if label else ''}等待跨进程滑块队列 "
                    f"elapsed={elapsed:.1f}s queue={len(tickets)}...",
                    flush=True,
                )
                last_wait_log = now
            time.sleep(poll_interval)
    finally:
        try:
            ticket.unlink(missing_ok=True)
        except Exception:
            pass


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
        with _acquire_slider_file_queue_turn(
            label=label,
            lock_path=lock_path,
            stop_event=stop_event,
            poll_interval=poll_interval,
            file_lock_timeout=file_lock_timeout,
        ):
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
    """串行化本进程短前台窗口操作。

    重要：Camoufox 启动/new_page 偶发卡顿，window 不能等待或持有任何跨进程
    文件锁，否则会把后续 slider/worker 流水线拖慢。这里仅做进程内协调；
    父进程在启动 worker 前可单独通过 intent 文件节流，真正滑块拖动前仍会
    通过 slider 文件锁、Windows 置顶和焦点复检兜底。
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
