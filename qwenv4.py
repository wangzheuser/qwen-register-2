#!/usr/bin/env python3
"""
Qwen 自动注册脚本 v3 - 增强 Token 提取
使用临时邮箱自动完成 Qwen 注册与验证。
支持代理轮换、验证码检测和认证令牌提取。

Author: wangqiupei
"""

import argparse
import time
import random
import string
import sys
import json
import os
import threading
import signal
from concurrent.futures import ThreadPoolExecutor, wait, FIRST_COMPLETED
from datetime import datetime
import httpx
from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeout

try:  # pragma: no cover - 依赖缺失时保留线程级兜底锁。
    import portalocker  # type: ignore
except ImportError:  # pragma: no cover
    portalocker = None  # type: ignore

from captcha_solvers.ai_slider import (
    DEFAULT_CAPTCHA_AI_ATTEMPTS,
    DEFAULT_CAPTCHA_AI_BASE_URL,
    DEFAULT_CAPTCHA_AI_MODEL,
    DEFAULT_CAPTCHA_AI_TIMEOUT,
    CaptchaSolverConfig,
    attach_manual_trace_recorder,
    dump_manual_trace_recording,
    install_aliyun_callback_probe,
    install_aliyun_verify_success_route,
    solve_slider_captcha,
)
from email_providers import EmailProviderFactory, EmailProviderError, EmailCreationError
from email_providers.generator_email import GeneratorEmailProvider
from email_providers.store import UsedEmailsStore
from integrations.qwen2api import (
    DEFAULT_QWEN2API_ADMIN_KEY,
    DEFAULT_QWEN2API_BASE_URL,
    DEFAULT_QWEN2API_TIMEOUT,
    Qwen2ApiSyncConfig,
    sync_account_to_qwen2api,
)

# ──────────────────────────────────────────────────────────
# Configuration
# ──────────────────────────────────────────────────────────

QWEN_REGISTER_URL = "https://chat.qwen.ai/auth?mode=register"
EMAIL_BASE_URL = "https://generator.email"
EMAIL_DOMAIN = "halyang.my.id"
OUTPUT_FILE_TXT = "qwen_accounts.txt"
OUTPUT_FILE_JSON = "qwen_accounts.json"
PROXY_FILE = "proxy.txt"
IMAGES_DIR = "images"
HEADLESS = False  # 必须为 False 以支持手动完成验证码

BROWSER_ARGS = [
    '--disable-blink-features=AutomationControlled',
    '--no-sandbox',
    '--disable-dev-shm-usage',
]

USER_AGENT = (
    'Mozilla/5.0 (Windows NT 10.0; Win64; x64) '
    'AppleWebKit/537.36 (KHTML, like Gecko) '
    'Chrome/124.0.0.0 Safari/537.36'
)

_ACCOUNT_SAVE_LOCKS = {}
_ACCOUNT_SAVE_LOCKS_GUARD = threading.Lock()
STOP_EVENT = threading.Event()
INTERRUPT_COUNT = 0


class _FallbackAccountLock:
    def __init__(self, lock_path):
        with _ACCOUNT_SAVE_LOCKS_GUARD:
            self._lock = _ACCOUNT_SAVE_LOCKS.setdefault(lock_path, threading.Lock())

    def __enter__(self):
        self._lock.acquire()
        return self

    def __exit__(self, exc_type, exc, tb):
        self._lock.release()
        return False


def account_save_lock():
    """保护账号文本和 JSON 输出的跨进程/线程锁。"""
    lock_path = f"{OUTPUT_FILE_JSON}.lock"
    if portalocker is not None:
        return portalocker.Lock(lock_path, timeout=10)
    return _FallbackAccountLock(lock_path)


def positive_int(value):
    """argparse 正整数校验。"""
    try:
        parsed = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("必须是正整数") from exc
    if parsed <= 0:
        raise argparse.ArgumentTypeError("必须是正整数")
    return parsed


def concurrency_value(value):
    """argparse 并发范围校验。"""
    parsed = positive_int(value)
    if parsed > 10:
        raise argparse.ArgumentTypeError("并发数量必须在 1-10 之间")
    return parsed


