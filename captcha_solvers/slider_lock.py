
"""滑块验证码全局串行锁。"""

from __future__ import annotations

import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator, Optional

try:  # pragma: no cover - portalocker 缺失时回退进程内锁。
    import portalocker  # type: ignore
except Exception:  # pragma: no cover
    portalocker = None  # type: ignore


_PROCESS_LOCK = threading.Lock()
DEFAULT_SLIDER_LOCK_PATH = Path(".slider-captcha.lock")


class SliderLockTimeout(RuntimeError):
    """等待滑块锁被取消或超时。"""


def _cancelled(stop_event: Optional[object]) -> bool:
    return bool(stop_event is not None and getattr(stop_event, "is_set", lambda: False)())


@contextmanager
def acquire_slider_lock(
    label: str = "",
    *,
    stop_event: Optional[object] = None,
    lock_path: str | Path = DEFAULT_SLIDER_LOCK_PATH,
    poll_interval: float = 0.2,
) -> Iterator[None]:
    """串行化完整滑块处理流程，保护 OS 鼠标、焦点和验证码状态。"""
    prefix = f"{label} " if label else ""
    print(f"  ⏳ {prefix}等待滑块全局锁...", flush=True)
    acquired_process_lock = False
    file_lock = None
    try:
        while True:
            if _cancelled(stop_event):
                raise SliderLockTimeout("等待滑块全局锁时收到停止请求")
            acquired_process_lock = _PROCESS_LOCK.acquire(blocking=False)
            if acquired_process_lock:
                break
            time.sleep(poll_interval)

        if portalocker is not None:
            file_lock = portalocker.Lock(str(lock_path), timeout=0)
            while True:
                if _cancelled(stop_event):
                    raise SliderLockTimeout("等待滑块全局文件锁时收到停止请求")
                try:
                    file_lock.acquire()
                    break
                except portalocker.exceptions.LockException:
                    time.sleep(poll_interval)

        print(f"  🔐 {prefix}已获得滑块全局锁", flush=True)
        yield
    finally:
        if file_lock is not None:
            try:
                file_lock.release()
            except Exception:
                pass
        if acquired_process_lock:
            _PROCESS_LOCK.release()
        print(f"  🔓 {prefix}释放滑块全局锁", flush=True)
