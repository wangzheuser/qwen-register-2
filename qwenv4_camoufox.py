#!/usr/bin/env python3
"""Qwen 自动注册脚本 Camoufox 版。

该入口复用 qwenv4.py 的业务流程，仅将浏览器启动层替换为 Camoufox。
"""

from __future__ import annotations

import os
import json
import signal
import subprocess
import sys
import threading
import time
from contextlib import contextmanager
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from camoufox.sync_api import Camoufox

from captcha_solvers.slider_lock import acquire_foreground_window_lock
from captcha_solvers import slider_lock as slider_lock_module
from email_providers import EmailCreationError, EmailProviderError, EmailProviderFactory
from email_providers.generator_email import GeneratorEmailProvider
from email_providers.store import UsedEmailsStore
from qwenv4 import (
    AccountRunResult,
    AccountRunSummary,
    HEADLESS,
    IMAGES_DIR,
    OUTPUT_FILE_JSON,
    OUTPUT_FILE_TXT,
    STOP_EVENT,
    START_MANAGED_ENV,
    build_browser_proxy,
    request_shutdown,
    build_qwen2api_sync_config,
    build_captcha_solver_config,
    collect_account_futures,
    close_run_logging,
    enable_run_logging,
    gen_first_name,
    gen_name,
    gen_password,
    get_current_ip,
    format_average_success_duration,
    format_success_rate,
    install_aliyun_callback_probe,
    install_aliyun_verify_success_route,
    is_generator_provider,
    maybe_sync_account_to_qwen2api,
    parse_args,
    positive_int,
    register_qwen,
    save_account,
    sleep_interruptible,
    start_parent_stop_file_watcher,
    submit_account_futures,
    wait_for_token_extraction,
)


CAMOUFOX_WINDOW = (1280, 800)
CAMOUFOX_VIEWPORT = {"width": 1280, "height": 800}
CAMOUFOX_RUNTIME_LOCK = threading.RLock()


@contextmanager
def acquire_camoufox_runtime_lock(label):
    """串行化 Camoufox Sync API 调用，避免多线程 asyncio loop/driver 互相阻塞。"""
    started = time.perf_counter()
    print(f"  ⏳ {label} 等待 Camoufox 运行时锁...")
    CAMOUFOX_RUNTIME_LOCK.acquire()
    print(f"  🔐 {label} 已获得 Camoufox 运行时锁 waited={time.perf_counter() - started:.3f}s")
    held_started = time.perf_counter()
    try:
        yield
    finally:
        held = time.perf_counter() - held_started
        CAMOUFOX_RUNTIME_LOCK.release()
        print(f"  🔓 {label} 释放 Camoufox 运行时锁 held={held:.3f}s")


def _camoufox_launch_kwargs(proxy_dict):
    """构造 Camoufox 启动参数，避免传入 Chromium 专属参数。"""
    kwargs = {
        "headless": HEADLESS,
        "window": CAMOUFOX_WINDOW,
        "humanize": True,
        "os": "windows",
    }
    if proxy_dict:
        kwargs["proxy"] = proxy_dict
    return kwargs


def wait_for_camoufox_start_slot(label, *, stop_event=None, file_lock_timeout=0.5):
    """父进程启动 Camoufox worker 前短探测前台资源是否空闲。

    该函数只用于节流新 worker 启动，不能在滑块 intent 存在时长时间占住父进程
    window 队列。若短时间内检测到滑块正在占用前台，返回 False，让调度循环稍后
    重试；真正的滑块/窗口串行仍由子进程内部锁兜底。
    """
    from captcha_solvers.slider_lock import SliderLockTimeout

    if _camoufox_slider_intent_active():
        return False

    try:
        with acquire_foreground_window_lock(
            label=f"{label} worker 启动槽",
            stop_event=stop_event,
            file_lock_timeout=file_lock_timeout,
        ):
            return True
    except SliderLockTimeout:
        return False