def parse_args(argv=None):
    """解析命令行参数，保持无 count 时的交互式输入兼容。"""
    parser = argparse.ArgumentParser(
        description="Qwen 自动注册脚本（支持多邮箱服务）"
    )
    parser.add_argument(
        "count",
        nargs="?",
        type=positive_int,
        help="要注册的账号数量；不传则交互式询问",
    )
    parser.add_argument(
        "--email-provider",
        default="mailtm",
        choices=["mailtm", "mail.tm", "mailporary", "generator.email", "generator"],
        help="邮箱服务类型，默认: mailtm",
    )
    parser.add_argument(
        "--api-proxy",
        default=None,
        help="API 请求代理地址，仅 Mail.tm / Mailporary 使用，如 http://127.0.0.1:7890",
    )
    parser.add_argument(
        "--concurrency",
        type=concurrency_value,
        default=1,
        help="并发账号数量，范围 1-10，默认: 1",
    )
    parser.add_argument(
        "--captcha-timeout",
        type=positive_int,
        default=600,
        help="滑块验证码等待秒数，默认: 600",
    )
    parser.add_argument(
        "--captcha-solver",
        choices=["manual", "ai"],
        default="manual",
        help="滑块验证码处理方式：manual=人工等待，ai=优先用 ddddocr 本地匹配，必要时调用 AI；默认: manual",
    )
    parser.add_argument(
        "--captcha-ai-base-url",
        default=DEFAULT_CAPTCHA_AI_BASE_URL,
        help=f"OpenAI 兼容滑块 AI 接口地址，默认: {DEFAULT_CAPTCHA_AI_BASE_URL}",
    )
    parser.add_argument(
        "--captcha-ai-api-key",
        default=os.getenv("CAPTCHA_AI_API_KEY", ""),
        help="滑块 AI API 密钥；也可用环境变量 CAPTCHA_AI_API_KEY",
    )
    parser.add_argument(
        "--captcha-ai-model",
        default=DEFAULT_CAPTCHA_AI_MODEL,
        help=f"滑块 AI 模型 ID，默认: {DEFAULT_CAPTCHA_AI_MODEL}",
    )
    parser.add_argument(
        "--captcha-ai-timeout",
        type=positive_int,
        default=DEFAULT_CAPTCHA_AI_TIMEOUT,
        help=f"滑块 AI 请求超时秒数，默认: {DEFAULT_CAPTCHA_AI_TIMEOUT}",
    )
    parser.add_argument(
        "--captcha-ai-attempts",
        type=positive_int,
        default=DEFAULT_CAPTCHA_AI_ATTEMPTS,
        help=f"滑块 AI 最大尝试次数，默认: {DEFAULT_CAPTCHA_AI_ATTEMPTS}",
    )
    parser.add_argument(
        "--no-captcha-ai-fallback-manual",
        action="store_true",
        help="AI 处理滑块失败时不回退人工等待",
    )
    parser.add_argument(
        "--captcha-record-trace",
        action="store_true",
        help="人工处理滑块时录制轨迹和验证码网络结果，用于后续重放调试",
    )
    parser.add_argument(
        "--captcha-record-only",
        action="store_true",
        help="只进行人工滑块轨迹录制，不运行自动滑块处理（会自动启用 --captcha-record-trace）",
    )
    parser.add_argument(
        "--captcha-replay-trace",
        default="",
        help="读取指定人工成功轨迹 JSON，并在 AI/ ddddocr 模式下按该曲线重放",
    )
    parser.add_argument(
        "--captcha-drag-backend",
        choices=["playwright", "os"],
        default=None,
        help="滑块拖动后端：playwright=浏览器合成事件，os=Windows 真实鼠标事件；AI 模式默认: os",
    )
    parser.add_argument(
        "--captcha-drag-strategy",
        choices=["auto", "closed_loop", "fast_quadratic", "quadratic", "human", "ratio_human", "scaled"],
        default=os.getenv("CAPTCHA_DRAG_STRATEGY", "fast_quadratic"),
        help="滑块拖动策略；AI 模式默认: fast_quadratic",
    )
    parser.add_argument(
        "--captcha-callback-bypass",
        action="store_true",
        help="本地靶场实验：尝试直接触发 AliyunCaptcha 成功回调（默认关闭）",
    )
    parser.add_argument(
        "--captcha-force-verify-success",
        action="store_true",
        help="本地靶场实验：将 Aliyun VerifyCaptchaV2 响应替换为成功（默认关闭）",
    )
    parser.add_argument(
        "--verbose",
        "-v",
        action="store_true",
        help="启用邮箱服务详细日志",
    )
    parser.add_argument(
        "--sync-qwen2api",
        action="store_true",
        help="注册成功保存本地账号后，同步注入到 qwen2API",
    )
    parser.add_argument(
        "--qwen2api-base-url",
        default=DEFAULT_QWEN2API_BASE_URL,
        help=f"qwen2API 后端地址，默认: {DEFAULT_QWEN2API_BASE_URL}",
    )
    parser.add_argument(
        "--qwen2api-admin-key",
        default=DEFAULT_QWEN2API_ADMIN_KEY,
        help="qwen2API 管理密钥，默认: admin",
    )
    parser.add_argument(
        "--qwen2api-timeout",
        type=positive_int,
        default=DEFAULT_QWEN2API_TIMEOUT,
        help=f"qwen2API 同步超时秒数，默认: {DEFAULT_QWEN2API_TIMEOUT}",
    )
    parser.add_argument(
        "--strict",
        action="store_true",
        default=True,
        help="严格模式：失败时跳过当前账号，不自动降级（默认启用，兼容参数）",
    )
    args = parser.parse_args(argv)
    if args.captcha_drag_backend is None:
        args.captcha_drag_backend = os.getenv("CAPTCHA_DRAG_BACKEND") or ("os" if args.captcha_solver == "ai" else "playwright")
    return args


def is_generator_provider(provider_type):
    return provider_type.lower().strip() in {"generator.email", "generator"}


def build_qwen2api_sync_config(args):
    """从命令行参数构造 qwen2API 同步配置。"""
    return Qwen2ApiSyncConfig(
        enabled=bool(args.sync_qwen2api),
        base_url=args.qwen2api_base_url,
        admin_key=args.qwen2api_admin_key,
        timeout=args.qwen2api_timeout,
    )


