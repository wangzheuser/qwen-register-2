# Qwen 自动注册脚本 - 多邮箱渠道集成设计

**日期**: 2026-06-06  
**作者**: wangqiupei  
**版本**: v1.3（同步适配当前 Playwright sync 主流程）

---

## 1. 概述

### 1.1 背景

当前 `qwenv4.py` 使用 `generator.email` 作为唯一的邮箱服务，通过浏览器页面交互获取验证邮件。为提升注册成功率和灵活性，需要集成多个邮箱服务渠道，并支持通过配置选择使用的服务。

### 1.2 目标

1. 集成 **Mail.tm** 和 **Mailporary** 两个 API 邮箱服务（第一梯队）
2. 保留现有的 **generator.email** 渠道
3. 通过命令行参数选择邮箱服务
4. 实现严格模式：失败时跳过当前账号，不自动降级到其他邮箱渠道
5. 支持独立的 API 代理配置
6. 实现邮箱去重的持久化存储
7. 提供可配置的日志详细程度
8. 保持良好的代码可维护性和可扩展性

### 1.3 设计原则

- **SOLID 原则**：接口驱动、职责单一、对扩展开放
- **KISS 原则**：保持实现简单，避免过度设计
- **DRY 原则**：公共逻辑提取到工具函数
- **YAGNI 原则**：只实现当前需要的功能
- **显式选择**：用户选择的邮箱服务失败时只跳过当前账号，不做隐式 fallback
- **同步适配**：当前 `qwenv4.py` 使用 `playwright.sync_api`，本期 provider 接口采用同步方法，不做全量 async 迁移

---

## 2. 技术调研

### 2.1 Mail.tm API 调研

**API 基础信息**：

- 基础 URL: `https://api.mail.tm`
- 认证方式: Bearer Token
- 数据格式: JSON
- 费用: 免费公开 API

**完整调用流程示例**：

```python
import httpx
import secrets

# 1. 获取可用域名
response = httpx.get("https://api.mail.tm/domains")
response.raise_for_status()
domains = [d["domain"] for d in response.json()["hydra:member"] if d.get("isActive")]
# 返回: ["example.com", "example2.com", ...]

# 2. 创建账户
email = f"oc{secrets.token_hex(5)}@{domains[0]}"
password = secrets.token_urlsafe(12)
response = httpx.post(
    "https://api.mail.tm/accounts",
    json={"address": email, "password": password},
)
response.raise_for_status()
# 返回: {"id": "...", "address": "oc7f2a3b4e@example.com", ...}

# 3. 获取认证 Token
response = httpx.post(
    "https://api.mail.tm/token",
    json={"address": email, "password": password},
)
response.raise_for_status()
token = response.json()["token"]
# 返回: {"token": "eyJhbGci...", "id": "..."}

# 4. 轮询邮件列表
response = httpx.get(
    "https://api.mail.tm/messages",
    headers={"Authorization": f"Bearer {token}"},
)
response.raise_for_status()
messages = response.json()["hydra:member"]
# 返回: [{"id": "msg1", "subject": "Welcome to Qwen", "from": {...}, ...}]

# 5. 获取邮件详情
message_id = messages[0]["id"]
response = httpx.get(
    f"https://api.mail.tm/messages/{message_id}",
    headers={"Authorization": f"Bearer {token}"},
)
response.raise_for_status()
detail = response.json()
# 返回: {
#   "id": "msg1",
#   "from": {"address": "no-reply@qwen.ai", "name": "Qwen Studio"},
#   "subject": "Verify your email",
#   "intro": "Welcome to Qwen Studio...",
#   "text": "Click here: https://studio.qwen.ai/auth/verify?token=xxx",
#   "html": ["<html>...</html>"]  # 数组格式
# }
```

**关键点**：

- 邮件内容在 `text` 和 `html` 字段
- `html` 是字符串数组，需要 `"".join(html)` 后解析
- Token 在运行期间保存在 provider 实例中，不要求跨运行持久化

---

### 2.2 Mailporary API 调研

**API 基础信息**：

- 基础 URL: `https://web.mailporary.com/api/v1`
- 认证方式: Bearer Token（从页面 `__NUXT_DATA__` 提取）
- 数据格式: JSON
- 费用: 免费服务

**完整调用流程示例**：

