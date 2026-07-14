"""FreeCustom (freecustom.email) 免配置临时邮箱服务。

FreeCustom 无需密钥：POST /api/auth 拿匿名 token，再从 /api/domains 动态获取
当前存活的免费域名，本地拼邮箱地址，首次读取 /api/public-mailbox 即建立收件箱。

注意：FreeCustom 的免费域名会定期过期（stagewise 里硬编码的 ditpay.info 等已过期），
因此这里**动态过滤存活域名**，不硬编码任何单个域名，避免随域名更替而失效。
"""

from __future__ import annotations

import re
import secrets
import time
from typing import Any, Optional
from urllib.parse import urlencode

import httpx

from .base import EmailCreationError, EmailParseError, EmailProvider, EmailTimeoutError
from .retry import RATE_LIMIT_MAX_RETRIES, delay_to_milliseconds, sleep_rate_limit_retry
from .utils import extract_activation_link, message_matches_keywords, normalize_text


class FreeCustomProvider(EmailProvider):
    provider_name = "FreeCustom"
    BASE_URL = "https://www.freecustom.email"
    _REFERER = "https://www.freecustom.email/en"
    # 上游全部域名过期时的兜底候选，仍会先尝试在线拉取。
    FALLBACK_DOMAINS = ("sqlcompiler.info", "addmy.space", "nimbusreach.info")

    def __init__(
        self,
        verbose: bool = False,
        api_proxy: Optional[str] = None,
        context: Optional[Any] = None,
        client: Optional[httpx.Client] = None,
    ) -> None:
        super().__init__(verbose=verbose, api_proxy=api_proxy, context=context)
        self._owns_client = client is None
        self.client = client or self._create_client(api_proxy)
        self.token: Optional[str] = None

    @staticmethod
    def _create_client(api_proxy: Optional[str]) -> httpx.Client:
        kwargs: dict[str, Any] = {"timeout": 30.0}
        if api_proxy:
            kwargs["proxy"] = api_proxy
        return httpx.Client(**kwargs)

    def _headers(self, auth: bool = False) -> dict[str, str]:
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
            "Accept": "application/json",
            "Referer": self._REFERER,
            "x-fce-client": "web-client",
        }
        if auth and self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        return headers

    def _request(self, method: str, path: str, *, auth: bool = False, **kwargs: Any) -> httpx.Response:
        """带 429 限流与网络错误重试的请求封装。"""
        url = f"{self.BASE_URL}{path}"
        rate_retries = 0
        network_retries = 0
        while True:
            try:
                response = self.client.request(method, url, headers=self._headers(auth=auth), **kwargs)
            except httpx.HTTPError as exc:
                if network_retries < RATE_LIMIT_MAX_RETRIES:
                    network_retries += 1
                    delay = sleep_rate_limit_retry()
                    self.log_debug(
                        f"FreeCustom 网络错误，随机 {delay_to_milliseconds(delay)}ms 后重试 "
                        f"({network_retries}/{RATE_LIMIT_MAX_RETRIES}): {exc}"
                    )
                    continue
                raise EmailCreationError(f"FreeCustom 网络错误: {exc}") from exc

            if response.status_code == 429 and rate_retries < RATE_LIMIT_MAX_RETRIES:
                rate_retries += 1
                delay = sleep_rate_limit_retry()
                self.log_debug(
                    f"FreeCustom 限流 429，随机 {delay_to_milliseconds(delay)}ms 后重试 "
                    f"({rate_retries}/{RATE_LIMIT_MAX_RETRIES})"
                )
                continue

            try:
                response.raise_for_status()
            except httpx.HTTPStatusError as exc:
                raise EmailCreationError(f"FreeCustom API 错误 {response.status_code}: {response.text[:200]}") from exc
            return response

    def _ensure_token(self) -> str:
        if self.token:
            return self.token
        response = self._request("POST", "/api/auth")
        token = str(response.json().get("token") or "").strip()
        if not token:
            raise EmailCreationError("FreeCustom auth 响应缺少 token")
        self.token = token
        return token

    def _live_domains(self) -> list[str]:
        """动态拉取存活免费域名，过滤已过期的（expires_in_days <= 0）。"""
        try:
            response = self._request("GET", "/api/domains", auth=True)
            domains = []
            for item in response.json().get("data") or []:
                if not isinstance(item, dict):
                    continue
                domain = str(item.get("domain") or "").strip().lower()
                if not domain or str(item.get("tier") or "").lower() not in ("", "free"):
                    continue
                expires_in = item.get("expires_in_days")
                # None 表示长期有效；正数表示未过期。
                if expires_in is None or (isinstance(expires_in, (int, float)) and expires_in > 0):
                    domains.append(domain)
            if domains:
                return domains
        except EmailCreationError as exc:
            self.log_debug(f"FreeCustom 拉取域名失败，改用兜底域名: {exc}")
        return list(self.FALLBACK_DOMAINS)

    def create_inbox(self) -> str:
        try:
            self._ensure_token()
            domain = self._live_domains()[0]
            local = "oc" + secrets.token_hex(5)
            local = re.sub(r"[^a-z0-9._-]+", "", local.lower()).strip("._-")
            self.email = f"{local}@{domain}"
            # 首次读取以初始化收件箱。
            self._list_messages()
            self.log_info(f"邮箱生成成功: {self.email} (FreeCustom)")
            return self.email
        except EmailCreationError:
            raise
        except Exception as exc:
            raise EmailCreationError(f"FreeCustom 邮箱创建失败: {exc}") from exc

    def _list_messages(self) -> list[dict[str, Any]]:
        query = urlencode({"fullMailboxId": self.email})
        response = self._request("GET", f"/api/public-mailbox?{query}", auth=True)
        data = response.json().get("data")
        return [item for item in data if isinstance(item, dict)] if isinstance(data, list) else []

    def _get_message(self, message_id: str) -> dict[str, Any]:
        query = urlencode({"fullMailboxId": self.email, "messageId": str(message_id)})
        response = self._request("GET", f"/api/public-mailbox?{query}", auth=True)
        data = response.json().get("data")
        return data if isinstance(data, dict) else {}

    def get_activation_link(
        self,
        timeout: int = 300,
        keywords: tuple[str, ...] = ("qwen", "alibaba"),
    ) -> str:
        if not self.email:
            raise EmailCreationError("FreeCustom 邮箱服务尚未创建邮箱")
        deadline = time.time() + timeout
        parse_errors: list[str] = []
        while time.time() < deadline:
            for message in self._list_messages():
                if not message_matches_keywords(message, keywords):
                    continue
                message_id = message.get("id") or message.get("_id")
                if not message_id:
                    continue
                detail = self._get_message(message_id)
                text = normalize_text(detail.get("text") or detail.get("intro") or message.get("text"))
                html = normalize_text(detail.get("html") or detail.get("body"))
                link = extract_activation_link(html=html, text=text)
                if link:
                    self.log_info("激活链接获取成功")
                    return link
                parse_errors.append(str(message_id))
            time.sleep(8)
        if parse_errors:
            raise EmailParseError(f"FreeCustom 找到目标邮件但无法解析激活链接: {', '.join(parse_errors)}")
        raise EmailTimeoutError(f"FreeCustom 等待激活邮件超时 ({timeout}s)")

    def cleanup(self) -> None:
        if self._owns_client:
            self.client.close()
