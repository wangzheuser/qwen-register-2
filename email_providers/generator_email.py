"""基于 Playwright 页面交互的 Generator.email 邮箱服务。"""

from __future__ import annotations

import random
import string
import time
from typing import Any, Optional

from .base import EmailCreationError, EmailParseError, EmailProvider, EmailTimeoutError
from .utils import extract_activation_link


class GeneratorEmailProvider(EmailProvider):
    provider_name = "Generator.email"
    BASE_URL = "https://generator.email"
    DOMAIN = "halyang.my.id"

    def __init__(self, verbose: bool = False, api_proxy: Optional[str] = None, context: Optional[Any] = None) -> None:
        super().__init__(verbose=verbose, api_proxy=None, context=context)
        if context is None:
            raise ValueError("generator.email 邮箱服务需要传入 BrowserContext")
        self.inbox_page: Optional[Any] = None

    @staticmethod
    def _random_prefix() -> str:
        letters = "".join(random.choices(string.ascii_lowercase, k=6))
        digits = "".join(random.choices(string.digits, k=3))
        return f"{letters}{digits}"

    def create_inbox(self) -> str:
        try:
            self.email = f"{self._random_prefix()}@{self.DOMAIN}"
            self.inbox_page = self.context.new_page()
            url = f"{self.BASE_URL}/{self.email}"
            self.log_debug(f"打开 generator.email 收件箱: {url}")
            self.inbox_page.goto(url, wait_until="domcontentloaded", timeout=60000)
            time.sleep(6)
            self.log_info(f"邮箱生成成功: {self.email} (Generator.email)")
            return self.email
        except Exception as exc:
            raise EmailCreationError(f"Generator.email 邮箱创建失败: {exc}") from exc

    def get_activation_link(
        self,
        timeout: int = 300,
        keywords: tuple[str, ...] = ("qwen", "alibaba"),
    ) -> str:
        if self.inbox_page is None:
            raise EmailCreationError("Generator.email 邮箱服务尚未创建邮箱")

        self.log_debug(f"等待验证邮件，超时时间 {timeout}s")
        deadline = time.time() + timeout
        last_count = 0
        saw_target_email = False

        while time.time() < deadline:
            try:
                try:
                    self.inbox_page.evaluate("""
                        [...document.querySelectorAll('a, button, span, div')]
                          .find(el => el.innerText && el.innerText.trim() === 'Refresh')
                          ?.click()
                    """)
                except Exception:
                    pass

                time.sleep(3)
                items = self.inbox_page.evaluate("""
                    [...document.querySelectorAll('div.e7m.list-group-item')]
                      .filter(el => !el.className.includes('active'))
                      .map(el => ({ text: el.innerText.trim(), html: el.outerHTML }))
                      .filter(e => e.text.length > 5)
                """) or []

                if len(items) != last_count:
                    self.log_debug(f"收件箱邮件数量: {len(items)}")
                    last_count = len(items)

                for item in items:
                    lower = item.get("text", "").lower()
                    if not any(keyword.lower() in lower for keyword in keywords):
                        continue
                    saw_target_email = True
                    self.log_debug(f"发现目标邮件: {item.get('text', '')[:80]}")
                    try:
                        self.inbox_page.evaluate(f"""
                            const html = {item['html']!r};
                            const items = [...document.querySelectorAll('div.e7m.list-group-item')];
                            const target = items.find(el => el.outerHTML === html);
                            if (target) target.click();
                        """)
                        time.sleep(6)
                    except Exception as exc:
                        self.log_debug(f"点击邮件失败，继续尝试提取链接: {exc}")
                        time.sleep(4)

                    links = self.inbox_page.evaluate("""
                        [...document.querySelectorAll('a[href]')]
                          .map(a => a.href)
                          .filter(h => h.startsWith('http'))
                    """) or []
                    for link in links:
                        activation_link = extract_activation_link(text=link)
                        if activation_link:
                            self.log_info("激活链接获取成功")
                            return activation_link
            except Exception as exc:
                self.log_debug(f"收件箱检查失败: {exc}")
            time.sleep(8)

        if saw_target_email:
            raise EmailParseError("Generator.email 找到目标邮件但无法解析激活链接")
        raise EmailTimeoutError(f"Generator.email 等待激活邮件超时 ({timeout}s)")

    def cleanup(self) -> None:
        if self.inbox_page is not None:
            try:
                self.inbox_page.close()
            except Exception:
                pass
            self.inbox_page = None