```python
import json
import random
import re
import string
import httpx

# 1. 从首页提取 Token
response = httpx.get("https://mailporary.com/zh")
response.raise_for_status()
html = response.text

# 提取 __NUXT_DATA__ 脚本
match = re.search(r'<script[^>]+id="__NUXT_DATA__"[^>]*>([\s\S]*?)</script>', html)
if not match:
    raise RuntimeError("无法定位 __NUXT_DATA__")

nuxt_data = json.loads(match.group(1))

# 解析 Token（从数组中定位）
root = nuxt_data[1]
token_index = root["mailServiceToken"]
token = nuxt_data[token_index]
# 返回: "eyJhbGci..."

# 2. 生成邮箱地址并激活
email = f"{''.join(random.choices(string.ascii_lowercase, k=6))}@oeralb.com"
response = httpx.get(
    f"https://web.mailporary.com/api/v1/mailbox/{email}",
    headers={"Authorization": f"Bearer {token}"},
)
response.raise_for_status()
# 返回: {"messages": [], ...}  # 空列表表示邮箱已激活

# 3. 轮询邮件列表
response = httpx.get(
    f"https://web.mailporary.com/api/v1/mailbox/{email}",
    headers={"Authorization": f"Bearer {token}"},
)
response.raise_for_status()
messages = response.json().get("messages", [])
# 返回: [{"id": "...", "from": {...}, "subject": "...", ...}]

# 4. 获取邮件详情
message_id = messages[0]["id"]
response = httpx.get(
    f"https://web.mailporary.com/api/v1/mailbox/{email}/{message_id}",
    headers={"Authorization": f"Bearer {token}"},
)
response.raise_for_status()
detail = response.json()
# 返回: {
#   "from": {"address": "no-reply@qwen.ai"},
#   "subject": "Verify your email",
#   "body": {
#     "text": "Click here: https://studio.qwen.ai/auth/verify?token=xxx",
#     "html": "<html>...</html>"
#   },
#   "intro": "Welcome..."
# }
```

**关键点**：

- Token 需从页面提取，页面结构变化时要抛出 `EmailCreationError`
- 内置域名示例：`['oeralb.com', 'sisood.com', 'disefl.com']`
- 邮件内容在 `body.text` 和 `body.html`
- `401` 可能意味着页面 token 失效，应优先刷新 token 后重试一次

---

### 2.3 激活链接格式分析

**Qwen 激活邮件链接格式**：

```text
https://studio.qwen.ai/auth/verify?token=<JWT_TOKEN>
```

**提取策略**：

1. 从 HTML 解析 `<a>` 标签，优先匹配 `qwen.ai` 域名并包含 `verify`、`activate` 或 `auth` 的链接
2. 正则匹配包含 `qwen.ai` 且路径中含激活关键词的完整 URL
3. 兜底匹配任意 `https://` 开头的 `qwen.ai` 链接
4. 排除退订、追踪、广告等非激活链接
5. 对 HTML 实体做反转义，并裁剪 URL 尾部标点

```python
import html as html_lib
import re
from typing import Optional
from bs4 import BeautifulSoup

EXCLUDED_LINK_PARTS = (
    "unsubscribe",
    "tracking",
    "analytics",
    "googleadservices",
    "doubleclick",
    "utm_source=ad",
)

ACTIVATION_KEYWORDS = ("verify", "activate", "auth")


def _normalize_candidate(url: str) -> str:
    return html_lib.unescape(url).strip().rstrip(".,;:)]}\"'")


def _is_activation_link(url: str) -> bool:
    lowered = url.lower()
    return (
        "qwen.ai" in lowered
        and any(keyword in lowered for keyword in ACTIVATION_KEYWORDS)
        and not any(excluded in lowered for excluded in EXCLUDED_LINK_PARTS)
    )


def extract_activation_link(html: str = "", text: str = "") -> Optional[str]:
    """从邮件 HTML/文本内容中提取 Qwen 激活链接。"""

    if html:
        soup = BeautifulSoup(html, "html.parser")
        for link in soup.find_all("a", href=True):
            href = _normalize_candidate(link["href"])
            if _is_activation_link(href):
                return href

    content = html_lib.unescape(f"{html}\n{text}")

    patterns = (
        r'https://[^"\s<>]*qwen\.ai[^"\s<>]*(?:verify|activate|auth)[^"\s<>]*',
        r'https://[^"\s<>]*qwen\.ai[^"\s<>]+',
    )

    for pattern in patterns:
        for match in re.finditer(pattern, content, re.IGNORECASE):
            candidate = _normalize_candidate(match.group(0))
            if _is_activation_link(candidate):
                return candidate

    return None
```

