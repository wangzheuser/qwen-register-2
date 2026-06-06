"""临时邮箱服务的基类和异常定义。"""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from typing import Any, Optional


class EmailProviderError(Exception):
    """邮箱服务失败的基础异常。"""


class EmailCreationError(EmailProviderError):
    """临时邮箱无法创建或激活时抛出。"""


class EmailTimeoutError(EmailProviderError):
    """验证邮件在超时前未到达时抛出。"""


class EmailParseError(EmailProviderError):
    """找到目标邮件但无法解析激活链接时抛出。"""


class EmailProvider(ABC):
    """同步临时邮箱服务接口。"""

    provider_name = "EmailProvider"

    def __init__(
        self,
        verbose: bool = False,
        api_proxy: Optional[str] = None,
        context: Optional[Any] = None,
    ) -> None:
        self.verbose = verbose
        self.api_proxy = api_proxy
        self.context = context
        self.email: Optional[str] = None
        self.logger = self._setup_logger()

    def _setup_logger(self) -> logging.Logger:
        logger = logging.getLogger(f"email_providers.{self.__class__.__name__}")
        logger.setLevel(logging.DEBUG if self.verbose else logging.INFO)
        if not logger.handlers:
            handler = logging.StreamHandler()
            handler.setFormatter(logging.Formatter("%(message)s"))
            logger.addHandler(handler)
        logger.propagate = False
        return logger

    def log_info(self, message: str) -> None:
        self.logger.info("✓ %s", message)

    def log_debug(self, message: str) -> None:
        if self.verbose:
            self.logger.debug("→ %s", message)

    def log_error(self, message: str) -> None:
        self.logger.error("✗ %s", message)

    @abstractmethod
    def create_inbox(self) -> str:
        """创建或激活临时收件箱并返回邮箱地址。"""

    @abstractmethod
    def get_activation_link(
        self,
        timeout: int = 300,
        keywords: tuple[str, ...] = ("qwen", "alibaba"),
    ) -> str:
        """等待 Qwen 激活邮件并返回激活链接。"""

    @abstractmethod
    def cleanup(self) -> None:
        """Release provider-owned resources."""