def build_captcha_solver_config(args):
    """从命令行参数构造滑块验证码处理配置。"""
    captcha_solver = "manual" if getattr(args, "captcha_record_only", False) else getattr(args, "captcha_solver", "manual")
    if getattr(args, "captcha_record_only", False):
        setattr(args, "captcha_record_trace", True)
    if getattr(args, "captcha_record_trace", False):
        os.environ["CAPTCHA_RECORD_TRACE"] = "1"
        os.environ.setdefault("CAPTCHA_DEBUG_NETWORK", "1")
    replay_trace = str(getattr(args, "captcha_replay_trace", "") or "").strip()
    if replay_trace:
        os.environ["CAPTCHA_REPLAY_TRACE"] = replay_trace
    drag_backend = str(getattr(args, "captcha_drag_backend", "") or "").strip().lower()
    if drag_backend in {"playwright", "os"}:
        os.environ["CAPTCHA_DRAG_BACKEND"] = drag_backend
    drag_strategy = str(getattr(args, "captcha_drag_strategy", "") or "").strip().lower()
    if drag_strategy and drag_strategy not in {"auto", "closed_loop"}:
        os.environ["CAPTCHA_DRAG_STRATEGY"] = drag_strategy
    elif drag_strategy in {"auto", "closed_loop"}:
        os.environ.pop("CAPTCHA_DRAG_STRATEGY", None)
    if getattr(args, "captcha_callback_bypass", False):
        os.environ["CAPTCHA_CALLBACK_BYPASS"] = "1"
    if getattr(args, "captcha_force_verify_success", False):
        os.environ["CAPTCHA_FORCE_VERIFY_SUCCESS"] = "1"
    return CaptchaSolverConfig(
        enabled=captcha_solver == "ai",
        base_url=getattr(args, "captcha_ai_base_url", DEFAULT_CAPTCHA_AI_BASE_URL),
        api_key=getattr(args, "captcha_ai_api_key", os.getenv("CAPTCHA_AI_API_KEY", "")),
        model=getattr(args, "captcha_ai_model", DEFAULT_CAPTCHA_AI_MODEL),
        timeout=getattr(args, "captcha_ai_timeout", DEFAULT_CAPTCHA_AI_TIMEOUT),
        attempts=getattr(args, "captcha_ai_attempts", DEFAULT_CAPTCHA_AI_ATTEMPTS),
        fallback_manual=not getattr(args, "no_captcha_ai_fallback_manual", False),
    )


def request_shutdown(reason="收到中断信号"):
    """请求所有账号任务尽快停止。"""
    global INTERRUPT_COUNT
    INTERRUPT_COUNT += 1
    if not STOP_EVENT.is_set():
        print(f"\n🛑 {reason}，正在请求所有任务停止...")
        print("   如果浏览器或底层驱动仍未退出，可再按一次 Ctrl+C 强制结束。")
    elif INTERRUPT_COUNT >= 2:
        print("\n🛑 再次收到中断，强制结束当前进程。")
        os._exit(130)
    STOP_EVENT.set()

def sleep_interruptible(seconds):
    """可被 STOP_EVENT 打断的等待；返回 False 表示被停止请求打断。"""
    remaining = max(0.0, float(seconds))
    while remaining > 0:
        chunk = min(0.2, remaining)
        if STOP_EVENT.wait(chunk):
            return False
        remaining -= chunk
    return True


def collect_account_futures(futures, total_accounts, poll_interval=0.5):
    """轮询收集账号任务结果，避免 Ctrl+C 后长期阻塞在 as_completed。"""
    success_count = 0
    pending = set(futures)
    while pending:
        if STOP_EVENT.is_set():
            for future in pending:
                try:
                    future.cancel()
                except Exception:
                    pass
            break

        try:
            done, pending = wait(pending, timeout=poll_interval, return_when=FIRST_COMPLETED)
        except AttributeError:
            # 单元测试可能传入只有 done/cancel/result 的轻量 fake future。
            done = {future for future in pending if getattr(future, "done", lambda: False)()}
            pending = pending - done
            if not done:
                sleep_interruptible(poll_interval)
                continue

        for future in done:
            index = futures[future]
            try:
                if future.result():
                    success_count += 1
            except KeyboardInterrupt:
                request_shutdown("收到 Ctrl+C")
                for item in pending:
                    try:
                        item.cancel()
                    except Exception:
                        pass
                return success_count
            except Exception as e:
                print(f"  ❌ [账号 {index}/{total_accounts}] 任务异常: {e}")
    return success_count


def maybe_sync_account_to_qwen2api(email, password, token, config, label=""):
    """按配置同步账号到 qwen2API；失败只记录警告。"""
    result = sync_account_to_qwen2api(
        email=email,
        password=password,
        token=token,
        config=config,
    )
    prefix = f"{label} " if label else ""
    if result.skipped:
        if config.enabled:
            print(f"  ⚠️ {prefix}{result.message}")
        return result
    if result.ok:
        print(f"  🔁 {prefix}{result.message}")
    else:
        print(f"  ⚠️ {prefix}qwen2API 同步失败: {result.message}")
    return result

# ──────────────────────────────────────────────────────────
# 代理管理
# ──────────────────────────────────────────────────────────

class ProxyRotator:
    """代理轮换管理器"""

    def __init__(self, proxy_file=PROXY_FILE):
        self.proxies = []
        self.current_index = 0
        self._lock = threading.Lock()
        self.load_proxies(proxy_file)

    def load_proxies(self, proxy_file):
        """从文件加载代理。格式: hostname:port:username:password 或 hostname:port"""
        try:
            with open(proxy_file, 'r') as f:
                lines = [line.strip() for line in f if line.strip() and not line.startswith('#')]

            if not lines:
                print(f"⚠️  在 {proxy_file} 中未找到代理")
                return

            self.proxies = lines
            print(f"✅ 已加载 {len(self.proxies)} 个代理")
            for i, p in enumerate(self.proxies[:3]):
                parts = p.split(':')
                print(f"   [{i+1}] {parts[0]}:{parts[1]}")
            if len(self.proxies) > 3:
                print(f"   ... 以及另外 {len(self.proxies) - 3} 个")

        except FileNotFoundError:
            print(f"⚠️  未找到 {proxy_file}，将不使用代理运行。")
            self.proxies = []

    def get_next(self):
        """获取下一个代理"""
        if not self.proxies:
            return None

        with self._lock:
            proxy = self.proxies[self.current_index]
            self.current_index = (self.current_index + 1) % len(self.proxies)
            return proxy

    @staticmethod
    def parse_proxy(proxy_str):
        """将代理字符串解析为 Playwright 格式的字典"""
        if not proxy_str:
            return None

        parts = proxy_str.split(':')
        if len(parts) == 4:
            hostname, port, username, password = parts
            return {
                'server': f'http://{hostname}:{port}',
                'username': username,
                'password': password,
            }
        elif len(parts) == 2:
            hostname, port = parts
            return {
                'server': f'http://{hostname}:{port}',
            }
        else:
            print(f"⚠️  代理格式无效: {proxy_str}")
            return None

