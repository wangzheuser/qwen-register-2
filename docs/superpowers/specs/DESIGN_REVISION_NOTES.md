# 设计文档修订说明

**文档**: `2026-06-06-multi-email-provider-integration-design.md`  
**修订版本**: v1.3  
**审查者反馈**: 需要补充关键实施细节，并修复章节编号与严格模式矛盾

---

## 修订状态

本说明列出的修订项已合并到主设计文档 v1.3。当前状态为：**已同步当前同步实现方案，待重新审查**。

---

## 主要修订内容

### 1. ✅ 新增并完善第 2 章：技术调研

#### 2.1 Mail.tm API 调研

- 补充完整 API 调用流程示例
- 明确 API 端点、认证方式、数据格式
- 明确 `html` 字段为数组，需要 join 后解析
- 将示例激活链接统一为 `https://studio.qwen.ai/auth/verify?token=...`

#### 2.2 Mailporary API 调研

- 补充 Token 提取方式：从页面 `__NUXT_DATA__` 解析
- 补充完整 API 调用流程示例
- 补充内置域名示例
- 明确 `401` 应优先刷新 token 后重试一次

#### 2.3 激活链接格式分析

- 明确 Qwen 激活邮件链接格式：`https://studio.qwen.ai/auth/verify?token=<JWT>`
- 补充三层链接提取策略：
  1. HTML 解析 `<a>` 标签
  2. 正则匹配激活关键词 URL
  3. 兜底匹配任意 `qwen.ai` 链接
- 补充排除模式：unsubscribe、tracking、analytics、广告链接
- 补充 HTML 实体反转义和 URL 尾部标点裁剪

---

### 2. ✅ 完善接口定义（第 3.2.1 节）

#### EmailProvider 抽象基类补充

- 完整的 `__init__` 参数：
  - `verbose: bool`
  - `api_proxy: Optional[str]`
  - `context: Optional[BrowserContext]`（仅 `generator.email` 需要）
- 内部属性：
  - `self.email: Optional[str]` - 存储邮箱地址
  - `self.logger` - 日志记录器
- 日志方法：
  - `log_info()` - `✓` 前缀，始终显示
  - `log_debug()` - `→` 前缀，仅 verbose 显示
  - `log_error()` - `✗` 前缀，始终显示

#### `create_inbox()` 方法规范

- 返回类型：`str`
- 异常：`EmailCreationError`
- 实现要点：生成邮箱 → 确保可用 → 保存到 `self.email` → 记录日志

#### `get_activation_link()` 方法规范

- 新增参数：`keywords: tuple[str, ...] = ('qwen', 'alibaba')` - 邮件过滤关键词
- 返回类型：`str` - 完整 URL
- 异常：`EmailTimeoutError` | `EmailParseError`
- 实现要点：轮询 → 过滤 → 获取详情 → 提取链接

---

### 3. ✅ 完善工厂类签名（第 3.2.3 节）

```python
class EmailProviderFactory:
    @staticmethod
    def create(
        provider_type: str,
        verbose: bool = False,
        api_proxy: Optional[str] = None,
        context: Optional[BrowserContext] = None,
    ) -> EmailProvider:
        ...
```

**关键变更**：

- 新增 `context` 参数，用于 `generator.email`
- API provider 使用 `api_proxy`
- 页面 provider 使用 `context`，不使用 `api_proxy`
- `generator.email` 缺少 `context` 时应抛出明确错误

---

### 4. ✅ 补充邮箱去重详细逻辑（第 7.1 节）

#### UsedEmailsStore 实现细节

- 数据结构：`set[str]`，存储小写邮箱地址
- 加载时机：初始化时从 `used_emails.json` 加载
- 保存时机：每次 `add()` 后立即保存
- 并发写入：使用 `portalocker` 文件锁保护读-改-写流程
- 依赖同步：第 10.1 节已加入 `portalocker==2.10.1`

#### 调用时机

1. `create_inbox()` 返回后立即检查
2. 如果已使用：Mail.tm / Mailporary 最多重试 3 次；generator.email 跳过当前账号
3. 如果未使用：调用 `add()` 并继续

---

### 5. ✅ 完善错误处理策略（第 6 章）

#### API 错误码处理表

| 错误类型 | HTTP 状态码 | 处理策略 | 重试次数 | 等待时间 |
|---|---:|---|---:|---:|
| 限流 | 429 | 等待后重试；仍失败则跳过当前账号 | 3 次 | 60s |
| 服务不可用 | 503 | 短暂等待后重试 | 2 次 | 5s |
| 认证失败 | 401 | 刷新或重新获取 token 后重试一次 | 1 次 | 0-3s |
| 网络超时 | - | 使用当前 `--api-proxy` 重试；不在 provider 内切换代理 | 2 次 | 3s |
| 邮件内容为空 | - | 继续等待直到超时 | 不限 | 轮询间隔 |
| 链接格式错误 | - | 记录错误并跳过当前账号 | 0 次 | - |