---

## 3. 架构设计

### 3.1 项目结构

```text
qwen-register-2/
├── qwenv4.py                          # 主入口（重构）
├── email_providers/                    # 邮箱服务模块
│   ├── __init__.py                     # 导出公共接口
│   ├── base.py                         # EmailProvider 抽象基类与异常
│   ├── mailtm.py                       # Mail.tm 实现
│   ├── mailporary.py                   # Mailporary 实现
│   ├── generator_email.py              # Generator.email 实现
│   ├── factory.py                      # Provider 工厂类
│   ├── store.py                        # UsedEmailsStore 持久化去重
│   └── utils.py                        # 公共工具函数
├── docs/
│   ├── superpowers/specs/              # 设计文档目录
│   └── EMAIL_PROVIDERS.md              # 邮箱服务使用指南
├── test/
│   └── test_email_providers.py         # 单元测试
├── used_emails.json                    # 已使用邮箱记录
├── proxy.txt                           # 浏览器代理配置
├── requirements.txt                    # 依赖清单
└── install.sh                          # 安装脚本
```

### 3.2 核心组件

#### 3.2.1 EmailProvider 抽象基类

所有邮箱服务实现统一继承 `EmailProvider`。基类负责保存通用配置、邮箱状态和日志能力；子类只实现具体渠道逻辑。

```python
import logging
from abc import ABC, abstractmethod
from typing import Any, Optional


class EmailProviderError(Exception):
    """邮箱 provider 基础异常。"""


class EmailCreationError(EmailProviderError):
    """邮箱创建失败。"""


class EmailTimeoutError(EmailProviderError):
    """等待激活邮件超时。"""


class EmailParseError(EmailProviderError):
    """邮件内容解析失败。"""


class EmailProvider(ABC):
    def __init__(
        self,
        verbose: bool = False,
        api_proxy: Optional[str] = None,
        context: Optional[Any] = None,
    ) -> None:
        self.verbose = verbose
        self.api_proxy = api_proxy
        self.context = context          # 仅 Generator.email 必需
        self.email: Optional[str] = None
        self.logger = self._setup_logger()

    def _setup_logger(self) -> logging.Logger:
        logger = logging.getLogger(f"email_providers.{self.__class__.__name__}")
        logger.setLevel(logging.DEBUG if self.verbose else logging.INFO)

        # 防止多次创建 provider 时重复添加 handler，导致日志重复输出。
        if not logger.handlers:
            handler = logging.StreamHandler()
            handler.setFormatter(logging.Formatter("%(message)s"))
            logger.addHandler(handler)

        logger.propagate = False
        return logger

    def log_info(self, message: str) -> None:
        self.logger.info(f"✓ {message}")

    def log_debug(self, message: str) -> None:
        if self.verbose:
            self.logger.debug(f"→ {message}")

    def log_error(self, message: str) -> None:
        self.logger.error(f"✗ {message}")

    @abstractmethod
    def create_inbox(self) -> str:
        """
        创建临时邮箱并返回邮箱地址。

        实现要求：
        - 生成邮箱地址并确保服务端可用
        - 成功后保存到 self.email
        - 输出成功日志
        - 失败时抛出 EmailCreationError
        """

    @abstractmethod
    def get_activation_link(
        self,
        timeout: int = 300,
        keywords: tuple[str, ...] = ("qwen", "alibaba"),
    ) -> str:
        """
        轮询验证邮件并返回完整激活 URL。

        实现要求：
        - 在 timeout 秒内轮询邮件
        - 使用 keywords 过滤 Qwen/Alibaba 相关邮件
        - 调用 extract_activation_link() 提取链接
        - 超时抛出 EmailTimeoutError
        - 找到邮件但无法解析链接时抛出 EmailParseError
        """

    @abstractmethod
    def cleanup(self) -> None:
        """清理 HTTP 客户端、页面句柄等资源。"""
```

#### 3.2.2 Provider 实现

- **MailtmProvider**：调用 Mail.tm API，使用 `api_proxy` 配置 `httpx.Client(proxy=...)`
- **MailporaryProvider**：从 Mailporary 页面提取 token 后调用 API，使用 `api_proxy` 配置 `httpx.Client(proxy=...)`
- **GeneratorEmailProvider**：重构现有页面交互实现，需要 Playwright `BrowserContext`，不使用 `api_proxy`

