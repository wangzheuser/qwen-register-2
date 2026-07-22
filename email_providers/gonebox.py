"""GoneBox 免配置临时邮箱服务。

GoneBox 无需任何密钥即可创建邮箱：POST 建箱直接返回固定域名地址，
列信 / 取信均以邮箱地址为路径参数，非常适合作为零配置渠道。
"""

from __future__ import annotations

import time
from typing import Any, Optional
from urllib.parse import quote

import httpx

from .base import EmailCreationError, EmailParseError, EmailProvider, EmailTimeoutError
from .retry import RATE_LIMIT_MAX_RETRIES, delay_to_milliseconds, sleep_rate_limit_retry
from .utils import extract_activation_link, message_matches_keywords, normalize_text


class GoneBoxProvider(EmailProvider):
    provider_name = "GoneBox"
    API_BASE_URL = "https://api.gonebox.email/api/v1"
    DOMAIN = "gonebox.email"
    _REFERER = "https://gonebox.email/"

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

    @staticmethod
    def _create_client(api_proxy: Optional[str]) -> httpx.Client:
        kwargs: dict[str, Any] = {"timeout": 30.0}
        if api_proxy:
            kwargs["proxy"] = api_proxy
        return httpx.Client(**kwargs)

    def _headers(self) -> dict[str, str]:
        return {"Referer": self._REFERER, "Accept": "application/json"}

    def _request(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        """带 429 限流与网络错误重试的请求封装，风格对齐 MailtmProvider。"""
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
                        f"GoneBox 网络错误，随机 {delay_to_milliseconds(delay)}ms 后重试 "
                        f"({network_retries}/{RATE_LIMIT_MAX_RETRIES}): {exc}"
                    )
                    continue
                raise EmailCreationError(f"GoneBox 网络错误: {exc}") from exc

            if response.status_code == 429 and rate_retries < RATE_LIMIT_MAX_RETRIES:
                rate_retries += 1
                delay = sleep_rate_limit_retry()
                self.log_debug(
                    f"GoneBox 限流 429，随机 {delay_to_milliseconds(delay)}ms 后重试 "
                    f"({rate_retries}/{RATE_LIMIT_MAX_RETRIES})"
                )
                continue

            try:
                response.raise_for_status()
            except httpx.HTTPStatusError as exc:
                raise EmailCreationError(f"GoneBox API 错误 {response.status_code}: {response.text[:200]}") from exc
            return response

    def create_inbox(self) -> str:
        try:
            response = self._request("POST", "/inboxes", json={"domain": self.DOMAIN})
            data = response.json().get("data") or {}
            address = str(data.get("address") or "").strip().lower()
            if "@" not in address:
                raise EmailCreationError(f"GoneBox 未返回邮箱地址: {response.text[:200]}")
            self.email = address
            self.log_info(f"邮箱生成成功: {self.email} (GoneBox)")
            return self.email
        except EmailCreationError:
            raise
        except Exception as exc:
            raise EmailCreationError(f"GoneBox 邮箱创建失败: {exc}") from exc

    def _list_messages(self) -> list[dict[str, Any]]:
        response = self._request("GET", f"/inboxes/{quote(self.email)}/messages")
        data = response.json().get("data") or {}
        messages = data.get("messages")
        return messages if isinstance(messages, list) else []

    def _get_message(self, message_id: str) -> dict[str, Any]:
        response = self._request("GET", f"/messages/{quote(str(message_id))}")
        try:
            payload = response.json()
        except ValueError:
            return {"text": response.text, "html": response.text}
        # 详情可能整体包在 data 里，也可能直接是字段。
        return payload.get("data") if isinstance(payload.get("data"), dict) else payload

    def get_activation_link(
        self,
        timeout: int = 300,
        keywords: tuple[str, ...] = ("qwen", "alibaba"),
    ) -> str:
        if not self.email:
            raise EmailCreationError("GoneBox 邮箱服务尚未创建邮箱")
        deadline = time.time() + timeout
        parse_errors: list[str] = []
        while time.time() < deadline:
            for message in self._list_messages():
                if not message_matches_keywords(message, keywords):
                    continue
                message_id = message.get("id")
                if not message_id:
                    continue
                detail = self._get_message(message_id)
                body = detail.get("body") if isinstance(detail.get("body"), dict) else {}
                # GoneBox 详情端点的正文字段是 bodyHtml/bodyText（驼峰），不是
                # body.html/html/text；缺了它俩会导致"找到 qwen 邮件但无法解析激活链接"。
                text = normalize_text(
                    detail.get("bodyText") or body.get("text") or detail.get("text") or detail.get("intro")
                )
                html = normalize_text(
                    detail.get("bodyHtml") or body.get("html") or detail.get("html")
                )
                link = extract_activation_link(html=html, text=text)
                if link:
                    self.log_info("激活链接获取成功")
                    return link
                parse_errors.append(str(message_id))
            time.sleep(8)
        if parse_errors:
            raise EmailParseError(f"GoneBox 找到目标邮件但无法解析激活链接: {', '.join(parse_errors)}")
        raise EmailTimeoutError(f"GoneBox 等待激活邮件超时 ({timeout}s)")

    def cleanup(self) -> None:
        if self._owns_client:
            self.client.close()