def _camoufox_slider_intent_active() -> bool:
    """快速检测是否已有滑块进程声明前台意图。"""
    portalocker = getattr(slider_lock_module, "portalocker", None)
    if portalocker is None:
        return False
    lock_path = slider_lock_module._foreground_intent_lock_path(slider_lock_module.DEFAULT_SLIDER_LOCK_PATH)
    try:
        probe = portalocker.Lock(
            str(lock_path),
            timeout=0,
            flags=portalocker.LockFlags.EXCLUSIVE | portalocker.LockFlags.NON_BLOCKING,
        )
        probe.acquire()
        try:
            probe.release()
        except Exception:
            pass
        return False
    except Exception:
        return True


def _append_option(command, name, value):
    if value is not None and value != "":
        command.extend([name, str(value)])


def _append_flag(command, enabled, name):
    if enabled:
        command.append(name)


def build_camoufox_worker_command(args, account_index, total_accounts):
    """构造独立 Camoufox worker 子进程命令。"""
    script_path = str(Path(__file__).resolve())
    command = [
        sys.executable,
        script_path,
        "1",
        "--camoufox-worker",
        "--account-index",
        str(account_index),
        "--total-accounts",
        str(total_accounts),
        "--email-provider",
        args.email_provider,
        "--concurrency",
        "1",
        "--account-retries",
        "1",
        "--captcha-timeout",
        str(getattr(args, "captcha_timeout", 600)),
        "--captcha-solver",
        getattr(args, "captcha_solver", "ddddocr"),
        "--captcha-ai-base-url",
        getattr(args, "captcha_ai_base_url", ""),
        "--captcha-ai-model",
        getattr(args, "captcha_ai_model", ""),
        "--captcha-ai-timeout",
        str(getattr(args, "captcha_ai_timeout", 120)),
        "--captcha-ai-attempts",
        str(getattr(args, "captcha_ai_attempts", 3)),
        "--captcha-drag-backend",
        getattr(args, "captcha_drag_backend", "os"),
        "--captcha-drag-strategy",
        getattr(args, "captcha_drag_strategy", "fast_quadratic"),
        "--qwen2api-base-url",
        getattr(args, "qwen2api_base_url", "http://127.0.0.1:7860"),
        "--qwen2api-admin-key",
        getattr(args, "qwen2api_admin_key", "admin"),
        "--qwen2api-timeout",
        str(getattr(args, "qwen2api_timeout", 30)),
        "--camoufox-account-timeout",
        str(getattr(args, "camoufox_account_timeout", 120)),
    ]
    _append_option(command, "--api-proxy", getattr(args, "api_proxy", None))
    _append_option(command, "--browser-proxy", getattr(args, "browser_proxy", ""))
    _append_option(command, "--captcha-ai-api-key", getattr(args, "captcha_ai_api_key", ""))
    _append_option(command, "--captcha-replay-trace", getattr(args, "captcha_replay_trace", ""))
    if getattr(args, "captcha_target_right_bias", None) is not None:
        command.extend(["--captcha-target-right-bias", str(args.captcha_target_right_bias)])
    _append_flag(command, getattr(args, "no_captcha_ai_fallback_manual", False), "--no-captcha-ai-fallback-manual")
    _append_flag(command, getattr(args, "captcha_record_trace", False), "--captcha-record-trace")
    _append_flag(command, getattr(args, "captcha_record_only", False), "--captcha-record-only")
    _append_flag(command, getattr(args, "captcha_callback_bypass", False), "--captcha-callback-bypass")
    _append_flag(command, getattr(args, "captcha_force_verify_success", False), "--captcha-force-verify-success")
    _append_flag(command, getattr(args, "verbose", False), "--verbose")
    _append_flag(command, getattr(args, "sync_qwen2api", False), "--sync-qwen2api")
    _append_flag(command, getattr(args, "strict", True), "--strict")
    return command