Provider 初始化参数使用规则：

| Provider | `api_proxy` | `context` | 说明 |
|---|---|---|---|
| `mailtm` | 可选 | 不使用 | 纯 API 渠道 |
| `mailporary` | 可选 | 不使用 | 首页 token 获取和 API 请求均走 httpx |
| `generator.email` | 不使用 | 必需 | 页面渠道，使用浏览器代理配置 |

#### 3.2.3 EmailProviderFactory

工厂类负责根据类型创建 provider 实例，并校验必要依赖。

```python
from typing import Any, Optional


class EmailProviderFactory:
    _PROVIDERS = {
        "mailtm": MailtmProvider,
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
            raise ValueError("generator.email provider 需要传入 BrowserContext")

        return provider_cls(
            verbose=verbose,
            api_proxy=api_proxy if provider_cls is not GeneratorEmailProvider else None,
            context=context if provider_cls is GeneratorEmailProvider else None,
        )
```

---

## 4. 命令行接口

### 4.1 命令行参数

```bash
python qwenv4.py <count> [options]

位置参数:
  count                  要注册的账号数量

可选参数:
  --email-provider       邮箱服务类型 (mailtm|mailporary|generator.email)
                         默认: generator.email

  --api-proxy            API 请求代理地址 (如: http://127.0.0.1:7890)
                         仅 Mail.tm / Mailporary 使用

  --verbose, -v          启用详细日志输出

  --strict               严格模式，默认启用；失败时跳过当前账号，不自动降级
```

### 4.2 使用示例

```bash
# 使用 Mail.tm；浏览器使用 proxy.txt，API 直连
python qwenv4.py 5 --email-provider mailtm

# 使用 Mailporary，启用详细日志
python qwenv4.py 3 --email-provider mailporary --verbose

# 浏览器使用 proxy.txt，API 使用单独代理
python qwenv4.py 5 --email-provider mailtm --api-proxy http://127.0.0.1:7890

# 使用默认 generator.email；只使用浏览器代理配置
python qwenv4.py 5
```

---

## 5. 数据流设计

### 5.1 主流程

```text
用户输入
  ↓
命令行参数解析
  ↓
加载浏览器代理 proxy.txt
  ↓
初始化 UsedEmailsStore
  ↓
for 每个账号:
  ├─ EmailProviderFactory.create(...)
  ├─ provider.create_inbox()                  → 返回邮箱地址
  ├─ UsedEmailsStore 检查并记录邮箱
  ├─ 填写注册表单 (Playwright)
  ├─ 处理验证码 (Playwright)
  ├─ provider.get_activation_link()           → 返回激活链接
  ├─ 打开激活链接 (Playwright)
  ├─ 提取 Token (Playwright)
  ├─ 保存账号信息
  └─ provider.cleanup()
  ↓
输出统计信息
```

### 5.2 邮件处理流程

**Mail.tm / Mailporary（API 方式）**：

1. 通过 API 创建或激活临时邮箱
2. 轮询 API 获取邮件列表
3. 用 `keywords=('qwen', 'alibaba')` 过滤目标邮件
4. 获取邮件详情
5. 从邮件 HTML/文本中提取激活链接
6. 将链接交给 Playwright 打开

**Generator.email（页面方式）**：

1. 生成邮箱地址并打开邮箱页面
2. 轮询页面中的邮件列表
3. 点击 Qwen/Alibaba 相关邮件
4. 提取激活按钮的 `href` 属性
5. 返回链接给主流程

---

## 6. 错误处理

### 6.1 异常层次

```text
EmailProviderError              # 基础异常
├── EmailCreationError          # 邮箱创建失败
├── EmailTimeoutError           # 获取邮件超时
└── EmailParseError             # 邮件解析失败
```

### 6.2 API 错误处理策略

| 错误类型 | HTTP 状态码 | 处理策略 | 重试次数 | 等待时间 |
|---|---:|---|---:|---:|
| 限流 | 429 | 等待后重试；仍失败则跳过当前账号 | 3 次 | 60s |
| 服务不可用 | 503 | 短暂等待后重试；仍失败则跳过当前账号 | 2 次 | 5s |
| 认证失败 | 401 | Mailporary 刷新 token 后重试 1 次；Mail.tm 重新获取 token 后重试 1 次 | 1 次 | 0-3s |
| 网络超时 | - | 使用当前 `--api-proxy` 重试；不在 provider 内切换代理 | 2 次 | 3s |
| 邮件内容为空 | - | 继续等待直到超时 | 不限 | 轮询间隔 |
| 链接格式错误 | - | 记录错误并跳过当前账号 | 0 次 | - |

