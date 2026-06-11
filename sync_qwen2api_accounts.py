#!/usr/bin/env python3
"""从 qwen_accounts.json 补偿同步账号到 qwen2API。"""

from __future__ import annotations

import argparse
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
import json
from pathlib import Path
import threading
from typing import Any, Iterable

try:
    import portalocker  # type: ignore
except ImportError:  # pragma: no cover - 运行环境通常已安装，保留降级路径。
    portalocker = None  # type: ignore

from integrations.qwen2api import (
    DEFAULT_QWEN2API_ADMIN_KEY,
    DEFAULT_QWEN2API_BASE_URL,
    DEFAULT_QWEN2API_TIMEOUT,
    Qwen2ApiSyncConfig,
    Qwen2ApiSyncResult,
    sync_account_to_qwen2api,
)


_FALLBACK_LOCKS: dict[str, threading.Lock] = {}
_FALLBACK_LOCKS_GUARD = threading.Lock()


@dataclass
class SyncSummary:
    total: int = 0
    pending: int = 0
    success: int = 0
    failed: int = 0
    skipped: int = 0


class _FallbackFileLock:
    def __init__(self, lock_path: Path):
        key = str(lock_path.resolve())
        with _FALLBACK_LOCKS_GUARD:
            self._lock = _FALLBACK_LOCKS.setdefault(key, threading.Lock())

    def __enter__(self):
        self._lock.acquire()
        return self

    def __exit__(self, exc_type, exc, tb):
        self._lock.release()
        return False


@contextmanager
def accounts_file_lock(accounts_file: Path):
    """使用与注册脚本一致的 `<accounts>.lock` 文件锁保护 JSON 写回。"""

    lock_path = Path(f"{accounts_file}.lock")
    if portalocker is not None:
        with portalocker.Lock(str(lock_path), timeout=10):
            yield
        return
    with _FallbackFileLock(lock_path):
        yield


def positive_int(value: str) -> int:
    try:
        parsed = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("必须是正整数") from exc
    if parsed <= 0:
        raise argparse.ArgumentTypeError("必须是正整数")
    return parsed


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="补偿同步 qwen_accounts.json 中的账号到 qwen2API")
    parser.add_argument("--accounts-file", default="qwen_accounts.json", help="账号 JSON 文件路径，默认: qwen_accounts.json")
    parser.add_argument("--qwen2api-base-url", default=DEFAULT_QWEN2API_BASE_URL, help="qwen2API 地址")
    parser.add_argument("--qwen2api-admin-key", default=DEFAULT_QWEN2API_ADMIN_KEY, help="qwen2API 管理员密钥")
    parser.add_argument("--qwen2api-timeout", type=positive_int, default=DEFAULT_QWEN2API_TIMEOUT, help="同步请求超时秒数")
    parser.add_argument("--force", action="store_true", help="强制重新同步所有有 token 的账号")
    parser.add_argument("--dry-run", action="store_true", help="只打印将同步账号，不发请求也不写文件")
    return parser.parse_args(argv)


def load_accounts(path: str | Path) -> list[dict[str, Any]]:
    accounts_path = Path(path)
    if not accounts_path.exists():
        raise FileNotFoundError(f"账号文件不存在: {accounts_path}")
    try:
        data = json.loads(accounts_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"账号文件不是有效 JSON: {accounts_path}") from exc
    if not isinstance(data, list):
        raise ValueError("账号文件必须是 JSON 数组")
    for index, item in enumerate(data, start=1):
        if not isinstance(item, dict):
            raise ValueError(f"账号文件第 {index} 项不是对象")
    return data


def save_accounts(path: str | Path, accounts: Iterable[dict[str, Any]]) -> None:
    accounts_path = Path(path)
    accounts_path.write_text(json.dumps(list(accounts), ensure_ascii=False, indent=2), encoding="utf-8")


def has_token(account: dict[str, Any]) -> bool:
    return bool(str(account.get("token") or "").strip())


def sync_status(account: dict[str, Any]) -> str:
    state = account.get("qwen2api_sync")
    if not isinstance(state, dict):
        return ""
    return str(state.get("status") or "").strip().lower()


def should_sync_account(account: dict[str, Any], *, force: bool = False) -> bool:
    if not has_token(account):
        return False
    if force:
        return True
    return sync_status(account) != "success"


def build_sync_state(result: Qwen2ApiSyncResult, *, synced_at: str | None = None) -> dict[str, Any]:
    if result.skipped:
        status = "skipped"
    elif result.ok:
        status = "success"
    else:
        status = "failed"
    return {
        "status": status,
        "message": result.message,
        "status_code": result.status_code,
        "synced_at": synced_at or datetime.now().isoformat(),
    }


def sync_accounts(accounts: list[dict[str, Any]], *, config: Qwen2ApiSyncConfig, force: bool = False, dry_run: bool = False) -> SyncSummary:
    summary = SyncSummary(total=len(accounts))
    pending_indexes = [index for index, account in enumerate(accounts) if should_sync_account(account, force=force)]
    summary.pending = len(pending_indexes)
    summary.skipped = len(accounts) - len(pending_indexes)

    if dry_run:
        print("🔎 DRY-RUN：以下账号将同步到 qwen2API，不会发请求或写文件")
        for index in pending_indexes:
            account = accounts[index]
            print(f"  - {account.get('email') or '<missing-email>'}")
        return summary

    for index in pending_indexes:
        account = accounts[index]
        email = str(account.get("email") or "").strip()
        password = str(account.get("password") or "")
        token = str(account.get("token") or "").strip()
        print(f"  🔁 同步 qwen2API: {email}")
        result = sync_account_to_qwen2api(email=email, password=password, token=token, config=config)
        account["qwen2api_sync"] = build_sync_state(result)
        if result.ok:
            summary.success += 1
            print(f"  ✅ {result.message}")
        elif result.skipped:
            summary.skipped += 1
            print(f"  ⚠️ {result.message}")
        else:
            summary.failed += 1
            print(f"  ❌ qwen2API 同步失败: {result.message}")

    return summary


def print_summary(summary: SyncSummary) -> None:
    print("\n════════════════════════════════════════════════════════════")
    print(f"📊 总账号数: {summary.total}")
    print(f"🔁 待同步: {summary.pending}")
    print(f"✅ 成功: {summary.success}")
    print(f"❌ 失败: {summary.failed}")
    print(f"⏭️ 跳过: {summary.skipped}")
    print("════════════════════════════════════════════════════════════")


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    accounts_path = Path(args.accounts_file)
    config = Qwen2ApiSyncConfig(
        enabled=True,
        base_url=args.qwen2api_base_url,
        admin_key=args.qwen2api_admin_key,
        timeout=args.qwen2api_timeout,
    )

    with accounts_file_lock(accounts_path):
        accounts = load_accounts(accounts_path)
        summary = sync_accounts(accounts, config=config, force=bool(args.force), dry_run=bool(args.dry_run))
        if not args.dry_run:
            save_accounts(accounts_path, accounts)

    print_summary(summary)
    return 1 if summary.failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