#### 异常层次

```text
EmailProviderError              # 基础异常
├── EmailCreationError          # 邮箱创建失败
├── EmailTimeoutError           # 获取邮件超时
└── EmailParseError             # 邮件解析失败
```

---

### 6. ✅ 明确代理配置使用范围（第 6.4 节）

**配置分离**：

- `proxy.txt` → 浏览器代理（Playwright 使用，保持现有轮换逻辑）
- `--api-proxy` → API 请求代理（httpx 使用，固定代理）
- `generator.email` 只使用浏览器代理，不使用 `--api-proxy`

**使用场景**：

```bash
# 浏览器走 proxy.txt，API 直连
python qwenv4.py 5 --email-provider mailtm

# 浏览器和 API 使用不同代理配置
python qwenv4.py 5 --email-provider mailtm --api-proxy http://127.0.0.1:7890

# Generator.email 只使用浏览器代理
python qwenv4.py 5 --email-provider generator.email
```

---

### 7. ✅ 明确保留现有功能（第 12.1 与 12.3 节）

**保留在主流程 `qwenv4.py` 的内容**：

- Token 提取（`extract_tokens`）
- IP 检测（`get_current_ip`）
- 截图（`page.screenshot` / `qwen.screenshot`）
- 验证码处理（`wait_for_captcha_completion`）
- 注册表单填写、Playwright 交互和账号信息保存

**迁移到 `email_providers/` 的内容**：

- 邮箱创建
- 邮件轮询
- 激活链接提取
- 邮箱 provider 相关错误处理

---

### 8. ✅ 修复日志系统实现（第 8 章）

**问题修复**：避免多次创建 provider 时重复添加 logging handler。

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

---

### 9. ✅ 补充验收标准（第 14.2 节）

新增或修正的验收项：

- [ ] 所有 API provider 能正确处理限流（429）
- [ ] `used_emails.json` 并发写入不会导致文件损坏
- [ ] 激活链接提取成功率 > 95%（基于至少 100 封样本/模拟邮件测试）
- [ ] 严格模式下单账号失败不影响后续账号，且不自动降级
- [ ] API 代理配置独立于浏览器代理
- [ ] Generator.email 的 `context` 依赖正确传递
- [ ] 多 provider 实例不会导致日志重复打印

同时将验收标准从已完成状态 `[x]` 改为待验证状态 `[ ]`，避免在设计阶段提前标记完成。

---

### 10. ✅ 移除或限定矛盾内容（第 11.2 与第 13 章）

**原问题内容**：

> 自动降级模式（可配置的 fallback 链）

**问题**：与第 1.2 节“严格模式：失败时跳过当前账号，不自动降级到其他邮箱渠道”矛盾。

**修正**：

- 默认严格模式下不允许隐式 fallback
- 未来扩展中仅保留“显式非严格模式下的可配置 fallback 链”，并标注“不在本期实现”
- 风险评估中将“Generator.email 作为备用”改为“提示用户显式切换服务后重新运行”

---

### 11. ✅ 修复章节编号与 Markdown 格式

- 修复架构设计下 `2.1`、`2.2` 的错误编号，改为 `3.1`、`3.2`
- 将重复的 `## 3. 接口设计` 改为 `## 4. 命令行接口`
- 顺延后续顶级章节编号至第 15 章
- 修复 `##下一步行动` 为 `## 下一步行动`

---

### 12. ✅ 同步接口适配当前实现

当前 `qwenv4.py` 使用 `playwright.sync_api`。本期实施不做全量 async 迁移，因此主设计文档 v1.3 已将 provider 接口从 `async def` / `await` 调整为同步方法：

```python
def create_inbox(self) -> str: ...
def get_activation_link(self, timeout: int = 300, keywords: tuple[str, ...] = ("qwen", "alibaba")) -> str: ...
def cleanup(self) -> None: ...
```

同时将 `httpx.AsyncClient` 修正为 `httpx.Client`，并移除 `pytest-asyncio` 依赖要求。

---

## 复审目标

修订后，主设计文档应达到：

- ✅ 接口定义完整清晰
- ✅ 实施细节充分，包含关键示例代码
- ✅ 错误处理策略明确
- ✅ 代理边界明确
- ✅ 并发写入风险有处理方案
- ✅ 验收标准未提前标记完成
- ✅ 默认严格模式无逻辑矛盾

**复审目标**：通过设计评审。

---

## 下一步行动

1. 重新审查主设计文档 v1.2
2. 如审查通过，提交到 git
3. 请用户审阅最终版本
4. 调用 `writing-plans` 技能创建实施计划