# ──────────────────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────────────────

FIRST_NAMES = ['Alex', 'Jordan', 'Taylor', 'Morgan', 'Casey', 'Riley',
               'Quinn', 'Avery', 'Blake', 'Cameron', 'Skyler', 'Drew',
               'Hayden', 'Parker', 'Reese', 'Sawyer', 'Logan', 'Emerson',
               'Finley', 'Harper', 'Jamie', 'Kendall', 'Lane', 'Marley',
               'Nico', 'Oakley', 'Peyton', 'Rowan', 'Sage', 'Tatum',
               'Wren', 'Adrian', 'Bailey', 'Charlie', 'Dakota', 'Elliot',
               'Frankie', 'Gray', 'Hollis', 'Indigo', 'Jesse', 'Kai',
               'Lennon', 'Micah', 'Noel', 'Phoenix', 'River', 'Sidney',
               'Toby', 'Val', 'West', 'Yael', 'Zane', 'Arden',
               'Brooke', 'Cody', 'Devon', 'Ellis', 'Flynn', 'Glenn',
               'Harlow', 'Ira']

LAST_NAMES = ['Smith', 'Johnson', 'Williams', 'Brown', 'Jones', 'Garcia',
              'Miller', 'Davis', 'Martinez', 'Wilson', 'Anderson', 'Taylor']


def gen_first_name():
    """生成随机名字"""
    return random.choice(FIRST_NAMES)


def gen_prefix(first_name):
    """生成邮箱前缀"""
    rand_digits = ''.join(random.choices(string.digits, k=3))
    return f"{first_name}{rand_digits}"


def gen_password(length=16):
    """
    生成符合 Qwen 要求的强密码

    要求：
    - 至少8个字符
    - 必须包含：大写字母、小写字母、数字、特殊字符

    参数:
        length: 密码长度（默认16，最小12）

    返回:
        str: 符合要求的强密码
    """
    # 确保长度至少为12（更安全）
    length = max(12, length)

    # 定义字符集
    uppercase = string.ascii_uppercase      # A-Z
    lowercase = string.ascii_lowercase      # a-z
    digits = string.digits                  # 0-9
    special = "!@#$%^&*"                   # 特殊字符

    # 确保包含每种类型至少一个字符
    password = [
        random.choice(uppercase),  # 至少1个大写字母
        random.choice(lowercase),  # 至少1个小写字母
        random.choice(digits),     # 至少1个数字
        random.choice(special),    # 至少1个特殊字符
    ]

    # 填充剩余长度
    all_chars = uppercase + lowercase + digits + special
    password.extend(random.choice(all_chars) for _ in range(length - 4))

    # 打乱顺序（避免前4位总是固定模式）
    random.shuffle(password)

    return ''.join(password)


def gen_name(first_name):
    """生成全名"""
    return f"{first_name} {random.choice(LAST_NAMES)}"


def get_current_ip(client=None):
    """通过 HTTP 请求获取当前公网 IP 和国家，不启动浏览器页面。"""
    owns_client = client is None
    http_client = client or httpx.Client(timeout=20.0)
    try:
        data = http_client.get('https://api.ipify.org?format=json').json()
        ip = data.get('ip', 'unknown')

        data2 = http_client.get(f'https://ipinfo.io/{ip}/json').json()
        country = data2.get('country', 'unknown')
        return ip, country
    except Exception as e:
        print(f"  ⚠️  无法检测当前 IP: {e}")
        return 'unknown', 'unknown'
    finally:
        if owns_client:
            http_client.close()


def load_accounts_json():
    """加载现有的账号 JSON 数组"""
    if not os.path.exists(OUTPUT_FILE_JSON):
        return []

    try:
        with open(OUTPUT_FILE_JSON, 'r', encoding='utf-8') as f:
            content = f.read().strip()
            if not content or content == '[]':
                return []
            return json.loads(content)
    except (json.JSONDecodeError, FileNotFoundError):
        return []


def save_account(email, password, name, ip='unknown', country='unknown',
                 token=None, active_token=None, device_id=None, user_role='user'):
    """
    保存账号信息到文本文件和 JSON 文件

    参数:
        email: 邮箱
        password: 密码
        name: 用户名
        ip: IP 地址
        country: 国家
        token: JWT Token
        active_token: 激活 Token
        device_id: 设备 ID
        user_role: 用户角色
    """
    with account_save_lock():
        # 保存到文本文件（兼容原格式）
        with open(OUTPUT_FILE_TXT, 'a', encoding='utf-8') as f:
            if token:
                f.write(f"{email}:{password}\t# {name} | {country} ({ip}) | token={token[:50]}...\n")
            else:
                f.write(f"{email}:{password}\t# {name} | {country} ({ip})\n")

        # 保存到 JSON 文件（数组格式）
        accounts = load_accounts_json()

        account_data = {
            'email': email,
            'password': password,
            'name': name,
            'ip': ip,
            'country': country,
            'token': token,
            'active_token': active_token,
            'device_id': device_id,
            'user_role': user_role,
            'created_at': datetime.now().isoformat(),
            'status': 'activated' if token else 'pending'
        }

        accounts.append(account_data)

        with open(OUTPUT_FILE_JSON, 'w', encoding='utf-8') as f:
            json.dump(accounts, f, ensure_ascii=False, indent=2)

    print(f"  💾 已保存到 {OUTPUT_FILE_TXT} 和 {OUTPUT_FILE_JSON}")


