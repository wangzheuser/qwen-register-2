"""TempMail.lol 免配置临时邮箱服务。

TempMail.lol 无需密钥：GET 建箱返回随机域名地址与 token，
列信接口一次性返回邮件全文（含 body / html），无需再取详情。
"""

from __future__ import annotations

import time
from typing import Any, Optional
from urllib.parse import urlencode

import httpx

from .base import EmailCreationError, EmailParseError, EmailProvider, EmailTimeoutError
from .retry import RATE_LIMIT_MAX_RETRIES, delay_to_milliseconds, sleep_rate_limit_retry
from .utils import extract_activation_link, message_matches_keywords, normalize_text


class TempMailLolProvider(EmailProvider):
    provider_name = "TempMail.lol"
    API_BASE_URL = "https://api.tempmail.lol"

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

    def _headers(self) -> dict[str, str]:
        return {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
            "Accept": "application/json",
        }

    def _request(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        """带 429 限流与网络错误重试的请求封装。"""
        url = f"{self.API_BASE_URL}{path}"
        rate_retries = 0
        network_retries = 0
        while True:
            try:
                response = self.client.request(method, url, headers=self._headers(), **kwargs)
            except httpx.HTTPError as exc:
                if network_retries < RATE_LIMIT_MAX_RETRIES:
                    network_retries += 1
                    delay = sleep_rate_limit_retry()
                    self.log_debug(
                        f"TempMail.lol 网络错误，随机 {delay_to_milliseconds(delay)}ms 后重试 "
                        f"({network_retries}/{RATE_LIMIT_MAX_RETRIES}): {exc}"
                    )
                    continue
                raise EmailCreationError(f"TempMail.lol 网络错误: {exc}") from exc

            if response.status_code == 429 and rate_retries < RATE_LIMIT_MAX_RETRIES:
                rate_retries += 1
                delay = sleep_rate_limit_retry()
                self.log_debug(
                    f"TempMail.lol 限流 429，随机 {delay_to_milliseconds(delay)}ms 后重试 "
                    f"({rate_retries}/{RATE_LIMIT_MAX_RETRIES})"
                )
                continue

            try:
                response.raise_for_status()
            except httpx.HTTPStatusError as exc:
                raise EmailCreationError(
                    f"TempMail.lol API 错误 {response.status_code}: {response.text[:200]}"
                ) from exc
            return response

    def create_inbox(self) -> str:
        try:
            response = self._request("GET", "/v2/inbox/create")
            payload = response.json()
            address = str(payload.get("address") or "").strip().lower()
            token = str(payload.get("token") or "").strip()
            if "@" not in address or not token:
                raise EmailCreationError(f"TempMail.lol 建箱响应缺少 address/token: {response.text[:200]}")
            self.email = address
            self.token = token
            self.log_info(f"邮箱生成成功: {self.email} (TempMail.lol)")
            return self.email
        except EmailCreationError:
            raise
        except Exception as exc:
            raise EmailCreationError(f"TempMail.lol 邮箱创建失败: {exc}") from exc

    def get_activation_link(
        self,
        timeout: int = 300,
        keywords: tuple[str, ...] = ("qwen", "alibaba"),
    ) -> str:
        if not self.token:
            raise EmailCreationError("TempMail.lol 邮箱服务尚未创建邮箱")
        deadline = time.time() + timeout
        parse_errors: list[str] = []
        while time.time() < deadline:
            response = self._request("GET", "/v2/inbox?" + urlencode({"token": self.token}))
            payload = response.json()
            if payload.get("expired"):
                raise EmailCreationError("TempMail.lol 邮箱已过期")
            for message in payload.get("emails") or []:
                if not message_matches_keywords(message, keywords):
                    continue
                # 列信接口已返回邮件全文，直接解析，无需取详情。
                text = normalize_text(message.get("body") or message.get("text"))
                html = normalize_text(message.get("html"))
                link = extract_activation_link(html=html, text=text)
                if link:
                    self.log_info("激活链接获取成功")
                    return link
                parse_errors.append(str(message.get("id") or message.get("subject") or "?"))
            time.sleep(8)
        if parse_errors:
            raise EmailParseError(f"TempMail.lol 找到目标邮件但无法解析激活链接: {', '.join(parse_errors)}")
        raise EmailTimeoutError(f"TempMail.lol 等待激活邮件超时 ({timeout}s)")

    def cleanup(self) -> None:
        if self._owns_client:
            self.client.close()
