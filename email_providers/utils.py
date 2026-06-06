"""邮箱服务实现共享工具函数。"""

from __future__ import annotations

import html as html_lib
import re
from typing import Any, Iterable, Optional

from bs4 import BeautifulSoup

EXCLUDED_LINK_PARTS = (
    "unsubscribe",
    "tracking",
    "analytics",
    "googleadservices",
    "doubleclick",
    "utm_source=ad",
)

ACTIVATION_KEYWORDS = ("verify", "activate", "auth", "confirm")
DEFAULT_MAIL_KEYWORDS = ("qwen", "alibaba")


def normalize_text(value: Any) -> str:
    """将 API 返回值转换为可搜索文本。"""
    if value is None:
        return ""
    if isinstance(value, list):
        return "\n".join(normalize_text(item) for item in value)
    if isinstance(value, dict):
        return "\n".join(normalize_text(item) for item in value.values())
    return str(value)


def message_matches_keywords(message: dict[str, Any], keywords: Iterable[str] = DEFAULT_MAIL_KEYWORDS) -> bool:
    """常见邮件元数据包含目标关键词时返回 True。"""
    haystack = "\n".join(
        normalize_text(message.get(field))
        for field in ("subject", "from", "intro", "text", "body")
    ).lower()
    return any(keyword.lower() in haystack for keyword in keywords)


def _normalize_candidate(url: str) -> str:
    return html_lib.unescape(url).strip().rstrip(".,;:)]}\"'")


def _is_activation_link(url: str, *, allow_fallback: bool = False) -> bool:
    lowered = url.lower()
    if "qwen.ai" not in lowered:
        return False
    if any(excluded in lowered for excluded in EXCLUDED_LINK_PARTS):
        return False
    if allow_fallback:
        return True
    return any(keyword in lowered for keyword in ACTIVATION_KEYWORDS)


def extract_activation_link(html: str = "", text: str = "") -> Optional[str]:
    """从邮件 HTML 或文本内容中提取 Qwen 激活链接。"""
    if html:
        soup = BeautifulSoup(html_lib.unescape(html), "html.parser")
        for link in soup.find_all("a", href=True):
            href = _normalize_candidate(link["href"])
            if _is_activation_link(href):
                return href

    content = html_lib.unescape(f"{html}\n{text}")
    activation_patterns = (
        r'https://[^"\s<>]*qwen\.ai[^"\s<>]*(?:verify|activate|auth|confirm)[^"\s<>]*',
        r'https://[^"\s<>]*qwen\.ai[^"\s<>]+',
    )

    for index, pattern in enumerate(activation_patterns):
        allow_fallback = index == 1
        for match in re.finditer(pattern, content, re.IGNORECASE):
            candidate = _normalize_candidate(match.group(0))
            if _is_activation_link(candidate, allow_fallback=allow_fallback):
                return candidate
    return None