# ──────────────────────────────────────────────────────────
# CAPTCHA Detection & Handling (新增)
# ──────────────────────────────────────────────────────────

def detect_captcha(page):
    """检测页面是否出现验证码"""
    captcha_texts = ("拖动滑块完成拼图", "访问验证", "验证您是真人", "请完成以下操作")
    selectors = (
        "#waf_nc_block",
        ".geetest_window",
        "[id*='nc_']",
        "[class*='nc_']",
        "[class*='btn_slide']",
        "[class*='captcha']",
        "[class*='Captcha']",
    )
    try:
        for selector in selectors:
            try:
                locator = page.locator(selector).first
                if locator.is_visible(timeout=500):
                    return True
            except Exception:
                continue
        body_text = page.evaluate('document.body.innerText') or ''
        return any(text in body_text for text in captcha_texts)
    except Exception:
        return False


def wait_for_captcha_completion(page, email, password, name, timeout=600):
    """
    等待用户手动完成验证码

    参数:
        page: Playwright 页面对象
        email: 注册邮箱（用于显示提示）
        password: 注册密码（用于显示提示）
        name: 注册用户名（用于显示提示）
        timeout: 超时时间（秒）

    返回:
        bool: 验证码是否成功完成
    """
    print(f"\n{'='*60}")
    print(f"  🤖 检测到验证码，需要手动完成")
    print(f"{'='*60}")
    print(f"  📧 邮箱:    {email}")
    print(f"  🔑 密码:    {password}")
    print(f"  👤 用户名:  {name}")
    print(f"\n  ⚠️  请在浏览器中手动拖动滑块完成验证")
    minutes = timeout // 60
    minute_text = f" / {minutes}分钟" if minutes else ""
    print(f"  ⏳ 等待验证码完成... (最多 {timeout}s{minute_text})")
    print(f"{'='*60}\n")

    start_time = time.time()
    check_interval = 2  # 每2秒检查一次

    while time.time() - start_time < timeout:
        if STOP_EVENT.is_set():
            print("  🛑 已收到停止请求，结束验证码等待")
            return False
        try:
            # 检查验证码弹窗是否还存在
            captcha_exists = detect_captcha(page)

            if not captcha_exists:
                print("  ✅ 验证码已完成！")
                sleep_interruptible(2)  # 等待页面处理
                return True

            # 检查页面是否有错误
            try:
                body_text = page.evaluate('document.body.innerText') or ''
                if 'error' in body_text.lower() or 'failed' in body_text.lower():
                    print(f"  ❌ 检测到注册错误")
                    return False
            except Exception:
                pass

            elapsed = int(time.time() - start_time)
            if elapsed > 0 and elapsed % 15 == 0:
                print(f"  ⏳ 仍在等待验证码完成... ({elapsed}s/{timeout}s)")

            if STOP_EVENT.is_set():
                print("  🛑 已收到停止请求，结束验证码等待")
                return False
            sleep_interruptible(check_interval)

        except Exception as e:
            print(f"  ⚠️  验证码检测错误: {e}")
            sleep_interruptible(check_interval)

    print(f"  ⏰ 验证码等待超时 ({timeout}s)")
    return False


# ──────────────────────────────────────────────────────────
# 认证令牌提取（新增）
# ──────────────────────────────────────────────────────────

def extract_tokens(page):
    """
    从页面的 localStorage 提取认证 Token

    返回:
        dict: 包含 token, active_token, device_id, user_role 的字典
    """
    try:
        result = page.evaluate("""
            () => {
                const data = {
                    token: localStorage.getItem('token'),
                    active_token: localStorage.getItem('active_token'),
                    device_id: localStorage.getItem('qwen_chat_device_id'),
                    user_role: localStorage.getItem('userRole'),
                };
                return data;
            }
        """)
        return result
    except Exception as e:
        print(f"  ⚠️  认证令牌提取失败: {e}")
        return {
            'token': None,
            'active_token': None,
            'device_id': None,
            'user_role': None
        }


# ──────────────────────────────────────────────────────────
# 邮箱服务（generator.email）
# ──────────────────────────────────────────────────────────

def open_email_inbox(context, email):
    """打开 generator.email 收件箱页面"""
    page = context.new_page()
    url = f"{EMAIL_BASE_URL}/{email}"
    print(f"  📥 正在打开收件箱: {url}")
    page.goto(url, wait_until='domcontentloaded', timeout=60000)
    time.sleep(6)
    return page


