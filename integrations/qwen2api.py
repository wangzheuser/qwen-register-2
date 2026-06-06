"""qwen2API 账号同步集成。"""

from __future__ import annotations

from dataclasses import dataclass
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
