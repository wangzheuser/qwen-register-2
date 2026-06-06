"""邮箱服务工厂。"""

from __future__ import annotations

from typing import Any, Optional

from .base import EmailProvider
from .generator_email import GeneratorEmailProvider
from .mailporary import MailporaryProvider
from .mailtm import MailtmProvider


class EmailProviderFactory:
    _PROVIDERS = {
        "mailtm": MailtmProvider,
        "mail.tm": MailtmProvider,
        "mailporary": MailporaryProvider,
        "generator.email": GeneratorEmailProvider,
        "generator": GeneratorEmailProvider,
    }

    @staticmethod
    def create(
        provider_type: str,
        verbose: bool = False,
        api_proxy: Optional[str] = None,
        context: Optional[Any] = None,
    ) -> EmailProvider:
        normalized_type = provider_type.lower().strip()
        provider_cls = EmailProviderFactory._PROVIDERS.get(normalized_type)
        if provider_cls is None:
            allowed = ", ".join(sorted(EmailProviderFactory._PROVIDERS))
            raise ValueError(f"不支持的邮箱服务: {provider_type}; 可选值: {allowed}")
        if provider_cls is GeneratorEmailProvider and context is None:
            raise ValueError("generator.email 邮箱服务需要传入 BrowserContext")
        return provider_cls(
            verbose=verbose,
            api_proxy=api_proxy if provider_cls is not GeneratorEmailProvider else None,
            context=context if provider_cls is GeneratorEmailProvider else None,
        )