def wait_for_email(inbox_page, timeout=300, keywords=('qwen', 'alibaba')):
    """轮询收件箱，等待匹配关键词的邮件。返回验证 URL。"""
    print(f"  ⏳ 正在等待验证邮件（最多 {timeout}s）...")
    deadline = time.time() + timeout
    last_count = 0

    while time.time() < deadline:
        try:
            # 刷新收件箱
            try:
                inbox_page.evaluate("""
                    [...document.querySelectorAll('a, button, span, div')]
                      .find(el => el.innerText && el.innerText.trim() === 'Refresh')
                      ?.click()
                """)
            except Exception:
                pass

            time.sleep(3)

            # 查找邮件列表项
            items = inbox_page.evaluate("""
                [...document.querySelectorAll('div.e7m.list-group-item')]
                  .filter(el => !el.className.includes('active'))
                  .map(el => ({
                    text: el.innerText.trim(),
                    html: el.outerHTML,
                  }))
                  .filter(e => e.text.length > 5)
            """) or []

            count = len(items)
            if count != last_count:
                print(f"  📨 收件箱中有 {count} 封邮件")
                last_count = count

            for item in items:
                lower = item['text'].lower()
                # 只匹配 qwen/alibaba 关键词
                if any(kw in lower for kw in keywords):
                    print(f"  🎯 找到 Qwen 邮件: {item['text'][:80]}...")

                    # 点击邮件
                    try:
                        inbox_page.evaluate(f"""
                            const html = {repr(item['html'])};
                            const items = [...document.querySelectorAll('div.e7m.list-group-item')];
                            const target = items.find(el => el.outerHTML === html);
                            if (target) target.click();
                        """)
                        time.sleep(6)
                    except Exception as click_err:
                        print(f"  ⚠️ 点击邮件失败（非致命）: {click_err}")
                        time.sleep(4)

                    # 提取验证链接
                    try:
                        links = inbox_page.evaluate("""
                            [...document.querySelectorAll('a[href]')]
                              .map(a => a.href)
                              .filter(h => h.startsWith('http'))
                              .filter(h => !h.includes('generator.email') &&
                                          !h.includes('googleads') &&
                                          !h.includes('doubleclick'))
                        """) or []
                    except Exception as e:
                        print(f"  ⚠️ 链接提取失败，准备重试: {e}")
                        time.sleep(3)
                        try:
                            links = inbox_page.evaluate("""
                                [...document.querySelectorAll('a[href]')]
                                  .map(a => a.href)
                                  .filter(h => h.startsWith('http'))
                                  .filter(h => !h.includes('generator.email') &&
                                              !h.includes('googleads') &&
                                              !h.includes('doubleclick'))
                            """) or []
                        except Exception:
                            links = []

                    # 优先选择包含 qwen/verify/activate 的链接
                    for link in links:
                        ll = link.lower()
                        if any(kw in ll for kw in ['qwen', 'verify', 'activate', 'confirm', 'alibaba']):
                            print(f"  🔗 验证链接: {link}")
                            return link

                    # 备选：返回第一个非追踪链接
                    if links:
                        print(f"  🔗 找到的第一个链接: {links[0]}")
                        return links[0]

        except Exception as e:
            print(f"  ⚠️ 收件箱检查出错: {e}")

        elapsed = int(time.time() - (deadline - timeout))
        if elapsed % 30 == 0 and elapsed > 0:
            print(f"  ⏳ 仍在等待... ({elapsed}s)")
        time.sleep(8)

    return None


# ──────────────────────────────────────────────────────────
# Qwen Registration (增强版)
# ──────────────────────────────────────────────────────────

def register_qwen(
    page,
    name,
    email,
    password,
    captcha_timeout=600,
    captcha_solver_config=None,
    label="",
):
    """
    填写并提交 Qwen 注册表单（增强版，支持验证码检测）

    返回:
        bool: 注册是否成功提交
    """
    print(f"  📝 正在注册 Qwen 账号...")

    try:
        page.goto(QWEN_REGISTER_URL, wait_until='domcontentloaded', timeout=60000)
        # 等待表单加载
        page.wait_for_selector('input[name="username"]', timeout=20000)
        time.sleep(3)

        # 填写表单，逐字段等待
        page.fill('input[name="username"]', name)
        time.sleep(0.8)
        page.fill('input[name="email"]', email)
        time.sleep(0.8)
        page.fill('input[name="password"]', password)
        time.sleep(0.8)
        page.fill('input[name="checkPassword"]', password)
        time.sleep(0.8)

        # 勾选用户条款
        try:
            page.check('input[type="checkbox"]')
            time.sleep(0.5)
        except Exception:
            pass

        # 点击提交按钮
        submit_btn = page.locator('button:has-text("Create Account"), button:has-text("创建账号")')
        submit_btn.scroll_into_view_if_needed()
        time.sleep(1)

        try:
            with page.expect_navigation(wait_until='domcontentloaded', timeout=30000):
                submit_btn.click()
        except PlaywrightTimeout:
            # 导航超时是正常的 — 表单可能已提交但没有重定向
            pass
        except Exception:
            pass

        time.sleep(5)

        # 检测是否出现验证码
        if detect_captcha(page):
            print("  🤖 检测到验证码弹窗")
            label_prefix = f"{label} " if label else ""
            manual_trace_records = None
            if captcha_solver_config is not None and captcha_solver_config.enabled:
                solver_result = solve_slider_captcha(
                    page,
                    captcha_solver_config,
                    label=label,
                    image_dir=IMAGES_DIR,
                    stop_event=STOP_EVENT,
                )
                if solver_result.ok:
                    print(f"  ✅ {label_prefix}{solver_result.message}")
                    time.sleep(3)
                elif captcha_solver_config.fallback_manual:
                    print(f"  ⚠️ {label_prefix}{solver_result.message}，改为人工处理")
                    manual_trace_records = attach_manual_trace_recorder(
                        page,
                        label=label,
                        image_dir=IMAGES_DIR,
                    )
                    captcha_completed = wait_for_captcha_completion(
                        page,
                        email,
                        password,
                        name,
                        timeout=captcha_timeout,
                    )
                    if not captcha_completed:
                        dump_manual_trace_recording(
                            page,
                            manual_trace_records,
                            label=label,
                            image_dir=IMAGES_DIR,
                        )
                        print("  ❌ 验证码未完成或超时")
                        return False
                    dump_manual_trace_recording(
                        page,
                        manual_trace_records,
                        label=label,
                        image_dir=IMAGES_DIR,
                    )
                    time.sleep(3)
                else:
                    print(f"  ❌ {label_prefix}{solver_result.message}")
                    return False
            else:
                manual_trace_records = attach_manual_trace_recorder(
                    page,
                    label=label,
                    image_dir=IMAGES_DIR,
                )
                # 等待用户手动完成验证码
                captcha_completed = wait_for_captcha_completion(
                    page,
                    email,
                    password,
                    name,
                    timeout=captcha_timeout,
                )

                if not captcha_completed:
                    dump_manual_trace_recording(
                        page,
                        manual_trace_records,
                        label=label,
                        image_dir=IMAGES_DIR,
                    )
                    print("  ❌ 验证码未完成或超时")
                    return False

                # 验证码完成后，等待页面跳转
                dump_manual_trace_recording(
                    page,
                    manual_trace_records,
                    label=label,
                    image_dir=IMAGES_DIR,
                )
                time.sleep(3)

        # 检查注册结果
        body = page.evaluate('document.body.innerText') or ''

        # 成功标志
        if '待激活' in body or '激活账号' in body or 'pending activation' in body.lower() or 'verification email' in body.lower():
            print("  ✅ 注册已提交，等待邮箱验证")
            return True

        # 错误标志
        if 'error' in body.lower() or 'failed' in body.lower() or '错误' in body:
            print(f"  ❌ 注册出错: {body[:200]}")
            return False

        # 假设成功（表单已消失）
        if 'create account' not in body.lower() and '创建账号' not in body:
            print("  ✅ 注册可能已提交")
            return True

        print(f"  ⚠️ 页面状态异常: {body[:200]}")
        if any(marker in body for marker in ("请完成以下操作", "验证您是真人", "访问验证")):
            print("  ❌ 验证码仍未真正完成")
        return False

    except PlaywrightTimeout as e:
        print(f"  ⚠️ 操作超时: {str(e)[:100]}")
        try:
            body = page.evaluate('document.body.innerText') or ''
            if 'create account' not in body.lower():
                print("  ✅ 虽然发生超时，但表单可能已提交")
                return True
        except Exception:
            pass
        return False
    except Exception as e:
        print(f"  ❌ 表单填写出错: {e}")
        try:
            page.screenshot(path=f'{IMAGES_DIR}/qwen_form_error.png')
        except Exception:
            pass
        return False