> 说明：`--api-proxy` 是固定 API 代理配置。代理轮换只属于浏览器侧 `proxy.txt` 的既有逻辑，不由 API provider 隐式切换。

### 6.3 严格模式

严格模式为默认行为：

- 邮箱服务失败 → 记录错误 → 跳过当前账号 → 继续下一个账号
- 不自动切换到其他邮箱渠道
- 单个账号失败不影响后续账号
- 如需更换邮箱渠道，由用户通过 `--email-provider` 显式选择后重新运行

### 6.4 代理配置使用范围

| 配置 | 使用方 | 是否轮换 | 说明 |
|---|---|---|---|
| `proxy.txt` | Playwright 浏览器上下文 | 保持现有逻辑 | 注册页、验证码、激活链接打开、generator.email 页面 |
| `--api-proxy` | `httpx` API 请求 | 固定代理 | Mail.tm / Mailporary 的 API 请求 |

使用场景：

```bash
# 浏览器走 proxy.txt，API 直连
python qwenv4.py 5 --email-provider mailtm

# 浏览器走 proxy.txt，API 走指定代理
python qwenv4.py 5 --email-provider mailtm --api-proxy http://127.0.0.1:7890

# Generator.email 只使用浏览器代理，不使用 --api-proxy
python qwenv4.py 5 --email-provider generator.email
```

---

## 7. 持久化存储

### 7.1 已使用邮箱存储（used_emails.json）

```json
{
  "emails": [
    "oc7f2a3b4e@example.com",
    "test123@halyang.my.id"
  ],
  "count": 2
}
```

**实现细节**：

- 内存结构：`set[str]`，统一保存小写邮箱地址
- 加载时机：`UsedEmailsStore` 初始化时从 `used_emails.json` 加载
- 保存时机：每次 `add()` 后立即保存
- 并发写入：使用 `portalocker` 文件锁保护读-改-写流程
- 写入策略：持锁后重新加载最新文件，再写回，避免多进程并发时覆盖其他进程新增的邮箱

```python
class UsedEmailsStore:
    def __init__(self, path: str = "used_emails.json") -> None:
        self.path = Path(path)
        self.lock_path = self.path.with_suffix(self.path.suffix + ".lock")
        self.emails: set[str] = set()
        self.load()

    def load(self) -> None:
        if not self.path.exists():
            self.emails = set()
            return
        data = json.loads(self.path.read_text(encoding="utf-8"))
        self.emails = {email.lower() for email in data.get("emails", [])}

    def is_used(self, email: str) -> bool:
        return email.lower() in self.emails

    def add(self, email: str) -> None:
        normalized = email.lower()
        with portalocker.Lock(str(self.lock_path), timeout=10):
            self.load()  # 持锁后重新加载，避免覆盖并发写入
            self.emails.add(normalized)
            payload = {
                "emails": sorted(self.emails),
                "count": len(self.emails),
            }
            self.path.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
```

**调用时机**：

1. `provider.create_inbox()` 返回后立即检查
2. 如果邮箱已使用：
   - Mail.tm / Mailporary：最多重试创建 3 次
   - Generator.email：记录并跳过当前账号，避免页面状态复杂化
3. 如果邮箱未使用：调用 `used_emails_store.add(email)` 并继续注册流程

```text
for attempt in range(max_email_retries):
    email = provider.create_inbox()
    if not used_emails_store.is_used(email):
        used_emails_store.add(email)
        break

    provider.log_debug(f"邮箱已使用，准备重试: {email}")
else:
    raise EmailCreationError("多次创建邮箱均重复")
```

### 7.2 账号信息存储

保持现有格式不变：

- `qwen_accounts.txt` - 文本格式
- `qwen_accounts.json` - JSON 数组格式

---

## 8. 日志设计

### 8.1 日志实现

使用 Python `logging` 模块。每个 provider 使用独立 logger 名称，避免重复 handler。

```python
def _setup_logger(self) -> logging.Logger:
    logger = logging.getLogger(f"email_providers.{self.__class__.__name__}")
    logger.setLevel(logging.DEBUG if self.verbose else logging.INFO)

    if not logger.handlers:
        handler = logging.StreamHandler()
        handler.setFormatter(logging.Formatter("%(message)s"))
        logger.addHandler(handler)

    logger.propagate = False
    return logger
```

