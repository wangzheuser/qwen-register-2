"""Mailporary 同步 API 邮箱服务。"""

from __future__ import annotations

import json
import random
import re
import string
import time
from typing import Any, Callable, Optional

import httpx

from .base import EmailCreationError, EmailParseError, EmailProvider, EmailTimeoutError
from .retry import RATE_LIMIT_MAX_RETRIES, delay_to_milliseconds, sleep_rate_limit_retry
from .utils import extract_activation_link, message_matches_keywords, normalize_text


class MailporaryProvider(EmailProvider):
    provider_name = "Mailporary"
    HOME_URL = "https://mailporary.com/zh"
    API_BASE_URL = "https://web.mailporary.com/api/v1"
    DOMAINS = ("oeralb.com", "sisood.com", "disefl.com")

    def __init__(
        self,
        verbose: bool = False,
        api_proxy: Optional[str] = None,
        context: Optional[Any] = None,
        client: Optional[httpx.Client] = None,
        email_prefix_factory: Optional[Callable[[], str]] = None,
    ) -> None:
        super().__init__(verbose=verbose, api_proxy=api_proxy, context=context)
        self._owns_client = client is None
        self.client = client or self._create_client(api_proxy)
        self.email_prefix_factory = email_prefix_factory or self._random_prefix
        self.token: Optional[str] = None

    @staticmethod
    def _create_client(api_proxy: Optional[str]) -> httpx.Client:
        kwargs: dict[str, Any] = {"timeout": 30.0, "follow_redirects": True}
        if api_proxy:
            kwargs["proxy"] = api_proxy
        return httpx.Client(**kwargs)

    @staticmethod
    def _random_prefix() -> str:
        return "".join(random.choices(string.ascii_lowercase + string.digits, k=8))

    def _extract_token_from_html(self, html: str) -> str:
        match = re.search(r'<script[^>]+id=["\']__NUXT_DATA__["\'][^>]*>([\s\S]*?)</script>', html)
        if not match:
            raise EmailCreationError("Mailporary 页面缺少 __NUXT_DATA__ token 数据")
        try:
            nuxt_data = json.loads(match.group(1))
            root = nuxt_data[1]
            token_index = root["mailServiceToken"]
            token = nuxt_data[token_index]
        except (KeyError, IndexError, TypeError, json.JSONDecodeError) as exc:
            raise EmailCreationError("Mailporary token 解析失败") from exc
        if not isinstance(token, str) or not token:
            raise EmailCreationError("Mailporary token 为空")
        return token

    def _refresh_token(self) -> None:
        self.log_debug("正在获取 Mailporary token")
        rate_retries = 0
        while True:
            try:
                response = self.client.get(self.HOME_URL)
            except httpx.HTTPError as exc:
                raise EmailCreationError(f"Mailporary 首页请求失败: {exc}") from exc

            if response.status_code == 429 and rate_retries < RATE_LIMIT_MAX_RETRIES:
                rate_retries += 1
                delay = sleep_rate_limit_retry()
                self.log_debug(
                    f"Mailporary 首页限流 429，随机 {delay_to_milliseconds(delay)}ms 后重试 "
                    f"({rate_retries}/{RATE_LIMIT_MAX_RETRIES})"
                )
                continue

            try:
                response.raise_for_status()
            except httpx.HTTPError as exc:
                raise EmailCreationError(f"Mailporary 首页请求失败: {exc}") from exc
            break
        self.token = self._extract_token_from_html(response.text)

    def _headers(self) -> dict[str, str]:
        if not self.token:
            self._refresh_token()
        return {"Authorization": f"Bearer {self.token}"}

    def _api_get(
        self,
        path: str,
        *,
        retry_auth: bool = True,
        max_429_retries: int = RATE_LIMIT_MAX_RETRIES,
    ) -> httpx.Response:
        url = f"{self.API_BASE_URL}{path}"
        rate_retries = 0
        unavailable_retried = False
        auth_retried = False
        while True:
            try:
                response = self.client.get(url, headers=self._headers())
            except httpx.TimeoutException as exc:
                raise EmailCreationError(f"Mailporary 网络超时: {exc}") from exc
            except httpx.HTTPError as exc:
                raise EmailCreationError(f"Mailporary 网络错误: {exc}") from exc

            if response.status_code == 401 and retry_auth and not auth_retried:
                auth_retried = True
                self.log_debug("Mailporary token 失效，刷新 token 后重试")
                self._refresh_token()
                continue
            if response.status_code == 429 and rate_retries < max_429_retries:
                rate_retries += 1
                delay = sleep_rate_limit_retry()
                self.log_debug(
                    f"Mailporary 限流 429，随机 {delay_to_milliseconds(delay)}ms 后重试 "
                    f"({rate_retries}/{max_429_retries})"
                )
                continue
            if response.status_code == 503 and not unavailable_retried:
                unavailable_retried = True
                self.log_debug("Mailporary 服务不可用 503，5s 后重试")
                time.sleep(5)
                continue

            try:
                response.raise_for_status()
            except httpx.HTTPStatusError as exc:
                raise EmailCreationError(f"Mailporary API 错误 {response.status_code}: {response.text[:200]}") from exc
            return response

    def create_inbox(self) -> str:
        try:
            self._refresh_token()
            domain = self.DOMAINS[0]
            self.email = f"{self.email_prefix_factory()}@{domain}"
            self._api_get(f"/mailbox/{self.email}")
            self.log_info(f"邮箱生成成功: {self.email} (Mailporary)")
            return self.email
        except EmailCreationError:
            raise
        except Exception as exc:
            raise EmailCreationError(f"Mailporary 邮箱创建失败: {exc}") from exc

    def get_activation_link(
        self,
        timeout: int = 300,
        keywords: tuple[str, ...] = ("qwen", "alibaba"),
    ) -> str:
        if not self.email:
            raise EmailCreationError("Mailporary 邮箱服务尚未创建邮箱")
        deadline = time.time() + timeout
        parse_errors: list[str] = []
        while time.time() < deadline:
            messages = self._api_get(f"/mailbox/{self.email}").json().get("messages", [])
            for message in messages:
                if not message_matches_keywords(message, keywords):
                    continue
                message_id = message.get("id")
                if not message_id:
                    continue
                detail = self._api_get(f"/mailbox/{self.email}/{message_id}").json()
                body = detail.get("body") or {}
                text = normalize_text(body.get("text") or detail.get("text") or detail.get("intro"))
                html = normalize_text(body.get("html") or detail.get("html"))
                link = extract_activation_link(html=html, text=text)
                if link:
                    self.log_info("激活链接获取成功")
                    return link
                parse_errors.append(str(message_id))
            time.sleep(8)
        if parse_errors:
            raise EmailParseError(f"Mailporary 找到目标邮件但无法解析激活链接: {', '.join(parse_errors)}")
        raise EmailTimeoutError(f"Mailporary 等待激活邮件超时 ({timeout}s)")

    def cleanup(self) -> None:
        if self._owns_client:
            self.client.close()