# ──────────────────────────────────────────────────────────
# Main Loop（并发版）
# ──────────────────────────────────────────────────────────

def run_single_account(account_index, total_accounts, args, proxy_str):
    """执行单个账号注册任务；每个线程独立创建 Playwright 实例。"""
    label = f"[账号 {account_index}/{total_accounts}]"
    print(f"\n{'═'*60}")
    print(f"🔢 {label}")
    print(f"{'═'*60}")

    proxy_dict = ProxyRotator.parse_proxy(proxy_str) if proxy_str else None
    if proxy_dict:
        parts = proxy_str.split(':')
        print(f"  🌐 {label} 浏览器代理: {parts[0]}:{parts[1]}")
    else:
        print(f"  🌐 {label} 未使用浏览器代理（直连）")

    provider = None
    browser = None
    qwen = None

    try:
        if STOP_EVENT.is_set():
            print(f"  🛑 {label} 已收到停止请求，跳过")
            return False
        with sync_playwright() as p:
            if STOP_EVENT.is_set():
                print(f"  🛑 {label} 已收到停止请求，跳过")
                return False
            try:
                browser = p.chromium.launch(
                    headless=HEADLESS,
                    args=BROWSER_ARGS,
                    proxy=proxy_dict,
                )
            except Exception as e:
                print(f"  ❌ {label} 浏览器启动失败: {e}")
                return False

            try:
                context = browser.new_context(
                    user_agent=USER_AGENT,
                    viewport={'width': 1280, 'height': 800},
                )

                current_ip, country = get_current_ip()
                print(f"  📡 {label} 当前 IP: {current_ip} ({country})")

                provider = EmailProviderFactory.create(
                    provider_type=args.email_provider,
                    verbose=args.verbose,
                    api_proxy=args.api_proxy,
                    context=context,
                )

                used_emails_store = UsedEmailsStore("used_emails.json")
                max_email_retries = 1 if isinstance(provider, GeneratorEmailProvider) else 3
                email = None
                for _attempt in range(max_email_retries):
                    candidate = provider.create_inbox()
                    if used_emails_store.claim(candidate):
                        email = candidate
                        break
                    print(f"  ⚠️ {label} 邮箱已使用: {candidate}")
                    if isinstance(provider, GeneratorEmailProvider):
                        break

                if not email:
                    raise EmailCreationError("多次创建邮箱均重复，跳过当前账号")

                first_name = gen_first_name()
                password = gen_password()
                name = gen_name(first_name)

                print(f"  📧 {label} 邮箱:    {email}")
                print(f"  👤 {label} 用户名:  {name}")
                print(f"  🔑 {label} 密码:    {password}")

                qwen = context.new_page()
                install_aliyun_callback_probe(qwen)
                if getattr(args, "captcha_force_verify_success", False):
                    install_aliyun_verify_success_route(qwen)
                if STOP_EVENT.is_set():
                    print(f"  🛑 {label} 已收到停止请求，跳过注册")
                    return False
                registered = register_qwen(
                    qwen,
                    name,
                    email,
                    password,
                    captcha_timeout=args.captcha_timeout,
                    captcha_solver_config=build_captcha_solver_config(args),
                    label=label,
                )
                if not registered:
                    print(f"  ❌ {label} 注册失败，跳过...")
                    return False

                verify_url = provider.get_activation_link(timeout=300)

                print(f"  🔄 {label} 正在打开验证链接...")
                qwen.goto(verify_url, wait_until='domcontentloaded', timeout=30000)
                time.sleep(5)

                qwen.screenshot(path=f'{IMAGES_DIR}/qwen_verified_{account_index}.png')

                print(f"  🔑 {label} 正在提取认证令牌...")
                tokens = extract_tokens(qwen)

                if tokens['token']:
                    print(f"  ✅ {label} 已提取认证令牌: {tokens['token'][:80]}...")
                else:
                    print(f"  ⚠️  {label} 未找到认证令牌（可能需要等待更久）")

                body = qwen.evaluate('document.body.innerText') or ''
                print(f"  📋 {label} 验证结果: {body[:200]}")

                save_account(
                    email=email,
                    password=password,
                    name=name,
                    ip=current_ip,
                    country=country,
                    token=tokens.get('token'),
                    active_token=tokens.get('active_token'),
                    device_id=tokens.get('device_id'),
                    user_role=tokens.get('user_role', 'user'),
                )
                maybe_sync_account_to_qwen2api(
                    email=email,
                    password=password,
                    token=tokens.get('token'),
                    config=build_qwen2api_sync_config(args),
                    label=label,
                )

                print(f"  ✅ {label} 已验证并保存！")
                return True

            except EmailProviderError as e:
                print(f"  ❌ {label} 邮箱服务错误: {e}")
                print(f"  ⏭️ {label} 严格模式：跳过当前账号，不自动降级")
            except Exception as e:
                print(f"  ❌ {label} 出错: {e}")
            finally:
                if qwen is not None:
                    try:
                        qwen.close()
                    except Exception:
                        pass
                if provider is not None:
                    try:
                        provider.cleanup()
                    except Exception:
                        pass
                if browser is not None:
                    try:
                        browser.close()
                    except Exception:
                        pass
    except Exception as e:
        print(f"  ❌ {label} 执行异常: {e}")

    return False