简化方法：

- `log_info(msg)` → `✓ msg`，始终显示
- `log_debug(msg)` → `→ msg`，仅 `--verbose` 显示
- `log_error(msg)` → `✗ msg`，始终显示

### 8.2 简洁模式（默认）

```text
[1/5] 开始注册账号
✓ 邮箱生成成功: oc7f2a3b4e@example.com (Mail.tm)
✓ 激活链接获取成功
✓ 账号注册成功!
```

### 8.3 详细模式（--verbose）

```text
[1/5] ========== 开始生成 Mail.tm 邮箱 ==========
→ 正在获取可用域名...
→ 获取到 3 个可用域名
→ 正在创建账户: oc7f2a3b4e@example.com
→ 账户创建成功
→ 正在获取 Bearer Token...
✓ 邮箱生成成功: oc7f2a3b4e@example.com

========== 开始等待激活邮件 ==========
→ 第 1/100 次尝试获取邮件...
→ 暂无新邮件，等待中...
→ 第 2/100 次尝试获取邮件...
→ 发现 Qwen 邮件，正在提取激活链接...
✓ 激活链接获取成功
```

---

## 9. 测试策略

### 9.1 单元测试

```python
# 测试每个 provider 的核心方法
test_mailtm_create_inbox()
test_mailporary_create_inbox()
test_generator_email_create_inbox()

# 测试工具函数
test_extract_activation_link_from_html()
test_extract_activation_link_from_text()
test_extract_activation_link_excludes_tracking_links()

# 测试工厂类
test_factory_create_valid_provider()
test_factory_create_invalid_provider()
test_factory_requires_context_for_generator_email()

# 测试存储
test_used_emails_store_add()
test_used_emails_store_is_used()
test_used_emails_store_concurrent_writes()

# 测试错误处理
test_api_rate_limit_retry()
test_api_proxy_is_independent_from_browser_proxy()
```

### 9.2 集成测试

```python
# 端到端测试（需要真实的 API 访问）
test_mailtm_full_flow()
test_mailporary_full_flow()
test_generator_email_full_flow()
```

---

## 10. 依赖管理

### 10.1 新增依赖

```txt
httpx>=0.27.0            # 同步 HTTP 客户端，支持 API 代理
beautifulsoup4>=4.12.3   # HTML 解析
lxml>=5.3.0              # XML/HTML 解析器
portalocker>=2.10.1      # used_emails.json 并发写入文件锁
pytest>=8.3.3            # 测试框架
```

### 10.2 现有依赖

```txt
playwright==1.48.0      # 浏览器自动化（保持不变）
```

---

## 11. 扩展性

### 11.1 新增邮箱服务

新增服务只需 3 步：

1. **创建 provider 类**：

   ```python
   # email_providers/newservice.py
   class NewServiceProvider(EmailProvider):
       def create_inbox(self) -> str:
           # 实现逻辑
           ...

       def get_activation_link(
           self,
           timeout: int = 300,
           keywords: tuple[str, ...] = ("qwen", "alibaba"),
       ) -> str:
           # 实现逻辑
           ...

       def cleanup(self) -> None:
           # 实现逻辑
           ...
   ```

2. **注册到工厂类**：

   ```python
   # email_providers/factory.py
   _PROVIDERS = {
       "newservice": NewServiceProvider,
       # ...
   }
   ```

3. **更新文档和命令行说明**：

   - 更新 `docs/EMAIL_PROVIDERS.md`
   - 更新 `--email-provider` 帮助文本

### 11.2 未来可能的扩展

- GPTMail Service（需要 API Key）
- Tempmail Service（需要域名配置）
- 邮箱服务健康检查
- 显式非严格模式下的可配置 fallback 链（不在本期实现，且不能影响默认严格模式）

---

## 12. 兼容性

### 12.1 保持兼容

- ✅ 输出文件格式不变（txt/json）
- ✅ 代理配置文件不变（`proxy.txt`）
- ✅ 默认行为不变（`generator.email`）
- ✅ 命令行基本用法不变（`python qwenv4.py 5`）
- ✅ Token 提取逻辑（`extract_tokens`）保留在主流程 `qwenv4.py`
- ✅ IP 检测逻辑（`get_current_ip`）保留在主流程 `qwenv4.py`
- ✅ 截图逻辑（`page.screenshot` / `qwen.screenshot`）保留在主流程 `qwenv4.py`
- ✅ 验证码处理逻辑（`wait_for_captcha_completion`）保留在主流程 `qwenv4.py`

