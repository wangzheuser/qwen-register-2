"""Mail.tm 同步 API 邮箱服务。"""

from __future__ import annotations

import secrets
import threading
import time
from typing import Any, Callable, Optional

import httpx

from .base import EmailCreationError, EmailParseError, EmailProvider, EmailTimeoutError
from .retry import RATE_LIMIT_MAX_RETRIES, delay_to_milliseconds, sleep_rate_limit_retry
from .utils import extract_activation_link, message_matches_keywords, normalize_text


class MailtmProvider(EmailProvider):
    provider_name = "Mail.tm"
    BASE_URL = "https://api.mail.tm"
    _domains_cache: list[str] = []
    _domains_cache_time: float = 0.0
    _domains_cache_ttl_seconds: float = 300.0
    _domains_cache_lock = threading.Lock()

    def __init__(
        self,
        verbose: bool = False,
        api_proxy: Optional[str] = None,
        context: Optional[Any] = None,
        client: Optional[httpx.Client] = None,
        email_factory: Optional[Callable[[str], str]] = None,
    ) -> None:
        super().__init__(verbose=verbose, api_proxy=api_proxy, context=context)
        self._owns_client = client is None
        self.client = client or self._create_client(api_proxy)
        self.email_factory = email_factory or (lambda domain: f"oc{secrets.token_hex(5)}@{domain}")
        self.password: Optional[str] = None
        self.token: Optional[str] = None

    @staticmethod
    def _create_client(api_proxy: Optional[str]) -> httpx.Client:
        kwargs: dict[str, Any] = {"timeout": 30.0}
        if api_proxy:
            kwargs["proxy"] = api_proxy
        return httpx.Client(**kwargs)

    def _url(self, path: str) -> str:
        return f"{self.BASE_URL}{path}"

    def _headers(self) -> dict[str, str]:
        if not self.token:
            return {}
        return {"Authorization": f"Bearer {self.token}"}

    def _request(
        self,
        method: str,
        path: str,
        *,
        max_429_retries: int = RATE_LIMIT_MAX_RETRIES,
        max_503_retries: int = 2,
        retry_auth: bool = True,
        **kwargs: Any,
    ) -> httpx.Response:
        rate_retries = 0
        unavailable_retries = 0
        network_retries = 0
        auth_retried = False
        while True:
            try:
                response = self.client.request(method, self._url(path), **kwargs)
            except httpx.TimeoutException as exc:
                if network_retries < max_429_retries:
                    network_retries += 1
                    delay = sleep_rate_limit_retry()
                    self.log_debug(
                        f"Mail.tm 网络超时，随机 {delay_to_milliseconds(delay)}ms 后重试 "
                        f"({network_retries}/{max_429_retries}): {exc}"
                    )
                    continue
                raise EmailCreationError(f"Mail.tm 网络超时: {exc}") from exc
            except httpx.HTTPError as exc:
                if network_retries < max_429_retries:
                    network_retries += 1
                    delay = sleep_rate_limit_retry()
                    self.log_debug(
                        f"Mail.tm 网络错误，随机 {delay_to_milliseconds(delay)}ms 后重试 "
                        f"({network_retries}/{max_429_retries}): {exc}"
                    )
                    continue
                raise EmailCreationError(f"Mail.tm 网络错误: {exc}") from exc

            if response.status_code == 429 and rate_retries < max_429_retries:
                rate_retries += 1
                delay = sleep_rate_limit_retry()
                self.log_debug(
                    f"Mail.tm 限流 429，随机 {delay_to_milliseconds(delay)}ms 后重试 "
                    f"({rate_retries}/{max_429_retries})"
                )
                continue
            if response.status_code == 503 and unavailable_retries < max_503_retries:
                unavailable_retries += 1
                self.log_debug(f"Mail.tm 服务不可用 503，5s 后重试 ({unavailable_retries}/{max_503_retries})")
                time.sleep(5)
                continue
            if response.status_code == 401 and retry_auth and not auth_retried and self.email and self.password:
                auth_retried = True
                self.log_debug("Mail.tm token 失效，重新获取 token")
                self._authenticate()
                headers = dict(kwargs.get("headers") or {})
                headers.update(self._headers())
                kwargs["headers"] = headers
                continue

            try:
                response.raise_for_status()
            except httpx.HTTPStatusError as exc:
                raise EmailCreationError(f"Mail.tm API 错误 {response.status_code}: {response.text[:200]}") from exc
            return response

    def _active_domains(self) -> list[str]:
        if self._owns_client:
            now = time.monotonic()
            with self._domains_cache_lock:
                if (
                    self._domains_cache
                    and now - self._domains_cache_time < self._domains_cache_ttl_seconds
                ):
                    self.log_debug("复用 Mail.tm 可用域名缓存")
                    return list(self._domains_cache)
        self.log_debug("正在获取 Mail.tm 可用域名")
        response = self._request("GET", "/domains")
        domains = [
            item["domain"]
            for item in response.json().get("hydra:member", [])
            if item.get("isActive") and item.get("domain")
        ]
        if not domains:
            raise EmailCreationError("Mail.tm 未返回可用域名")
        if self._owns_client:
            with self._domains_cache_lock:
                self.__class__._domains_cache = list(domains)
                self.__class__._domains_cache_time = time.monotonic()
        return domains

    def _authenticate(self) -> None:
        if not self.email or not self.password:
            raise EmailCreationError("Mail.tm 缺少邮箱或密码，无法获取 token")
        response = self._request(
            "POST",
            "/token",
            retry_auth=False,
            json={"address": self.email, "password": self.password},
        )
        token = response.json().get("token")
        if not token:
            raise EmailCreationError("Mail.tm token 响应缺少 token 字段")
        self.token = token

    def create_inbox(self) -> str:
        try:
            domain = self._active_domains()[0]
            self.email = self.email_factory(domain)
            self.password = secrets.token_urlsafe(12)
            self.log_debug(f"正在创建 Mail.tm 账户: {self.email}")
            self._request(
                "POST",
                "/accounts",
                json={"address": self.email, "password": self.password},
            )
            self._authenticate()
            self.log_info(f"邮箱生成成功: {self.email} (Mail.tm)")
            return self.email
        except EmailCreationError:
            raise
        except Exception as exc:
            raise EmailCreationError(f"Mail.tm 邮箱创建失败: {exc}") from exc

    def get_activation_link(
        self,
        timeout: int = 300,
        keywords: tuple[str, ...] = ("qwen", "alibaba"),
    ) -> str:
        if not self.token:
            raise EmailCreationError("Mail.tm 邮箱服务尚未创建邮箱")
        deadline = time.time() + timeout
        parse_errors: list[str] = []
        while time.time() < deadline:
            response = self._request("GET", "/messages", headers=self._headers())
            messages = response.json().get("hydra:member", [])
            for message in messages:
                if not message_matches_keywords(message, keywords):
                    continue
                message_id = message.get("id")
                if not message_id:
                    continue
                detail = self._request("GET", f"/messages/{message_id}", headers=self._headers()).json()
                text = normalize_text(detail.get("text") or detail.get("intro"))
                html = normalize_text(detail.get("html"))
                link = extract_activation_link(html=html, text=text)
                if link:
                    self.log_info("激活链接获取成功")
                    return link
                parse_errors.append(str(message_id))
            time.sleep(8)
        if parse_errors:
            raise EmailParseError(f"Mail.tm 找到目标邮件但无法解析激活链接: {', '.join(parse_errors)}")
        raise EmailTimeoutError(f"Mail.tm 等待激活邮件超时 ({timeout}s)")

    def cleanup(self) -> None:
        if self._owns_client:
            self.client.close()
