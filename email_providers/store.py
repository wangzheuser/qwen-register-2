"""带跨进程锁的已使用邮箱持久化存储。"""

from __future__ import annotations

import json
import threading
from pathlib import Path

try:  # pragma: no cover - exercised when dependency is installed.
    import portalocker  # type: ignore
except ImportError:  # pragma: no cover - fallback keeps tests runnable before dependency install.
    portalocker = None  # type: ignore

_PROCESS_LOCKS: dict[Path, threading.Lock] = {}
_PROCESS_LOCKS_GUARD = threading.Lock()


class _FallbackLock:
    def __init__(self, path: Path, timeout: int = 10) -> None:
        del timeout
        with _PROCESS_LOCKS_GUARD:
            self._lock = _PROCESS_LOCKS.setdefault(path, threading.Lock())

    def __enter__(self):
        self._lock.acquire()
        return self

    def __exit__(self, exc_type, exc, tb):
        self._lock.release()
        return False


def _lock_for(path: Path, timeout: int = 10):
    if portalocker is not None:
        return portalocker.Lock(str(path), timeout=timeout)
    return _FallbackLock(path, timeout=timeout)


class UsedEmailsStore:
    """将已使用邮箱以小写形式保存到 JSON 文件。"""

    def __init__(self, path: str | Path = "used_emails.json") -> None:
        self.path = Path(path)
        self.lock_path = self.path.with_suffix(self.path.suffix + ".lock")
        self.emails: set[str] = set()
        self.load()

    def load(self) -> None:
        if not self.path.exists():
            self.emails = set()
            return
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            self.emails = set()
            return
        self.emails = {str(email).lower() for email in data.get("emails", [])}

    def is_used(self, email: str) -> bool:
        return email.lower() in self.emails

    def claim(self, email: str) -> bool:
        """原子占用邮箱；首次写入返回 True，已存在返回 False。"""
        normalized = email.lower()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with _lock_for(self.lock_path, timeout=10):
            self.load()
            if normalized in self.emails:
                return False
            self.emails.add(normalized)
            payload = {"emails": sorted(self.emails), "count": len(self.emails)}
            self.path.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            return True

    def add(self, email: str) -> None:
        self.claim(email)