def main():
    """主函数 - 自动化注册流程"""

    STOP_EVENT.clear()
    global INTERRUPT_COUNT
    INTERRUPT_COUNT = 0
    previous_sigint = None
    try:
        previous_sigint = signal.getsignal(signal.SIGINT)

        def _handle_sigint(_signum, _frame):
            request_shutdown("收到 Ctrl+C")

        signal.signal(signal.SIGINT, _handle_sigint)
    except Exception:
        previous_sigint = None

    args = parse_args()
    num_accounts = args.count
    if num_accounts is None:
        try:
            num_accounts = positive_int(input("📊 要创建多少个账号？ "))
        except (argparse.ArgumentTypeError, ValueError, EOFError):
            print("❌ 数量无效")
            sys.exit(1)

    os.makedirs(IMAGES_DIR, exist_ok=True)

    proxy_rotator = ProxyRotator(PROXY_FILE)
    proxy_assignments = [proxy_rotator.get_next() for _ in range(num_accounts)]

    print(f"\n🎯 准备创建 {num_accounts} 个 Qwen 账号...")
    print(f"📮 邮箱服务: {args.email_provider}")
    if args.api_proxy and not is_generator_provider(args.email_provider):
        print(f"🔌 API 代理: {args.api_proxy}")
    print(f"🚦 并发数量: {args.concurrency}")
    minutes = args.captcha_timeout // 60
    minute_text = f" / {minutes}分钟" if minutes else ""
    print(f"🤖 滑块等待: {args.captcha_timeout}s{minute_text}")
    if args.captcha_record_only:
        print("🧠 滑块自动处理: 已禁用（只录制人工轨迹）")
    elif args.captcha_solver == "ai":
        fallback_text = "启用" if not args.no_captcha_ai_fallback_manual else "禁用"
        print(f"🧠 滑块自动处理: 已启用（优先 ddddocr，AI 模型 {args.captcha_ai_model}，人工回退{fallback_text}）")
    else:
        print("🧠 滑块自动处理: 未启用")
    if args.captcha_record_trace or args.captcha_record_only:
        print("🎥 滑块轨迹录制: 已启用（人工通过后会保存 manual_trace_*.json）")
    if args.captcha_replay_trace:
        print(f"🎞️ 滑块轨迹重放: {args.captcha_replay_trace}")
    print(f"🖱️ 滑块拖动后端: {args.captcha_drag_backend}")
    print(f"🧭 滑块拖动策略: {args.captcha_drag_strategy}")
    if args.captcha_callback_bypass:
        print("🧪 本地靶场回调实验: 已启用")
    if args.captcha_force_verify_success:
        print("🧪 本地靶场 Verify 响应替换: 已启用")
    if args.sync_qwen2api:
        print(f"🔁 qwen2API 同步: 已启用（{args.qwen2api_base_url}）")
    else:
        print("🔁 qwen2API 同步: 未启用")
    print("🔒 严格模式: 已启用（不会自动降级）\n")

    success_count = 0
    try:
        with ThreadPoolExecutor(max_workers=args.concurrency) as executor:
            futures = {
                executor.submit(
                    run_single_account,
                    index,
                    num_accounts,
                    args,
                    proxy_assignments[index - 1],
                ): index
                for index in range(1, num_accounts + 1)
            }
            try:
                success_count = collect_account_futures(futures, num_accounts)
            finally:
                if STOP_EVENT.is_set():
                    for future in futures:
                        future.cancel()
                    executor.shutdown(wait=False, cancel_futures=True)
                    print("🛑 已停止等待新任务完成，正在关闭已启动的浏览器...")

        print(f"\n{'═'*60}")
        print(f"🎉 完成: 已创建 {success_count}/{num_accounts} 个账号")
        print("💾 结果保存到:")
        print(f"   - {OUTPUT_FILE_TXT}（文本格式）")
        print(f"   - {OUTPUT_FILE_JSON}（JSON 数组格式）")
        print(f"{'═'*60}")
    finally:
        if previous_sigint is not None:
            try:
                signal.signal(signal.SIGINT, previous_sigint)
            except Exception:
                pass


if __name__ == "__main__":
    main()
