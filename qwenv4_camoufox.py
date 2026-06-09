#!/usr/bin/env python3
"""Qwen 自动注册脚本 Camoufox 版。

该入口复用 qwenv4.py 的业务流程，仅将浏览器启动层替换为 Camoufox。
"""

from __future__ import annotations

import os
import signal
import sys
import threading
import time
from contextlib import contextmanager
from concurrent.futures import ThreadPoolExecutor

from camoufox.sync_api import Camoufox

from captcha_solvers.slider_lock import acquire_foreground_window_lock
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
                    with acquire_foreground_window_lock(label=f"{label} 新建注册页", stop_event=STOP_EVENT):
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
                    qwen.goto(verify_url, wait_until="domcontentloaded", timeout=30000)

                    print(f"  🔑 {label} 正在提取认证令牌...")
                    tokens = wait_for_token_extraction(qwen, timeout=5.0)

                    qwen.screenshot(path=f"{IMAGES_DIR}/qwen_verified_{account_index}.png")

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
                return AccountRunResult(True, time.perf_counter() - worker_started_at)
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
