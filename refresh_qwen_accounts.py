#!/usr/bin/env python3
"""重新登录本地 Qwen 账号，刷新 token，并删除明确失效的本地凭据。"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import random
import shutil
import tempfile
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path
from typing import Any

import httpx

from sync_qwen2api_accounts import accounts_file_lock, load_accounts, positive_int


SIGNIN_URL = "https://chat.qwen.ai/api/v1/auths/signin"
DEAD_MARKERS = (
    "invalid email or password",
    "incorrect email or password",
    "email or password is incorrect",
    "wrong password",
    "user not found",
    "account not found",
    "account does not exist",
    "account disabled",
    "account deleted",
    "用户不存在",
    "账号不存在",
    "密码错误",
    "账号已禁用",
    "账号已删除",
)
_clients = threading.local()


def normalize_proxy(value: str) -> str | None:
    value = value.strip()
    if not value:
        return None
    return value if "://" in value else f"http://{value}"


def nonnegative_int(value: str) -> int:
    parsed = int(value)
    if parsed < 0:
        raise argparse.ArgumentTypeError("必须是非负整数")
    return parsed


def get_client(proxy: str | None) -> httpx.Client:
    client = getattr(_clients, "client", None)
    if client is None:
        client = httpx.Client(proxy=proxy, timeout=httpx.Timeout(45, connect=20))
        _clients.client = client
    return client


def response_message(response: httpx.Response) -> tuple[dict[str, Any], str]:
    try:
        data = response.json()
    except Exception:
        return {}, response.text[:1000].lower()
    if not isinstance(data, dict):
        return {}, str(data)[:1000].lower()
    message = " | ".join(
        str(data[key]) for key in ("code", "message", "detail", "error", "error_description", "msg") if data.get(key) is not None
    )
    return data, message.lower()[:1000]


def verify_account(index: int, account: dict[str, Any], proxy: str | None) -> dict[str, Any]:
    email = str(account.get("email") or "").strip()
    password = str(account.get("password") or "")
    if not email or not password:
        return {"index": index, "email": email, "state": "dead", "reason": "missing_local_credentials"}

    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/147.0.0.0 Safari/537.36",
        "Origin": "https://chat.qwen.ai",
        "Referer": "https://chat.qwen.ai/auth?mode=login",
        "x-device-id": str(account.get("device_id") or uuid.uuid4()),
    }
    payload = {"email": email, "password": hashlib.sha256(password.encode()).hexdigest()}
    reason = "request_failed"

    for attempt in range(4):
        try:
            response = get_client(proxy).post(SIGNIN_URL, headers=headers, json=payload)
            data, message = response_message(response)
            if response.status_code == 200:
                token = str(data.get("token") or "").strip()
                returned_email = str(data.get("email") or email).strip()
                if token and returned_email.casefold() == email.casefold():
                    return {
                        "index": index,
                        "email": email,
                        "state": "live",
                        "token": token,
                        "name": data.get("name"),
                        "role": data.get("role"),
                    }
                return {"index": index, "email": email, "state": "uncertain", "reason": "malformed_success"}
            if any(marker in message for marker in DEAD_MARKERS):
                return {"index": index, "email": email, "state": "dead", "reason": f"http_{response.status_code}:{message[:180]}"}
            reason = f"http_{response.status_code}:{message[:180] or 'no_message'}"
            if response.status_code not in (408, 425, 429) and response.status_code < 500:
                break
        except (httpx.TimeoutException, httpx.NetworkError, httpx.ProxyError) as exc:
            reason = f"{type(exc).__name__}:{str(exc)[:160]}"
        if attempt < 3:
            time.sleep(1.5 * (2**attempt) + random.random())
    return {"index": index, "email": email, "state": "uncertain", "reason": reason}


def atomic_write(path: Path, content: str) -> None:
    fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_name, path)
    except Exception:
        Path(temp_name).unlink(missing_ok=True)
        raise


def account_line(account: dict[str, Any]) -> str:
    token = str(account.get("token") or "")
    suffix = f" | token={token[:50]}..." if token else ""
    return (
        f"{account.get('email', '')}:{account.get('password', '')}\t# {account.get('name', '')} | "
        f"{account.get('country', 'unknown')} ({account.get('ip', 'unknown')}){suffix}\n"
    )


def apply_results(
    accounts_file: Path, txt_file: Path, results: list[dict[str, Any]], backup_root: Path
) -> tuple[int, int, Path]:
    by_email = {str(result["email"]).casefold(): result for result in results}
    backup_dir = backup_root / f"account-prune-backup-{datetime.now():%Y%m%d-%H%M%S}"

    with accounts_file_lock(accounts_file):
        accounts = load_accounts(accounts_file)
        backup_dir.mkdir(parents=True)
        shutil.copy2(accounts_file, backup_dir / accounts_file.name)
        if txt_file.exists():
            shutil.copy2(txt_file, backup_dir / txt_file.name)

        survivors = []
        deleted = refreshed = 0
        verified_at = datetime.now().isoformat()
        for account in accounts:
            result = by_email.get(str(account.get("email") or "").casefold())
            if result and result["state"] == "dead":
                deleted += 1
                continue
            if result and result["state"] == "live":
                refreshed += account.get("token") != result["token"]
                account["token"] = account["active_token"] = result["token"]
                account["name"] = result.get("name") or account.get("name")
                account["user_role"] = result.get("role") or account.get("user_role", "user")
                account["status"] = "activated"
                account["last_verified_at"] = verified_at
                account["qwen2api_sync"] = {"status": "pending", "message": "token refreshed; resync required"}
            survivors.append(account)

        atomic_write(accounts_file, json.dumps(survivors, ensure_ascii=False, indent=2) + "\n")
        atomic_write(txt_file, "".join(map(account_line, survivors)))
    return deleted, refreshed, backup_dir


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--accounts-file", default="qwen_accounts.json")
    parser.add_argument("--txt-file", default="qwen_accounts.txt")
    parser.add_argument("--proxy-file", default="proxy.txt")
    parser.add_argument("--workers", type=positive_int, default=5)
    parser.add_argument("--limit", type=nonnegative_int, default=0, help="只验证前 N 个账号，0 表示全部")
    parser.add_argument("--apply", action="store_true", help="刷新存活账号并删除明确失效账号")
    args = parser.parse_args()

    accounts_file, txt_file = Path(args.accounts_file), Path(args.txt_file)
    accounts = load_accounts(accounts_file)
    selected = accounts[: args.limit] if args.limit else accounts
    proxy_file = Path(args.proxy_file)
    proxy = normalize_proxy(proxy_file.read_text(encoding="utf-8")) if proxy_file.exists() else None
    results = []

    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        futures = [pool.submit(verify_account, index, account, proxy) for index, account in enumerate(selected)]
        for done, future in enumerate(as_completed(futures), 1):
            result = future.result()
            results.append(result)
            if result["state"] != "live":
                print(f"[{done}/{len(selected)}] {result['state']} {result['email']} {result['reason']}")
            elif done % 25 == 0 or done == len(selected):
                print(f"[{done}/{len(selected)}] progress")

    counts = {state: sum(result["state"] == state for result in results) for state in ("live", "dead", "uncertain")}
    print("SUMMARY " + json.dumps(counts, ensure_ascii=False))
    if args.apply:
        deleted, refreshed, backup = apply_results(accounts_file, txt_file, results, Path("logs"))
        print(f"APPLIED deleted={deleted} refreshed={refreshed} backup={backup.resolve()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
