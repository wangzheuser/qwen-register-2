#!/usr/bin/env python3
"""
Qwen 自动注册脚本 v3 - 增强 Token 提取
使用临时邮箱自动完成 Qwen 注册与验证。
支持浏览器代理、验证码检测和认证令牌提取。

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
import urllib.parse
import uuid
from contextlib import contextmanager
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, wait, FIRST_COMPLETED
from dataclasses import dataclass, field
from datetime import datetime
import httpx
from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeout

try:  # pragma: no cover - 依赖缺失时保留线程级兜底锁。
    import portalocker  # type: ignore
except ImportError:  # pragma: no cover
    portalocker = None  # type: ignore

from captcha_solvers.slider_lock import SliderLockTimeout, acquire_foreground_window_lock, acquire_slider_lock
from captcha_solvers.window_focus import ensure_page_foreground, hold_page_topmost, minimize_page_window
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
    Qwen2ApiAsyncSyncer,
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
IMAGES_DIR = "images"
HEADLESS = False  # 必须为 False 以支持手动完成验证码
RUN_LOG_MAX_BYTES = 128 * 1024 * 1024
POST_CAPTCHA_SUBMISSION_TIMEOUT = 8.0
DDDDOCR_POST_CAPTCHA_SUBMISSION_TIMEOUT = 1.5

BROWSER_ARGS = [
    '--disable-blink-features=AutomationControlled',
    '--no-sandbox',
    '--disable-dev-shm-usage',
]
BROWSER_WINDOW_WIDTH = 1280
BROWSER_WINDOW_HEIGHT = 800
BROWSER_WINDOW_POSITION_BASE_X = 20
BROWSER_WINDOW_POSITION_BASE_Y = 20
BROWSER_WINDOW_POSITION_STEP_X = 55
BROWSER_WINDOW_POSITION_STEP_Y = 35


def build_browser_launch_args(account_index=1):
    """构造 Chromium 启动参数；每个账号使用不同窗口位置，降低 HWND 定位歧义。"""
    try:
        slot = (max(1, int(account_index)) - 1) % 10
    except Exception:
        slot = 0
    x = BROWSER_WINDOW_POSITION_BASE_X + slot * BROWSER_WINDOW_POSITION_STEP_X
    y = BROWSER_WINDOW_POSITION_BASE_Y + slot * BROWSER_WINDOW_POSITION_STEP_Y
    return [
        *BROWSER_ARGS,
        f"--window-size={BROWSER_WINDOW_WIDTH},{BROWSER_WINDOW_HEIGHT}",
        f"--window-position={x},{y}",
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
WORKER_START_INTERVAL_SECONDS = 0.1
QWEN2API_ASYNC_SYNCER = Qwen2ApiAsyncSyncer()


@dataclass
class AccountRunResult:
    """单个账号 worker 的执行结果。"""

    success: bool
    duration_seconds: float | None = None

    def __bool__(self):
        return self.success


@dataclass
class AccountRunSummary:
    """账号批量执行汇总。"""

    success_count: int = 0
    success_durations: list[float] = field(default_factory=list)

    def __eq__(self, other):
        if isinstance(other, int):
            return self.success_count == other
        return super().__eq__(other)



class AccountStageTimer:
    """记录单账号阶段耗时，用于并发性能排查。"""

    def __init__(self, label, *, clock=None, clock_values=None):
        self.label = label
        if clock_values is not None:
            iterator = iter(clock_values)
            self._clock = lambda: next(iterator)
        else:
            self._clock = clock or time.perf_counter
        self._last = float(self._clock())
        self._stages = []

    def mark(self, name):
        now = float(self._clock())
        self._stages.append((str(name), max(0.0, now - self._last)))
        self._last = now

    def print_summary(self, *, success):
        if not self._stages:
            return
        status = "成功" if success else "失败"
        print(f"  ⏱️ {self.label} 耗时拆分（{status}）:")
        for name, seconds in self._stages:
            print(f"     - {name}: {seconds:.1f}s")


def post_captcha_submission_timeout(captcha_solver_config=None):
    """验证码通过后的短确认窗口；ddddocr 默认更短，避免串行滑块后继续空等。"""
    mode = str(getattr(captcha_solver_config, "mode", "") or "").strip().lower()
    if mode == "ddddocr":
        raw = os.getenv("DDDDOCR_POST_CAPTCHA_SUBMISSION_TIMEOUT", "").strip()
        if raw:
            try:
                return max(0.0, float(raw))
            except ValueError:
                pass
        return DDDDOCR_POST_CAPTCHA_SUBMISSION_TIMEOUT
    raw = os.getenv("POST_CAPTCHA_SUBMISSION_TIMEOUT", "").strip()
    if raw:
        try:
            return max(0.0, float(raw))
        except ValueError:
            pass
    return POST_CAPTCHA_SUBMISSION_TIMEOUT

def format_success_rate(success_count, total_accounts):
    if total_accounts <= 0:
        return "0.00%"
    return f"{(success_count / total_accounts) * 100:.2f}%"


def format_duration_seconds(seconds):
    total_seconds = max(0, int(round(float(seconds))))
    minutes, seconds = divmod(total_seconds, 60)
    if minutes:
        return f"{minutes}分{seconds:02d}秒"
    return f"{seconds}秒"


def format_average_success_duration(summary):
    durations = list(getattr(summary, "success_durations", []) or [])
    if not durations:
        return "无成功账号"
    average_seconds = sum(durations) / len(durations)
    return format_duration_seconds(average_seconds)
START_STOP_FILE_ENV = "QWEN_REGISTER_STOP_FILE"
START_MANAGED_ENV = "QWEN_REGISTER_MANAGED_BY_START"


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
        choices=["mailtm", "mail.tm", "mailporary", "gonebox", "tempmail_lol", "tempmail.lol", "freecustom", "freecustom.email", "generator.email", "generator"],
        help="邮箱服务类型，默认: mailtm",
    )
    parser.add_argument(
        "--api-proxy",
        default=None,
        help="API 请求代理地址，仅 Mail.tm / Mailporary 使用，如 http://127.0.0.1:7890",
    )
    parser.add_argument(
        "--browser-proxy",
        default="",
        help="浏览器代理地址，可包含 {uuid} 模板，如 http://user.{uuid}:pass@127.0.0.1:9200",
    )
    parser.add_argument(
        "--concurrency",
        type=concurrency_value,
        default=1,
        help="并发账号数量，范围 1-10，默认: 1",
    )
    parser.add_argument(
        "--account-retries",
        type=positive_int,
        default=1,
        help="单个账号编号失败后的补偿重试次数，默认: 1（不额外重试）",
    )
    parser.add_argument(
        "--captcha-timeout",
        type=positive_int,
        default=600,
        help="滑块验证码等待秒数，默认: 600",
    )
    parser.add_argument(
        "--captcha-solver",
        choices=["ddddocr", "manual", "ai"],
        default="ddddocr",
        help="滑块验证码处理方式：ddddocr=本地自动识别，ai=远程滑块 AI，manual=人工等待；默认: ddddocr",
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
        choices=["auto", "closed_loop", "success_profile", "fast_quadratic", "quadratic", "human", "ratio_human", "scaled"],
        default=os.getenv("CAPTCHA_DRAG_STRATEGY", "fast_quadratic"),
        help="滑块拖动策略；默认: fast_quadratic",
    )
    parser.add_argument(
        "--captcha-target-right-bias",
        type=float,
        default=None,
        help="滑块目标位置右侧微调像素；默认不额外微调，启动脚本可按当前成功配置传入",
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
    parser.add_argument(
        "--log-file",
        default="",
        help="运行日志文件路径；默认写入单个 logs/qwenv4.log，并在超过 128MB 时自动丢弃最旧内容",
    )
    parser.add_argument("--camoufox-worker", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--account-index", type=positive_int, default=1, help=argparse.SUPPRESS)
    parser.add_argument("--total-accounts", type=positive_int, default=1, help=argparse.SUPPRESS)
    parser.add_argument(
        "--camoufox-account-timeout",
        type=positive_int,
        default=int(os.getenv("CAMOUFOX_ACCOUNT_TIMEOUT", "180")),
        help=argparse.SUPPRESS,
    )
    args = parser.parse_args(argv)
    if args.captcha_drag_backend is None:
        args.captcha_drag_backend = os.getenv("CAPTCHA_DRAG_BACKEND") or ("os" if args.captcha_solver in {"ai", "ddddocr"} else "playwright")
    return args


class _ThreadAwareTee:
    """把终端输出同步写入文件，文件内每行带时间和线程信息。"""

    def __init__(self, stream, log_file, stream_name, path, max_bytes):
        self._stream = stream
        self._log_file = log_file
        self._stream_name = stream_name
        self._path = Path(path)
        self._max_bytes = max_bytes
        self._lock = threading.Lock()

    def write(self, data):
        self._stream.write(data)
        if not data:
            return 0
        text = str(data)
        with self._lock:
            for part in text.splitlines(True):
                if part in {"\n", "\r\n", "\r"}:
                    self._log_file.write(part)
                    continue
                now = datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
                thread = threading.current_thread()
                self._log_file.write(f"{now} [{thread.name}:{thread.ident}] [{self._stream_name}] {part}")
            self._log_file.flush()
            trim_open_log_file_to_limit(self._log_file, self._path, self._max_bytes)
        return len(data)

    def flush(self):
        self._stream.flush()
        with self._lock:
            self._log_file.flush()

    def writelines(self, lines):
        for line in lines:
            self.write(line)

    def __getattr__(self, name):
        return getattr(self._stream, name)


class RunLoggingState:
    def __init__(self, path, log_file, stdout, stderr):
        self.path = path
        self.log_file = log_file
        self.stdout = stdout
        self.stderr = stderr
        self.closed = False


def _default_log_file(script_stem):
    return Path("logs") / f"{script_stem}.log"


def trim_log_file_to_limit(path, max_bytes=RUN_LOG_MAX_BYTES):
    """把日志文件裁剪到最大字节数，保留末尾最新内容。"""
    log_path = Path(path)
    if max_bytes <= 0 or not log_path.exists():
        return
    size = log_path.stat().st_size
    if size <= max_bytes:
        return
    with open(log_path, "rb") as handle:
        handle.seek(max(0, size - max_bytes))
        data = handle.read(max_bytes)
    with open(log_path, "wb") as handle:
        handle.write(data)


def trim_open_log_file_to_limit(log_file, path, max_bytes=RUN_LOG_MAX_BYTES):
    """裁剪当前打开的日志文件并把写入位置移回末尾。"""
    log_file.flush()
    trim_log_file_to_limit(path, max_bytes=max_bytes)
    log_file.seek(0, os.SEEK_END)


def enable_run_logging(args, script_stem="qwenv4"):
    """开启运行日志 tee；终端原样显示，文件包含完整明文和线程信息。"""
    raw_path = str(getattr(args, "log_file", "") or "").strip()
    log_path = Path(raw_path) if raw_path else _default_log_file(script_stem)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    trim_log_file_to_limit(log_path)
    log_file = open(log_path, "a", encoding="utf-8", buffering=1)
    state = RunLoggingState(log_path, log_file, sys.stdout, sys.stderr)
    sys.stdout = _ThreadAwareTee(state.stdout, log_file, "stdout", log_path, RUN_LOG_MAX_BYTES)
    sys.stderr = _ThreadAwareTee(state.stderr, log_file, "stderr", log_path, RUN_LOG_MAX_BYTES)
    return state


def close_run_logging(state):
    """关闭运行日志 tee 并恢复 stdout/stderr。"""
    if state is None or getattr(state, "closed", False):
        return
    try:
        sys.stdout.flush()
        sys.stderr.flush()
    finally:
        sys.stdout = state.stdout
        sys.stderr = state.stderr
        state.log_file.close()
        state.closed = True


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
    captcha_solver = "manual" if getattr(args, "captcha_record_only", False) else getattr(args, "captcha_solver", "ddddocr")
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
    target_right_bias = getattr(args, "captcha_target_right_bias", None)
    if target_right_bias is not None:
        os.environ["CAPTCHA_TARGET_RIGHT_BIAS"] = str(float(target_right_bias))
    if getattr(args, "captcha_callback_bypass", False):
        os.environ["CAPTCHA_CALLBACK_BYPASS"] = "1"
    if getattr(args, "captcha_force_verify_success", False):
        os.environ["CAPTCHA_FORCE_VERIFY_SUCCESS"] = "1"
    if captcha_solver == "ddddocr":
        attempts = 3
        fallback_manual = False
    elif captcha_solver == "ai":
        attempts = getattr(args, "captcha_ai_attempts", DEFAULT_CAPTCHA_AI_ATTEMPTS)
        fallback_manual = False
    else:
        attempts = getattr(args, "captcha_ai_attempts", DEFAULT_CAPTCHA_AI_ATTEMPTS)
        fallback_manual = False
    return CaptchaSolverConfig(
        enabled=captcha_solver in {"ddddocr", "ai"},
        mode=captcha_solver,
        base_url=getattr(args, "captcha_ai_base_url", DEFAULT_CAPTCHA_AI_BASE_URL),
        api_key=getattr(args, "captcha_ai_api_key", os.getenv("CAPTCHA_AI_API_KEY", "")),
        model=getattr(args, "captcha_ai_model", DEFAULT_CAPTCHA_AI_MODEL),
        timeout=getattr(args, "captcha_ai_timeout", DEFAULT_CAPTCHA_AI_TIMEOUT),
        attempts=attempts,
        fallback_manual=fallback_manual,
        overall_timeout=getattr(args, "captcha_timeout", 0) if captcha_solver == "ai" else 0,
    )


INTERRUPT_FORCE_EXIT_SECONDS = 5


def _force_exit_after_interrupt(timeout=None):
    """中断后给清理流程一个短窗口，避免浏览器/驱动卡死导致 Ctrl+C 无法退出。"""
    delay = INTERRUPT_FORCE_EXIT_SECONDS if timeout is None else timeout
    try:
        time.sleep(max(0.0, float(delay)))
    except Exception:
        time.sleep(5)
    print("\n🛑 中断清理超时，强制结束当前进程。", flush=True)
    os._exit(130)


def request_shutdown(reason="收到中断信号", force_exit=True):
    """请求所有账号任务尽快停止。"""
    global INTERRUPT_COUNT
    INTERRUPT_COUNT += 1
    if not STOP_EVENT.is_set():
        print(f"\n🛑 {reason}，正在请求所有任务停止...", flush=True)
        if force_exit:
            print("   若浏览器或底层驱动未及时退出，将在数秒后自动强制结束。", flush=True)
            threading.Thread(target=_force_exit_after_interrupt, daemon=True).start()
        else:
            print("   已由启动脚本接管超时兜底，将优先等待当前浏览器清理完成。", flush=True)
    elif INTERRUPT_COUNT >= 2:
        print("\n🛑 再次收到中断，强制结束当前进程。", flush=True)
        os._exit(130)
    STOP_EVENT.set()


def start_parent_stop_file_watcher():
    """监控 start.ps1 写入的停止信号文件，用于干净中断。"""
    stop_file = os.getenv(START_STOP_FILE_ENV, "").strip()
    if not stop_file:
        return None

    def _watch():
        while not STOP_EVENT.is_set():
            if os.path.exists(stop_file):
                request_shutdown("收到启动脚本停止请求", force_exit=False)
                return
            time.sleep(0.2)

    watcher = threading.Thread(target=_watch, name="start-stop-file-watcher", daemon=True)
    watcher.start()
    return watcher

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
    summary = AccountRunSummary()
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
                result = future.result()
                if result:
                    summary.success_count += 1
                    duration = getattr(result, "duration_seconds", None)
                    if duration is not None:
                        summary.success_durations.append(float(duration))
            except KeyboardInterrupt:
                request_shutdown("收到 Ctrl+C")
                for item in pending:
                    try:
                        item.cancel()
                    except Exception:
                        pass
                return summary
            except Exception as e:
                print(f"  ❌ [账号 {index}/{total_accounts}] 任务异常: {e}")
    return summary


def submit_account_futures(
    executor,
    total_accounts,
    args,
    worker,
    proxy_str=None,
    start_interval=WORKER_START_INTERVAL_SECONDS,
):
    """按账号顺序提交 worker，并在并发启动之间错峰等待。"""
    futures = {}
    for index in range(1, total_accounts + 1):
        if STOP_EVENT.is_set():
            break
        future = executor.submit(
            worker,
            index,
            total_accounts,
            args,
            proxy_str,
        )
        futures[future] = index
        if index < total_accounts and start_interval > 0:
            if not sleep_interruptible(start_interval):
                break
    return futures


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


def maybe_sync_account_to_qwen2api_async(email, password, token, config, label=""):
    """把 qwen2API 同步提交到后台队列；本地保存成功不再等待远端同步。"""
    if not config.enabled:
        return None
    return QWEN2API_ASYNC_SYNCER.submit(
        email=email,
        password=password,
        token=token,
        config=config,
        label=label,
    ) is not None


def flush_qwen2api_async_sync(timeout=30):
    """等待已提交的 qwen2API 后台同步完成。"""
    return QWEN2API_ASYNC_SYNCER.flush(timeout=timeout)

# ──────────────────────────────────────────────────────────
# 浏览器代理
# ──────────────────────────────────────────────────────────

def build_browser_proxy(proxy_template):
    """将浏览器代理模板解析为 Playwright/Camoufox 兼容配置。"""
    raw_template = str(proxy_template or "").strip()
    if not raw_template:
        return {"proxy": None, "display": "", "resolved": "", "username": ""}

    resolved = raw_template.replace("{uuid}", uuid.uuid4().hex)
    parsed = urllib.parse.urlparse(resolved)
    if parsed.scheme not in {"http", "https", "socks5"} or not parsed.hostname or not parsed.port:
        raise ValueError("浏览器代理格式无效，请使用 http://user:pass@host:port 或 http://host:port")

    server = f"{parsed.scheme}://{parsed.hostname}:{parsed.port}"
    proxy = {"server": server}
    username = urllib.parse.unquote(parsed.username or "")
    password = urllib.parse.unquote(parsed.password or "")
    if username:
        proxy["username"] = username
        proxy["password"] = password
        display = f"{parsed.scheme}://{username}@{parsed.hostname}:{parsed.port}"
    else:
        display = server

    return {
        "proxy": proxy,
        "display": display,
        "resolved": resolved,
        "username": username,
    }

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
    http_client = client or httpx.Client(timeout=3.0)
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
        body_text = read_page_body_text(page)
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
                body_text = read_page_body_text(page)
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


def registration_submission_detected(body_text):
    """判断注册提交是否已进入等待邮箱验证状态。"""
    text = body_text or ""
    lower = text.lower()
    return (
        "待激活" in text
        or "激活账号" in text
        or "pending activation" in lower
        or "verification email" in lower
    )


def registration_form_still_visible(body_text):
    """判断注册提交/滑块后是否仍停留在注册表单。"""
    text = body_text or ""
    lower = text.lower()
    return (
        "create account" in lower
        or "创建账号" in text
        or "request aborted" in lower
    )


def registration_form_input_visible(page, timeout=500):
    """快速判断注册表单输入框是否仍可见，避免滑块后误走 20s 慢等待重提交流程。"""
    try:
        return bool(page.locator('input[name="username"]').first.is_visible(timeout=timeout))
    except AttributeError:
        # 测试替身或极简 page 可能没有 locator；保守返回 True，沿用原重提交流程。
        return True
    except Exception:
        return False


_TRANSIENT_BODY_READ_MARKERS = (
    "document.body is null",
    "Execution context was destroyed",
    "Cannot find context with specified id",
    "Target page, context or browser has been closed",
    "Page.evaluate: Target closed",
)


def _is_transient_body_read_error(error):
    message = str(error)
    return any(marker in message for marker in _TRANSIENT_BODY_READ_MARKERS)


def read_page_body_text(page, timeout=0.0, interval=0.15):
    """安全读取页面正文；兼容 Camoufox/Firefox 导航瞬间 document.body 为空。"""
    deadline = time.monotonic() + max(0.0, float(timeout or 0.0))
    script = "() => document.body ? document.body.innerText : ''"
    while True:
        if STOP_EVENT.is_set():
            return ""
        try:
            return page.evaluate(script) or ""
        except Exception as exc:
            if not _is_transient_body_read_error(exc):
                return ""
            if time.monotonic() >= deadline:
                return ""
            wait_seconds = min(float(interval), max(0.0, deadline - time.monotonic()))
            if wait_seconds <= 0:
                return ""
            if not sleep_interruptible(wait_seconds):
                return ""


def wait_for_registration_submission(page, timeout=3.0, interval=0.25):
    """验证码通过后短轮询注册提交状态，避免无条件固定等待。"""
    deadline = time.monotonic() + max(0.0, float(timeout))
    while True:
        if STOP_EVENT.is_set():
            return False
        try:
            body = read_page_body_text(page, timeout=1.0, interval=0.2)
            if registration_submission_detected(body):
                return True
        except Exception:
            pass
        if time.monotonic() >= deadline:
            return False
        wait_seconds = min(float(interval), max(0.0, deadline - time.monotonic()))
        if not sleep_interruptible(wait_seconds):
            return False


def wait_for_token_extraction(page, timeout=5.0, interval=0.25):
    """打开验证链接后短轮询 token，token 出现即返回。"""
    deadline = time.monotonic() + max(0.0, float(timeout))
    last_tokens = {
        "token": None,
        "active_token": None,
        "device_id": None,
        "user_role": "user",
    }
    while True:
        if STOP_EVENT.is_set():
            return last_tokens
        try:
            last_tokens = extract_tokens(page)
            if last_tokens.get("token"):
                return last_tokens
        except Exception:
            pass
        if time.monotonic() >= deadline:
            return last_tokens
        wait_seconds = min(float(interval), max(0.0, deadline - time.monotonic()))
        if not sleep_interruptible(wait_seconds):
            return last_tokens


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

def submit_registration_form_background(page, name, email, password):
    """用 DOM 合成事件后台填写并提交注册表单，避免并发窗口抢占前台焦点。"""
    try:
        form_wait_timeout = int(os.getenv("REGISTRATION_FORM_WAIT_TIMEOUT_MS", "2500"))
    except Exception:
        form_wait_timeout = 2500
    page.wait_for_selector('input[name="username"]', timeout=max(500, min(form_wait_timeout, 20000)))
    script = """
    ({ name, email, password }) => {
      const setInputValue = (selector, value) => {
        const element = document.querySelector(selector);
        if (!element) {
          throw new Error(`缺少表单字段: ${selector}`);
        }
        const descriptor = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value');
        descriptor.set.call(element, value);
        element.dispatchEvent(new Event('input', { bubbles: true }));
        element.dispatchEvent(new Event('change', { bubbles: true }));
      };

      setInputValue('input[name="username"]', name);
      setInputValue('input[name="email"]', email);
      setInputValue('input[name="password"]', password);
      setInputValue('input[name="checkPassword"]', password);

      const checkbox = document.querySelector('input[type="checkbox"]');
      if (checkbox) {
        const checkedDescriptor = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'checked');
        checkbox.dispatchEvent(new MouseEvent('click', { bubbles: true, cancelable: true, view: window }));
        checkedDescriptor.set.call(checkbox, true);
        checkbox.dispatchEvent(new Event('input', { bubbles: true }));
        checkbox.dispatchEvent(new Event('change', { bubbles: true }));
      }

      const buttons = Array.from(document.querySelectorAll('button'));
      const submitButton = buttons.find(button => {
        const text = (button.innerText || button.textContent || '').trim().toLowerCase();
        return text.includes('create account') || text.includes('创建账号');
      }) || buttons.find(button => button.type === 'submit');
      if (!submitButton) {
        throw new Error('缺少创建账号按钮');
      }
      submitButton.disabled = false;
      submitButton.removeAttribute('disabled');
      submitButton.setAttribute('aria-disabled', 'false');
      submitButton.dispatchEvent(new MouseEvent('mouseover', { bubbles: true, cancelable: true, view: window }));
      submitButton.dispatchEvent(new MouseEvent('mousedown', { bubbles: true, cancelable: true, view: window }));
      submitButton.dispatchEvent(new MouseEvent('mouseup', { bubbles: true, cancelable: true, view: window }));
      submitButton.dispatchEvent(new MouseEvent('click', { bubbles: true, cancelable: true, view: window }));
      if (typeof submitButton.click === 'function') {
        submitButton.click();
      }
      return true;
    }
    """
    return bool(page.evaluate(script, {"name": name, "email": email, "password": password}))


def goto_register_page_with_retry(page, attempts=3):
    """打开注册页；Camoufox/Firefox 代理场景下偶发 NS_ERROR_NET_INTERRUPT，轻量重试。"""
    last_error = None
    total = max(1, int(attempts))
    transient_markers = (
        "NS_ERROR_NET_INTERRUPT",
        "NS_BINDING_ABORTED",
        "net::ERR_ABORTED",
    )
    for attempt in range(1, total + 1):
        try:
            page.goto(QWEN_REGISTER_URL, wait_until='domcontentloaded', timeout=60000)
            return True
        except Exception as e:
            last_error = e
            message = str(e)
            if attempt >= total or not any(marker in message for marker in transient_markers):
                raise
            print(f"  ⚠️ 注册页导航被中断，正在重试 ({attempt}/{total}): {message[:120]}")
            sleep_interruptible(0.5)
    if last_error is not None:
        raise last_error
    return False



def submit_registration_form_with_retry(page, name, email, password, *, max_attempts=2):
    """提交注册表单；Camoufox 偶发页面未渲染表单时重进注册页再试一次。"""
    attempts = max(1, int(max_attempts or 1))
    last_error = None
    for attempt in range(1, attempts + 1):
        try:
            return submit_registration_form_background(page, name=name, email=email, password=password)
        except Exception as exc:
            last_error = exc
            if attempt >= attempts:
                break
            print(f"  ⚠️ 注册表单暂不可用，重新打开注册页后重试 ({attempt}/{attempts - 1}): {str(exc)[:100]}")
            goto_register_page_with_retry(page)
            sleep_interruptible(0.2)
    if last_error is not None:
        raise last_error
    return False



def wait_for_captcha_or_submission_after_submit(page, timeout=5.0, interval=0.25):
    """提交注册表单后短轮询验证码/提交状态，避免无条件固定等待。"""
    deadline = time.monotonic() + max(0.0, float(timeout))
    while True:
        if STOP_EVENT.is_set():
            return "stopped"
        try:
            if detect_captcha(page):
                return "captcha"
        except Exception:
            pass
        try:
            body = read_page_body_text(page)
            if registration_submission_detected(body):
                return "submitted"
        except Exception:
            pass
        if time.monotonic() >= deadline:
            return "timeout"
        sleep_interruptible(min(float(interval), max(0.0, deadline - time.monotonic())))


def focus_page_for_slider(page, label="", attempts=3, delay=0.3):
    """激活滑块页面并确认可见/焦点状态，避免 OS 鼠标拖到错误窗口。"""
    prefix = f"{label} " if label else ""
    total_attempts = max(1, int(attempts))
    last_state = None
    use_os_focus = os.name == "nt" and os.getenv("CAPTCHA_DRAG_BACKEND", "playwright").strip().lower() == "os"
    for attempt in range(1, total_attempts + 1):
        print(f"  🪟 {prefix}正在激活滑块窗口 ({attempt}/{total_attempts})", flush=True)
        if use_os_focus:
            os_focused = ensure_page_foreground(page, label=label, attempts=1, delay=delay)
        else:
            os_focused = True
            try:
                page.bring_to_front()
            except Exception:
                pass
            try:
                page.evaluate("() => window.focus()")
            except Exception:
                pass
        try:
            focused = bool(page.evaluate("() => document.visibilityState === 'visible' && document.hasFocus()"))
        except Exception:
            focused = False
        if os_focused and focused:
            print(f"  ✅ {prefix}滑块窗口焦点确认成功", flush=True)
            return True
        try:
            last_state = page.evaluate(
                "() => ({ visibilityState: document.visibilityState, hasFocus: document.hasFocus(), url: location.href })"
            )
            if isinstance(last_state, dict):
                path = urllib.parse.urlparse(str(last_state.get("url", ""))).path or "/"
                print(
                    f"  ⚠️ {prefix}滑块窗口焦点状态: "
                    f"visibilityState={last_state.get('visibilityState')} "
                    f"hasFocus={last_state.get('hasFocus')} path={path}",
                    flush=True,
                )
        except Exception as e:
            last_state = {"error": str(e)}
            print(f"  ⚠️ {prefix}滑块窗口焦点状态读取失败: {e}", flush=True)
        if attempt < total_attempts:
            time.sleep(delay)
    if isinstance(last_state, dict) and "error" not in last_state:
        path = urllib.parse.urlparse(str(last_state.get("url", ""))).path or "/"
        print(
            f"  ❌ {prefix}滑块窗口焦点确认失败 "
            f"(visibilityState={last_state.get('visibilityState')}, "
            f"hasFocus={last_state.get('hasFocus')}, path={path})",
            flush=True,
        )
    else:
        print(f"  ❌ {prefix}滑块窗口焦点确认失败", flush=True)
    return False


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
        goto_register_page_with_retry(page)
        submit_registration_form_with_retry(page, name=name, email=email, password=password)

        submit_state = wait_for_captcha_or_submission_after_submit(page, timeout=5.0)
        if submit_state == "timeout":
            try:
                body_after_submit = read_page_body_text(page)
            except Exception:
                body_after_submit = ''
            if 'create account' in body_after_submit.lower() or '创建账号' in body_after_submit:
                print("  ⚠️ 注册提交后仍停留在表单页，正在重试提交")
                submit_registration_form_background(page, name=name, email=email, password=password)
                submit_state = wait_for_captcha_or_submission_after_submit(page, timeout=5.0)
        if submit_state == "stopped":
            return False

        # 检测是否出现验证码
        post_captcha_submission_needed = False
        if submit_state == "captcha" or detect_captcha(page):
            print("  🤖 检测到验证码弹窗")
            label_prefix = f"{label} " if label else ""
            if os.getenv("CAPTCHA_FORCE_VERIFY_SUCCESS", "").strip():
                install_aliyun_verify_success_route(page)
            topmost_hwnd = None
            slider_window_minimized = False

            def minimize_slider_window():
                nonlocal slider_window_minimized
                if slider_window_minimized:
                    return True
                if topmost_hwnd is not None:
                    minimized = minimize_page_window(page, label=label, hwnd=topmost_hwnd)
                else:
                    minimized = minimize_page_window(page, label=label)
                slider_window_minimized = bool(minimized)
                return minimized

            def ensure_slider_window_minimized():
                if slider_window_minimized:
                    return True
                with acquire_slider_lock(label=label, stop_event=STOP_EVENT):
                    return minimize_slider_window()

            @contextmanager
            def focused_slider_guard():
                nonlocal slider_window_minimized, topmost_hwnd
                current_hwnd = None
                minimize_before_release = False
                with acquire_slider_lock(label=label, stop_event=STOP_EVENT):
                    with hold_page_topmost(page, label=label) as topmost_ok:
                        if not topmost_ok:
                            yield False
                            return
                        current_hwnd = getattr(topmost_ok, "hwnd", None)
                        topmost_hwnd = current_hwnd
                        slider_window_minimized = False
                        if not focus_page_for_slider(page, label=label):
                            yield False
                            return
                        if not detect_captcha(page):
                            minimize_before_release = True
                            yield False
                            return
                        yield True
                        minimize_before_release = not detect_captcha(page)
                    if minimize_before_release:
                        minimize_slider_window()

            def complete_captcha_manually():
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
                dump_manual_trace_recording(
                    page,
                    manual_trace_records,
                    label=label,
                    image_dir=IMAGES_DIR,
                )
                return captcha_completed

            try:
                if captcha_solver_config is not None and captcha_solver_config.enabled:
                    try:
                        solver_result = solve_slider_captcha(
                            page,
                            captcha_solver_config,
                            label=label,
                            image_dir=IMAGES_DIR,
                            stop_event=STOP_EVENT,
                            drag_guard=focused_slider_guard,
                        )
                    finally:
                        print(f"  🪟 {label_prefix}滑块阶段结束，正在最小化窗口", flush=True)
                        ensure_slider_window_minimized()
                    if solver_result.ok:
                        print(f"  ✅ {label_prefix}{solver_result.message}")
                        post_captcha_submission_needed = True
                    elif captcha_solver_config.fallback_manual:
                        print(f"  ⚠️ {label_prefix}{solver_result.message}，改为人工处理")
                        with focused_slider_guard() as ready:
                            if not ready:
                                if not detect_captcha(page):
                                    post_captcha_submission_needed = True
                                else:
                                    return False
                            elif complete_captcha_manually():
                                post_captcha_submission_needed = True
                            else:
                                print("  ❌ 验证码未完成或超时")
                                return False
                    else:
                        print(f"  ❌ {label_prefix}{solver_result.message}")
                        return False
                else:
                    try:
                        with focused_slider_guard() as ready:
                            if not ready:
                                if not detect_captcha(page):
                                    print(f"  ✅ {label_prefix}验证码已在等待期间完成")
                                    post_captcha_submission_needed = True
                                else:
                                    return False
                            elif complete_captcha_manually():
                                post_captcha_submission_needed = True
                            else:
                                print("  ❌ 验证码未完成或超时")
                                return False
                    finally:
                        print(f"  🪟 {label_prefix}滑块阶段结束，正在最小化窗口", flush=True)
                        ensure_slider_window_minimized()
            except SliderLockTimeout as e:
                print(f"  🛑 {label_prefix}{e}")
                return False

        if post_captcha_submission_needed:
            if wait_for_registration_submission(page, timeout=post_captcha_submission_timeout(captcha_solver_config)):
                print("  ✅ 注册已提交，等待邮箱验证")
                return True

        # 检查注册结果
        body = read_page_body_text(page, timeout=1.5, interval=0.2)

        # 成功标志
        if registration_submission_detected(body):
            print("  ✅ 注册已提交，等待邮箱验证")
            return True

        # 错误标志
        if 'error' in body.lower() or 'failed' in body.lower() or '错误' in body:
            print(f"  ❌ 注册出错: {body[:200]}")
            return False

        if post_captcha_submission_needed and registration_form_still_visible(body):
            print("  ⚠️ 滑块后仍停留在注册表单，正在同页重试提交")
            if not registration_form_input_visible(page, timeout=500):
                print("  ℹ️ 注册表单字段已不可见，跳过慢速重提交流程，继续确认提交状态")
                if wait_for_registration_submission(page, timeout=post_captcha_submission_timeout(captcha_solver_config)):
                    print("  ✅ 注册已提交，等待邮箱验证")
                    return True
                body = read_page_body_text(page, timeout=1.5, interval=0.2)
                if registration_submission_detected(body):
                    print("  ✅ 注册已提交，等待邮箱验证")
                    return True
                if not registration_form_still_visible(body):
                    print("  ✅ 注册可能已提交")
                    return True
                print(f"  ⚠️ 页面状态异常: {body[:200]}")
                return False
            try:
                submit_registration_form_background(page, name=name, email=email, password=password)
            except Exception as resubmit_exc:
                print(f"  ⚠️ 滑块后重提交流程不可用，改为确认提交状态: {str(resubmit_exc)[:100]}")
                if wait_for_registration_submission(page, timeout=post_captcha_submission_timeout(captcha_solver_config)):
                    print("  ✅ 注册已提交，等待邮箱验证")
                    return True
                body = read_page_body_text(page, timeout=1.5, interval=0.2)
                if registration_submission_detected(body):
                    print("  ✅ 注册已提交，等待邮箱验证")
                    return True
                if not registration_form_still_visible(body):
                    print("  ✅ 注册可能已提交")
                    return True
                print(f"  ⚠️ 页面状态异常: {body[:200]}")
                return False
            resubmit_state = wait_for_captcha_or_submission_after_submit(page, timeout=5.0)
            if resubmit_state == "submitted":
                print("  ✅ 注册已提交，等待邮箱验证")
                return True
            wait_for_registration_submission(page, timeout=post_captcha_submission_timeout(captcha_solver_config))
            body = read_page_body_text(page, timeout=1.5, interval=0.2)
            if registration_submission_detected(body):
                print("  ✅ 注册已提交，等待邮箱验证")
                return True

        # 假设成功（表单已消失）
        if not registration_form_still_visible(body):
            print("  ✅ 注册可能已提交")
            return True

        print(f"  ⚠️ 页面状态异常: {body[:200]}")
        if any(marker in body for marker in ("请完成以下操作", "验证您是真人", "访问验证")):
            print("  ❌ 验证码仍未真正完成")
        return False

    except PlaywrightTimeout as e:
        print(f"  ⚠️ 操作超时: {str(e)[:100]}")
        try:
            body = read_page_body_text(page)
            body_lower = body.lower()
            if '待激活' in body or '激活账号' in body or 'pending activation' in body_lower or 'verification email' in body_lower:
                print("  ✅ 虽然发生超时，但已检测到注册提交状态")
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

def run_single_account(account_index, total_accounts, args, proxy_str=None):
    """执行单个账号编号；失败时可换新邮箱/浏览器代理补偿重试。"""
    worker_started_at = time.perf_counter()
    max_attempts = max(1, int(getattr(args, "account_retries", 1) or 1))
    for attempt in range(1, max_attempts + 1):
        if STOP_EVENT.is_set():
            return AccountRunResult(False, time.perf_counter() - worker_started_at)
        if max_attempts > 1:
            print(f"\n🔁 [账号 {account_index}/{total_accounts}] 第 {attempt}/{max_attempts} 次尝试")
        result = _run_single_account_once(account_index, total_accounts, args, proxy_str)
        if result:
            if isinstance(result, AccountRunResult):
                return AccountRunResult(True, time.perf_counter() - worker_started_at)
            return result
        if attempt < max_attempts and not STOP_EVENT.is_set():
            print(f"  🔁 [账号 {account_index}/{total_accounts}] 本次尝试失败，准备更换邮箱和浏览器代理重试...")
            sleep_interruptible(0.5)
    return AccountRunResult(False, time.perf_counter() - worker_started_at)


def _run_single_account_once(account_index, total_accounts, args, proxy_str=None):
    """执行单个账号注册任务；每个线程独立创建 Playwright 实例。"""
    started_at = time.perf_counter()

    label = f"[账号 {account_index}/{total_accounts}]"
    stage_timer = AccountStageTimer(label)
    summary_printed = False

    def result(success):
        nonlocal summary_printed
        if not summary_printed:
            stage_timer.print_summary(success=bool(success))
            summary_printed = True
        return AccountRunResult(success=success, duration_seconds=time.perf_counter() - started_at)

    print(f"\n{'═'*60}")
    print(f"🔢 {label}")
    print(f"{'═'*60}")

    try:
        proxy_info = build_browser_proxy(getattr(args, "browser_proxy", "") or proxy_str)
    except ValueError as e:
        print(f"  ❌ {label} {e}")
        return result(False)
    stage_timer.mark("代理解析")
    proxy_dict = proxy_info["proxy"]
    if proxy_dict:
        print(f"  🌐 {label} 浏览器代理: {proxy_info['display']}")
    else:
        print(f"  🌐 {label} 未使用浏览器代理（直连）")

    provider = None
    browser = None
    qwen = None

    try:
        if STOP_EVENT.is_set():
            print(f"  🛑 {label} 已收到停止请求，跳过")
            return result(False)
        with sync_playwright() as p:
            if STOP_EVENT.is_set():
                print(f"  🛑 {label} 已收到停止请求，跳过")
                return result(False)
            try:
                with acquire_foreground_window_lock(label=f"{label} 浏览器启动", stop_event=STOP_EVENT):
                    browser = p.chromium.launch(
                        headless=HEADLESS,
                        args=build_browser_launch_args(account_index),
                        proxy=proxy_dict,
                    )
                stage_timer.mark("浏览器启动")
            except Exception as e:
                print(f"  ❌ {label} 浏览器启动失败: {e}")
                return result(False)

            try:
                context = browser.new_context(
                    user_agent=USER_AGENT,
                    viewport={'width': 1280, 'height': 800},
                )

                if getattr(args, "browser_proxy", "") or proxy_str:
                    current_ip, country = "unknown", "unknown"
                    print(f"  📡 {label} 已配置浏览器代理，跳过本机 IP 检测")
                else:
                    current_ip, country = get_current_ip()
                    print(f"  📡 {label} 当前 IP: {current_ip} ({country})")

                provider = EmailProviderFactory.create(
                    provider_type=args.email_provider,
                    verbose=args.verbose,
                    api_proxy=args.api_proxy,
                    context=context,
                )
                stage_timer.mark("邮箱 Provider")

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
                stage_timer.mark("邮箱创建")

                first_name = gen_first_name()
                password = gen_password()
                name = gen_name(first_name)

                print(f"  📧 {label} 邮箱:    {email}")
                print(f"  👤 {label} 用户名:  {name}")
                print(f"  🔑 {label} 密码:    {password}")

                with acquire_foreground_window_lock(label=f"{label} 新建注册页", stop_event=STOP_EVENT):
                    qwen = context.new_page()
                stage_timer.mark("新建页面")
                install_aliyun_callback_probe(qwen)
                if getattr(args, "captcha_force_verify_success", False):
                    install_aliyun_verify_success_route(qwen)
                if STOP_EVENT.is_set():
                    print(f"  🛑 {label} 已收到停止请求，跳过注册")
                    return result(False)
                registered = register_qwen(
                    qwen,
                    name,
                    email,
                    password,
                    captcha_timeout=args.captcha_timeout,
                    captcha_solver_config=build_captcha_solver_config(args),
                    label=label,
                )
                stage_timer.mark("滑块/注册")
                if not registered:
                    print(f"  ❌ {label} 注册失败，跳过...")
                    return result(False)

                verify_url = provider.get_activation_link(timeout=300)
                stage_timer.mark("邮箱激活")

                print(f"  🔄 {label} 正在打开验证链接...")
                qwen.goto(verify_url, wait_until='domcontentloaded', timeout=30000)

                print(f"  🔑 {label} 正在提取认证令牌...")
                tokens = wait_for_token_extraction(qwen, timeout=5.0)
                stage_timer.mark("验证与令牌")

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
                maybe_sync_account_to_qwen2api_async(
                    email=email,
                    password=password,
                    token=tokens.get('token'),
                    config=build_qwen2api_sync_config(args),
                    label=label,
                )
                stage_timer.mark("保存/同步提交")

                print(f"  ✅ {label} 已验证并保存！", flush=True)
                success_result = result(True)
                try:
                    qwen.screenshot(path=f'{IMAGES_DIR}/qwen_verified_{account_index}.png')
                except Exception as e:
                    print(f"  ⚠️ {label} 截图失败，已忽略，不影响账号保存: {e}")
                return success_result

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

    return result(False)


def main():
    """主函数 - 自动化注册流程"""

    STOP_EVENT.clear()
    global INTERRUPT_COUNT
    INTERRUPT_COUNT = 0
    previous_sigint = None
    try:
        previous_sigint = signal.getsignal(signal.SIGINT)

        def _handle_sigint(_signum, _frame):
            managed_by_start = os.getenv(START_MANAGED_ENV, "") == "1"
            request_shutdown("收到 Ctrl+C", force_exit=not managed_by_start)

        signal.signal(signal.SIGINT, _handle_sigint)
    except Exception:
        previous_sigint = None
    start_parent_stop_file_watcher()

    args = parse_args()
    logging_state = enable_run_logging(args, script_stem="qwenv4")
    summary = AccountRunSummary()
    try:
        print(f"🧾 运行日志: {logging_state.path}")
        num_accounts = args.count
        if num_accounts is None:
            try:
                num_accounts = positive_int(input("📊 要创建多少个账号？ "))
            except (argparse.ArgumentTypeError, ValueError, EOFError):
                print("❌ 数量无效")
                sys.exit(1)

        os.makedirs(IMAGES_DIR, exist_ok=True)

        print(f"\n🎯 准备创建 {num_accounts} 个 Qwen 账号...")
        print(f"📮 邮箱服务: {args.email_provider}")
        if args.api_proxy and not is_generator_provider(args.email_provider):
            print(f"🔌 API 代理: {args.api_proxy}")
        if args.browser_proxy:
            print("🌐 浏览器代理: 已配置（每个账号启动时动态解析）")
        print(f"🚦 并发数量: {args.concurrency}")
        if args.captcha_record_only:
            print("🧠 滑块自动处理: 已禁用（只录制人工轨迹）")
        elif args.captcha_solver == "ddddocr":
            print("🧠 滑块处理: ddddocr 本地自动（最多 3 次，失败不回退人工）")
        elif args.captcha_solver == "ai":
            minutes = args.captcha_timeout // 60
            minute_text = f" / {minutes}分钟" if minutes else ""
            print(f"🤖 滑块 AI 超时: {args.captcha_timeout}s{minute_text}")
            print(f"🧠 滑块处理: 远程 AI（模型 {args.captcha_ai_model}，失败不回退人工）")
        else:
            minutes = args.captcha_timeout // 60
            minute_text = f" / {minutes}分钟" if minutes else ""
            print(f"🤖 人工滑块等待: {args.captcha_timeout}s{minute_text}")
            print("🧠 滑块处理: 人工")
        if args.captcha_record_trace or args.captcha_record_only:
            print("🎥 滑块轨迹录制: 已启用（人工通过后会保存 manual_trace_*.json）")
        if args.captcha_replay_trace:
            print(f"🎞️ 滑块轨迹重放: {args.captcha_replay_trace}")
        print(f"🖱️ 滑块拖动后端: {args.captcha_drag_backend}")
        print(f"🧭 滑块拖动策略: {args.captcha_drag_strategy}")
        if args.captcha_target_right_bias is not None:
            print(f"🎚️ 滑块释放目标右偏: {float(args.captcha_target_right_bias):+.1f}px")
        if args.captcha_callback_bypass:
            print("🧪 本地靶场回调实验: 已启用")
        if args.captcha_force_verify_success:
            print("🧪 本地靶场 Verify 响应替换: 已启用")
        if args.sync_qwen2api:
            print(f"🔁 qwen2API 同步: 已启用（{args.qwen2api_base_url}）")
        else:
            print("🔁 qwen2API 同步: 未启用")
        print("🔒 严格模式: 已启用（不会自动降级）\n")

        batch_started_at = time.perf_counter()
        with ThreadPoolExecutor(max_workers=args.concurrency) as executor:
            futures = submit_account_futures(
                executor,
                total_accounts=num_accounts,
                args=args,
                worker=run_single_account,
                proxy_str=None,
            )
            try:
                summary = collect_account_futures(futures, num_accounts)
            finally:
                if STOP_EVENT.is_set():
                    for future in futures:
                        future.cancel()
                    executor.shutdown(wait=False, cancel_futures=True)
                    print("🛑 已停止等待新任务完成，正在关闭已启动的浏览器...")
        total_duration_seconds = time.perf_counter() - batch_started_at

        print(f"\n{'═'*60}")
        success_count = summary.success_count
        print(f"🎉 完成: 已创建 {success_count}/{num_accounts} 个账号")
        print(f"📊 成功数: {success_count}/{num_accounts}")
        print(f"📈 成功率: {format_success_rate(success_count, num_accounts)}")
        print(f"⏱️ 平均成功耗时: {format_average_success_duration(summary)}")
        print(f"⏳ 总耗时: {format_duration_seconds(total_duration_seconds)}")
        print("💾 结果保存到:")
        print(f"   - {OUTPUT_FILE_TXT}（文本格式）")
        print(f"   - {OUTPUT_FILE_JSON}（JSON 数组格式）")
        if args.sync_qwen2api:
            print("🔁 正在等待 qwen2API 后台同步完成...")
            flush_qwen2api_async_sync(timeout=max(1, int(args.qwen2api_timeout)))
        print(f"{'═'*60}")
    finally:
        close_run_logging(logging_state)
        if previous_sigint is not None:
            try:
                signal.signal(signal.SIGINT, previous_sigint)
            except Exception:
                pass


if __name__ == "__main__":
    main()

