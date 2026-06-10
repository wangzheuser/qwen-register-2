"""qwen2API 账号同步集成。"""

from __future__ import annotations

from concurrent.futures import Future, ThreadPoolExecutor, wait
from dataclasses import dataclass
import threading
from typing import Optional

import httpx


DEFAULT_QWEN2API_BASE_URL = "http://127.0.0.1:7860"
DEFAULT_QWEN2API_ADMIN_KEY = "admin"
DEFAULT_QWEN2API_TIMEOUT = 30


@dataclass(frozen=True)
class Qwen2ApiSyncConfig:
    """qwen2API 同步配置。"""

    enabled: bool = False
    base_url: str = DEFAULT_QWEN2API_BASE_URL
    admin_key: str = DEFAULT_QWEN2API_ADMIN_KEY
    timeout: int = DEFAULT_QWEN2API_TIMEOUT


@dataclass(frozen=True)
class Qwen2ApiSyncResult:
    """qwen2API 同步结果。"""

    ok: bool
    skipped: bool = False
    message: str = ""
    status_code: Optional[int] = None


def _accounts_url(base_url: str) -> str:
    return f"{base_url.rstrip('/')}/api/admin/accounts"


def sync_account_to_qwen2api(
    *,
    email: str,
    password: str,
    token: Optional[str],
    config: Qwen2ApiSyncConfig,
    client: Optional[httpx.Client] = None,
) -> Qwen2ApiSyncResult:
    """把账号同步到 qwen2API 后端账号池；失败不抛出。"""
    if not config.enabled:
        return Qwen2ApiSyncResult(ok=False, skipped=True, message="qwen2API 同步未启用")

    clean_token = (token or "").strip()
    if not clean_token:
        return Qwen2ApiSyncResult(ok=False, skipped=True, message="未提取到 token，跳过 qwen2API 同步")

    owns_client = client is None
    http_client = client or httpx.Client(timeout=float(config.timeout))
    try:
        try:
            response = http_client.post(
                _accounts_url(config.base_url),
                headers={"Authorization": f"Bearer {config.admin_key}"},
                json={"token": clean_token, "email": email, "password": password},
                timeout=float(config.timeout),
            )
        except httpx.TimeoutException as exc:
            return Qwen2ApiSyncResult(ok=False, message=f"qwen2API 同步超时: {exc}")
        except httpx.HTTPError as exc:
            return Qwen2ApiSyncResult(ok=False, message=f"qwen2API 同步请求失败: {exc}")

        if response.status_code >= 400:
            return Qwen2ApiSyncResult(
                ok=False,
                message=f"qwen2API 同步失败，HTTP {response.status_code}: {response.text[:200]}",
                status_code=response.status_code,
            )

        try:
            data = response.json()
        except ValueError:
            return Qwen2ApiSyncResult(
                ok=False,
                message=f"qwen2API 同步响应不是有效 JSON: {response.text[:200]}",
                status_code=response.status_code,
            )

        if data.get("ok") is True:
            synced_email = data.get("email") or email
            return Qwen2ApiSyncResult(
                ok=True,
                message=f"qwen2API 同步成功: {synced_email}",
                status_code=response.status_code,
            )

        return Qwen2ApiSyncResult(
            ok=False,
            message=str(data.get("error") or data.get("detail") or "qwen2API 同步失败"),
            status_code=response.status_code,
        )
    finally:
        if owns_client:
            http_client.close()


class Qwen2ApiAsyncSyncer:
    """qwen2API 后台同步队列。"""

    def __init__(self, max_workers: int = 2):
        self._executor = ThreadPoolExecutor(max_workers=max(1, int(max_workers)), thread_name_prefix="qwen2api-sync")
        self._futures: list[Future] = []
        self._lock = threading.Lock()

    def submit(
        self,
        *,
        email: str,
        password: str,
        token: Optional[str],
        config: Qwen2ApiSyncConfig,
        label: str = "",
    ) -> Optional[Future]:
        """提交后台同步任务；未启用或无 token 时不创建任务。"""
        if not config.enabled:
            return None
        if not (token or "").strip():
            prefix = f"{label} " if label else ""
            print(f"  ⚠️ {prefix}未提取到 token，跳过 qwen2API 后台同步")
            return None
        future = self._executor.submit(
            sync_account_to_qwen2api,
            email=email,
            password=password,
            token=token,
            config=config,
        )
        with self._lock:
            self._futures.append(future)
        prefix = f"{label} " if label else ""
        print(f"  🔁 {prefix}qwen2API 后台同步已提交: {email}")
        return future

    def flush(self, timeout: Optional[float] = None) -> int:
        """等待当前已提交的后台同步完成，并返回完成数量。"""
        with self._lock:
            futures = list(self._futures)
            self._futures.clear()
        if not futures:
            return 0
        done, pending = wait(futures, timeout=timeout)
        completed = 0
        for future in done:
            completed += 1
            try:
                result = future.result()
            except Exception as exc:  # pragma: no cover - sync_account 已兜底，这里是最后防线。
                print(f"  ⚠️ qwen2API 后台同步异常: {exc}")
                continue
            if result.skipped:
                if result.message:
                    print(f"  ⚠️ {result.message}")
            elif result.ok:
                print(f"  🔁 {result.message}")
            else:
                print(f"  ⚠️ qwen2API 后台同步失败: {result.message}")
        if pending:
            with self._lock:
                self._futures.extend(pending)
            print(f"  ⚠️ qwen2API 后台同步仍有 {len(pending)} 个任务未完成")
        return completed

    def shutdown(self, wait_for_tasks: bool = True) -> None:
        self._executor.shutdown(wait=wait_for_tasks)