def _terminate_process_tree(process, label):
    """终止 worker 子进程树，避免 Camoufox driver 残留。"""
    pid = getattr(process, "pid", None)
    if not pid:
        try:
            process.terminate()
        except Exception:
            pass
        return
    try:
        if os.name == "nt":
            subprocess.run(
                ["taskkill", "/PID", str(pid), "/T", "/F"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
            )
        else:
            process.terminate()
    except Exception as e:
        print(f"  ⚠️ {label} 终止 Camoufox worker 子进程失败: {e}", flush=True)


def run_account_subprocess(account_index, total_accounts, args, proxy_str=None, timeout_seconds=None):
    """用独立 Python 子进程执行一个 Camoufox 账号，隔离 Camoufox driver 卡死。"""
    started_at = time.perf_counter()
    label = f"[账号 {account_index}/{total_accounts}]"
    timeout = float(timeout_seconds or getattr(args, "camoufox_account_timeout", 180) or 180)
    max_attempts = max(1, int(getattr(args, "account_retries", 1) or 1))
    env = os.environ.copy()
    env["PYTHONUNBUFFERED"] = "1"
    process = None
    reader = None
    child_result: dict[str, object] = {}
    child_result_lock = threading.Lock()

    def result(success):
        return AccountRunResult(success=success, duration_seconds=time.perf_counter() - started_at)

    for attempt in range(1, max_attempts + 1):
        if STOP_EVENT.is_set():
            return result(False)
        attempt_started_at = time.perf_counter()
        command = build_camoufox_worker_command(args, account_index, total_accounts)
        if max_attempts > 1:
            print(f"  🔁 {label} 独立 worker 第 {attempt}/{max_attempts} 次尝试", flush=True)
        try:
            last_start_slot_wait_log = 0.0
            while not wait_for_camoufox_start_slot(label, stop_event=STOP_EVENT):
                if STOP_EVENT.is_set():
                    return result(False)
                now = time.monotonic()
                if now - last_start_slot_wait_log >= 5.0:
                    print(f"  ⏳ {label} 滑块正在占用前台，稍后再启动新 worker", flush=True)
                    last_start_slot_wait_log = now
                sleep_interruptible(0.5)
            if STOP_EVENT.is_set():
                return result(False)
            print(f"  🧩 {label} 正在启动独立 Camoufox worker 子进程（超时 {int(timeout)}s）", flush=True)
            child_result.clear()
            process = None
            reader = None
            last_output_at = {"value": time.monotonic()}
            process = subprocess.Popen(
                command,
                cwd=str(Path(__file__).resolve().parent),
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace",
                env=env,
                bufsize=1,
            )

            def _read_output():
                marker = "__QWENV4_CAMOUFOX_WORKER_RESULT__ "
                try:
                    if process.stdout is not None:
                        for line in process.stdout:
                            with child_result_lock:
                                last_output_at["value"] = time.monotonic()
                            clean_line = line.rstrip("\r\n")
                            if clean_line.startswith(marker):
                                try:
                                    parsed = json.loads(clean_line[len(marker):])
                                    with child_result_lock:
                                        child_result.update(parsed)
                                except Exception:
                                    pass
                                continue
                            if "已验证并保存" in clean_line:
                                with child_result_lock:
                                    child_result.setdefault("success", True)
                                    child_result.setdefault("duration_seconds", time.perf_counter() - attempt_started_at)
                            print(clean_line, flush=True)
                except Exception as e:
                    print(f"  ⚠️ {label} 读取 Camoufox worker 输出失败: {e}", flush=True)

            reader = threading.Thread(target=_read_output, name=f"camoufox-worker-output-{account_index}", daemon=True)
            reader.start()
            timed_out = False
            while True:
                return_code = process.poll()
                if return_code is not None:
                    break
                if STOP_EVENT.is_set():
                    print(f"  🛑 {label} 收到停止请求，正在终止 Camoufox worker 子进程", flush=True)
                    _terminate_process_tree(process, label)
                    return result(False)
                now = time.monotonic()
                with child_result_lock:
                    idle_seconds = now - float(last_output_at.get("value", now))
                if idle_seconds >= timeout:
                    timed_out = True
                    with child_result_lock:
                        child_success_seen = bool(child_result.get("success"))
                        child_duration = child_result.get("duration_seconds")
                    if child_success_seen:
                        print(
                            f"  ⏰ {label} 已保存成功但子进程未及时退出，正在终止清理残留进程",
                            flush=True,
                        )
                    else:
                        print(
                            f"  ⏰ {label} Camoufox worker 无输出超时 {int(timeout)}s，"
                            f"正在终止当前账号子进程",
                            flush=True,
                        )
                    _terminate_process_tree(process, label)
                    break
                sleep_interruptible(0.2)

            if reader is not None:
                reader.join(timeout=5)
            with child_result_lock:
                child_success_seen = bool(child_result.get("success"))
                child_duration = child_result.get("duration_seconds")
            if child_success_seen:
                if isinstance(child_duration, (int, float)):
                    return AccountRunResult(success=True, duration_seconds=float(child_duration))
                return result(True)
            success = (not timed_out) and process.returncode == 0
            if success and isinstance(child_duration, (int, float)):
                return AccountRunResult(success=True, duration_seconds=float(child_duration))
            if success:
                return AccountRunResult(success=True, duration_seconds=time.perf_counter() - attempt_started_at)
            if not timed_out:
                print(f"  ❌ {label} Camoufox worker 子进程失败，退出码: {process.returncode}", flush=True)
            if attempt < max_attempts and not STOP_EVENT.is_set():
                print(f"  🔁 {label} worker 本次失败，准备重启独立子进程重试...", flush=True)
                sleep_interruptible(0.5)
        except Exception as e:
            print(f"  ❌ {label} Camoufox worker 子进程异常: {e}", flush=True)
            if process is not None and process.poll() is None:
                _terminate_process_tree(process, label)
            if attempt < max_attempts and not STOP_EVENT.is_set():
                print(f"  🔁 {label} worker 异常，准备重启独立子进程重试...", flush=True)
                sleep_interruptible(0.5)
                continue
            return result(False)
    return result(False)


def open_verification_link_with_retry(page, verify_url, label, attempts=2):
    """打开邮箱验证链接；Camoufox/Firefox 偶发 NS_BINDING_ABORTED 时轻量重试。"""
    last_error = None
    for attempt in range(1, max(1, int(attempts)) + 1):
        try:
            page.goto(verify_url, wait_until="domcontentloaded", timeout=30000)
            return True
        except Exception as e:
            last_error = e
            if attempt >= attempts:
                raise
            print(f"  ⚠️ {label} 打开验证链接失败，准备重试 ({attempt}/{attempts}): {e}")
            sleep_interruptible(1.0)
    if last_error is not None:
        raise last_error
    return False


def _run_single_account_once(account_index, total_accounts, args, proxy_str=None):
    """执行单个账号注册任务；每个线程独立创建 Camoufox 实例。"""
    started_at = time.perf_counter()

    def result(success):
        return AccountRunResult(success=success, duration_seconds=time.perf_counter() - started_at)

    label = f"[账号 {account_index}/{total_accounts}]"
    print(f"\n{'═'*60}")
    print(f"🦊 {label} Camoufox")
    print(f"{'═'*60}")

    try:
        proxy_info = build_browser_proxy(getattr(args, "browser_proxy", "") or proxy_str)
    except ValueError as e:
        print(f"  ❌ {label} {e}")
        return result(False)
    proxy_dict = proxy_info["proxy"]
    if proxy_dict:
        print(f"  🌐 {label} 浏览器代理: {proxy_info['display']}")
    else:
        print(f"  🌐 {label} 未使用浏览器代理（直连）")

    provider = None
    browser = None
    context = None
    qwen = None

    try:
        if STOP_EVENT.is_set():
            print(f"  🛑 {label} 已收到停止请求，跳过")
            return result(False)
        browser_cm = None
        try:
            with acquire_camoufox_runtime_lock(f"{label} Camoufox 启动"):
                with acquire_foreground_window_lock(label=f"{label} Camoufox 启动", stop_event=STOP_EVENT):
                    browser_cm = Camoufox(**_camoufox_launch_kwargs(proxy_dict))
                    browser = browser_cm.__enter__()
        except Exception as e:
            print(f"  ❌ {label} Camoufox 启动失败: {e}")
            print("  💡 如首次使用 Camoufox，请先运行: python -m camoufox fetch")
            return result(False)

        try:
            try:
                with acquire_camoufox_runtime_lock(f"{label} 新建注册页"):
                    # Camoufox/Firefox 的 browser.new_page() 偶发卡顿几十秒。它不是 OS
                    # 鼠标拖动，不应长时间占用前台窗口锁，否则会拖慢后续滑块队列。
                    # 滑块阶段仍会通过 slider 文件锁 + Windows 置顶重新获取前台。
                    qwen = browser.new_page(viewport=CAMOUFOX_VIEWPORT)
                    context = qwen.context
                    install_aliyun_callback_probe(qwen)

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

                if STOP_EVENT.is_set():
                    print(f"  🛑 {label} 已收到停止请求，跳过注册")
                    return result(False)
                with acquire_camoufox_runtime_lock(f"{label} 注册流程"):
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
                    return result(False)

                verify_url = provider.get_activation_link(timeout=300)

                with acquire_camoufox_runtime_lock(f"{label} 验证与令牌提取"):
                    print(f"  🔄 {label} 正在打开验证链接...")
                    open_verification_link_with_retry(qwen, verify_url, label)

                    print(f"  🔑 {label} 正在提取认证令牌...")
                    tokens = wait_for_token_extraction(qwen, timeout=5.0)

                    try:
                        qwen.screenshot(path=f"{IMAGES_DIR}/qwen_verified_{account_index}.png")
                    except Exception as e:
                        print(f"  ⚠️ {label} 截图失败，已忽略，不影响账号保存: {e}")

                    if tokens["token"]:
                        print(f"  ✅ {label} 已提取认证令牌: {tokens['token'][:80]}...")
                    else:
                        print(f"  ⚠️  {label} 未找到认证令牌（可能需要等待更久）")

                    body = qwen.evaluate("document.body.innerText") or ""
                print(f"  📋 {label} 验证结果: {body[:200]}")

                save_account(
                    email=email,
                    password=password,
                    name=name,
                    ip=current_ip,
                    country=country,
                    token=tokens.get("token"),
                    active_token=tokens.get("active_token"),
                    device_id=tokens.get("device_id"),
                    user_role=tokens.get("user_role", "user"),
                )
                maybe_sync_account_to_qwen2api(
                    email=email,
                    password=password,
                    token=tokens.get("token"),
                    config=build_qwen2api_sync_config(args),
                    label=label,
                )

                print(f"  ✅ {label} 已验证并保存！")
                return result(True)

            except EmailProviderError as e:
                print(f"  ❌ {label} 邮箱服务错误: {e}")
                print(f"  ⏭️ {label} 严格模式：跳过当前账号，不自动降级")
            except Exception as e:
                print(f"  ❌ {label} 出错: {e}")
            finally:
                if qwen is not None:
                    try:
                        with acquire_camoufox_runtime_lock(f"{label} 关闭注册页"):
                            qwen.close()
                    except Exception:
                        pass
                if provider is not None:
                    try:
                        provider.cleanup()
                    except Exception:
                        pass
                if context is not None:
                    try:
                        with acquire_camoufox_runtime_lock(f"{label} 关闭浏览器上下文"):
                            context.close()
                    except Exception:
                        pass
                if browser_cm is not None:
                    try:
                        with acquire_camoufox_runtime_lock(f"{label} 关闭 Camoufox"):
                            browser_cm.__exit__(None, None, None)
                    except Exception:
                        pass
        except Exception as e:
            print(f"  ❌ {label} Camoufox 启动失败: {e}")
            print("  💡 如首次使用 Camoufox，请先运行: python -m camoufox fetch")
            return result(False)
    except Exception as e:
        print(f"  ❌ {label} 执行异常: {e}")

    return result(False)


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
                return result
            return result
        if attempt < max_attempts and not STOP_EVENT.is_set():
            print(f"  🔁 [账号 {account_index}/{total_accounts}] 本次尝试失败，准备更换邮箱和浏览器代理重试...")
            sleep_interruptible(0.5)
    return AccountRunResult(False, time.perf_counter() - worker_started_at)


def main():
    """主函数 - Camoufox 自动化注册流程。"""
    STOP_EVENT.clear()
    try:
        def _handle_sigint(_signum, _frame):
            managed_by_start = os.getenv(START_MANAGED_ENV, "") == "1"
            request_shutdown("收到 Ctrl+C", force_exit=not managed_by_start)

        signal.signal(signal.SIGINT, _handle_sigint)
    except Exception:
        pass
    start_parent_stop_file_watcher()

    args = parse_args()
    if getattr(args, "camoufox_worker", False):
        result = run_single_account(args.account_index, args.total_accounts, args, None)
        print(
            "__QWENV4_CAMOUFOX_WORKER_RESULT__ "
            + json.dumps(
                {
                    "success": bool(result.success),
                    "duration_seconds": result.duration_seconds,
                },
                ensure_ascii=False,
            ),
            flush=True,
        )
        sys.exit(0 if result.success else 1)

    logging_state = enable_run_logging(args, script_stem="qwenv4-camoufox")
    try:
        print(f"🧾 运行日志: {logging_state.path}")
        num_accounts = args.count
        if num_accounts is None:
            try:
                num_accounts = positive_int(input("📊 要创建多少个账号？ "))
            except Exception:
                print("❌ 数量无效")
                sys.exit(1)

        os.makedirs(IMAGES_DIR, exist_ok=True)

        print(f"\n🎯 准备使用 Camoufox 创建 {num_accounts} 个 Qwen 账号...")
        print(f"📮 邮箱服务: {args.email_provider}")
        if args.api_proxy and not is_generator_provider(args.email_provider):
            print(f"🔌 API 代理: {args.api_proxy}")
        if args.browser_proxy:
            print("🌐 浏览器代理: 已配置（每个账号启动时动态解析）")
        print(f"🚦 并发数量: {args.concurrency}")
        if args.captcha_solver == "ddddocr":
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
        if args.captcha_record_trace:
            print("🎥 滑块轨迹录制: 已启用（人工通过后会保存 manual_trace_*.json）")
        if args.captcha_replay_trace:
            print(f"🎞️ 滑块轨迹重放: {args.captcha_replay_trace}")
        print(f"🖱️ 滑块拖动后端: {args.captcha_drag_backend}")
        print(f"🧭 滑块拖动策略: {args.captcha_drag_strategy}")
        if args.captcha_target_right_bias is not None:
            print(f"🎚️ 滑块释放目标右偏: {float(args.captcha_target_right_bias):+.1f}px")
        if args.captcha_callback_bypass:
            print("🧪 本地靶场回调实验: 已启用")
        if getattr(args, "captcha_force_verify_success", False):
            print("🧪 本地靶场 Verify 响应替换: 已启用")
        if args.sync_qwen2api:
            print(f"🔁 qwen2API 同步: 已启用（{args.qwen2api_base_url}）")
        else:
            print("🔁 qwen2API 同步: 未启用")
        print("🔒 严格模式: 已启用（不会自动降级）\n")

        summary = AccountRunSummary()
        worker = run_account_subprocess if args.concurrency > 1 else run_single_account
        with ThreadPoolExecutor(max_workers=args.concurrency) as executor:
            futures = submit_account_futures(
                executor,
                total_accounts=num_accounts,
                args=args,
                worker=worker,
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

        print(f"\n{'═'*60}")
        success_count = summary.success_count
        print(f"🎉 完成: 已创建 {success_count}/{num_accounts} 个账号")
        print(f"📊 成功数: {success_count}/{num_accounts}")
        print(f"📈 成功率: {format_success_rate(success_count, num_accounts)}")
        print(f"⏱️ 平均成功耗时: {format_average_success_duration(summary)}")
        print("💾 结果保存到:")
        print(f"   - {OUTPUT_FILE_TXT}（文本格式）")
        print(f"   - {OUTPUT_FILE_JSON}（JSON 数组格式）")
        print(f"{'═'*60}")
    finally:
        close_run_logging(logging_state)


if __name__ == "__main__":
    main()