### 12.2 新增功能

- ✅ `--email-provider` 参数
- ✅ `--api-proxy` 参数
- ✅ `--verbose` 参数
- ✅ `used_emails.json` 文件

### 12.3 重构边界

迁移到 `email_providers/` 的内容：

- 邮箱创建
- 邮件轮询
- 激活链接提取
- 邮箱 provider 相关错误处理

保留在 `qwenv4.py` 主流程的内容：

- 注册表单填写
- Playwright 浏览器上下文和页面管理
- 验证码等待
- 激活链接打开
- Token 提取
- 账号信息保存

---

## 13. 风险评估

### 13.1 技术风险

| 风险 | 影响 | 缓解措施 |
|---|---|---|
| API 服务不可用 | 高 | 严格模式跳过当前账号，并提示用户显式切换服务后重新运行 |
| 页面结构变更 | 中 | 对 Generator.email 保留独立 provider；失败时不自动 fallback，只提示手动切换 |
| 代理配置错误 | 中 | 明确区分浏览器代理和 API 代理，详细模式输出请求错误 |
| 链接提取失败 | 中 | 使用 HTML 解析、关键词正则、兜底匹配三层策略 |
| 并发写入损坏 `used_emails.json` | 中 | 使用 `portalocker` 文件锁保护读-改-写流程 |

### 13.2 实施风险

| 风险 | 影响 | 缓解措施 |
|---|---|---|
| 重构引入 bug | 高 | 完整单元测试 + 小步集成 |
| 用户学习成本 | 低 | 提供 `docs/EMAIL_PROVIDERS.md` 使用文档 |
| 性能下降 | 低 | API 渠道实际更快，页面渠道保持原行为 |

---

## 14. 实施计划

### 14.1 阶段划分

**阶段 1：基础架构（2 小时）**

- 创建 `email_providers/` 模块结构
- 实现 `EmailProvider` 抽象基类和异常层次
- 实现 `EmailProviderFactory` 工厂类
- 实现 `UsedEmailsStore` 存储类

**阶段 2：Provider 实现（2 小时）**

- 实现 `MailtmProvider`
- 实现 `MailporaryProvider`
- 重构 `GeneratorEmailProvider`

**阶段 3：主流程重构（1 小时）**

- 重构 `qwenv4.py` 主流程
- 实现命令行参数解析
- 集成所有 provider
- 保留现有 Token 提取、IP 检测、截图和验证码处理逻辑

**阶段 4：测试与文档（1 小时）**

- 编写单元测试
- 编写使用文档
- 端到端测试

**总计**：约 6 小时

### 14.2 验收标准

- [ ] 所有 3 个 provider 能正常创建邮箱
- [ ] 所有 3 个 provider 能正常获取激活链接
- [ ] 命令行参数正确解析
- [ ] 已使用邮箱正确去重
- [ ] `used_emails.json` 并发写入不会导致文件损坏
- [ ] 所有 API provider 能正确处理限流（429）
- [ ] API 代理配置独立于浏览器代理
- [ ] Generator.email 的 `context` 依赖正确传递
- [ ] 激活链接提取成功率 > 95%（基于至少 100 封样本/模拟邮件测试）
- [ ] 错误处理符合严格模式要求：单账号失败不影响后续账号，且不自动降级
- [ ] 日志输出清晰可读，且多 provider 实例不会重复打印日志
- [ ] 文档完整准确
- [ ] 单元测试覆盖核心功能

---

## 15. 总结

本设计方案采用**接口驱动**的模块化架构，通过抽象基类定义统一接口，使用工厂模式创建实例，实现了对多邮箱服务的良好支持。设计遵循 SOLID 原则，保持代码简洁可维护，同时为未来扩展留有空间。

**核心优势**：

- ✅ 架构清晰，职责分离
- ✅ 易于扩展新服务
- ✅ 完善的错误处理
- ✅ 持久化去重机制
- ✅ 浏览器代理与 API 代理边界清晰
- ✅ 默认严格模式无隐式 fallback
- ✅ 兼容现有代码

**实施可行性**：高  
**维护成本**：低  
**扩展性**：强

---

**审批**：

- [ ] 设计评审通过
- [ ] 开始实施
