#!/usr/bin/env python3
"""Qwen 自动注册脚本 Camoufox 版。

该入口复用 qwenv4.py 的业务流程，仅将浏览器启动层替换为 Camoufox。
"""

from __future__ import annotations

import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor

from camoufox.sync_api import Camoufox

from email_providers import EmailCreationError, EmailProviderError, EmailProviderFactory
from email_providers.generator_email import GeneratorEmailProvider
from email_providers.store import UsedEmailsStore
from qwenv4 import (
    HEADLESS,
    IMAGES_DIR,
    OUTPUT_FILE_JSON,
    OUTPUT_FILE_TXT,
    PROXY_FILE,
    STOP_EVENT,
    ProxyRotator,
    request_shutdown,
    build_qwen2api_sync_config,
    build_captcha_solver_config,
    collect_account_futures,
    extract_tokens,
    gen_first_name,
    gen_name,
    gen_password,
    get_current_ip,
    install_aliyun_callback_probe,
    install_aliyun_verify_success_route,
    is_generator_provider,
    maybe_sync_account_to_qwen2api,
    parse_args,
    positive_int,
    register_qwen,
    save_account,
)


CAMOUFOX_WINDOW = (1280, 800)
CAMOUFOX_VIEWPORT = {"width": 1280, "height": 800}


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


def run_single_account(account_index, total_accounts, args, proxy_str):
    """执行单个账号注册任务；每个线程独立创建 Camoufox 实例。"""
    label = f"[账号 {account_index}/{total_accounts}]"
    print(f"\n{'═'*60}")
    print(f"🦊 {label} Camoufox")
    print(f"{'═'*60}")

    proxy_dict = ProxyRotator.parse_proxy(proxy_str) if proxy_str else None
    if proxy_dict:
        parts = proxy_str.split(":")
        print(f"  🌐 {label} 浏览器代理: {parts[0]}:{parts[1]}")
    else:
        print(f"  🌐 {label} 未使用浏览器代理（直连）")

    provider = None
    browser = None
    context = None
    qwen = None

    try:
        if STOP_EVENT.is_set():
            print(f"  🛑 {label} 已收到停止请求，跳过")
            return False
        try:
            browser_cm = Camoufox(**_camoufox_launch_kwargs(proxy_dict))
        except Exception as e:
            print(f"  ❌ {label} Camoufox 启动失败: {e}")
            print("  💡 如首次使用 Camoufox，请先运行: python -m camoufox fetch")
            return False

        try:
            with browser_cm as browser:
                try:
                    context = browser.new_context(viewport=CAMOUFOX_VIEWPORT)

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
                    qwen.goto(verify_url, wait_until="domcontentloaded", timeout=30000)
                    time.sleep(5)

                    qwen.screenshot(path=f"{IMAGES_DIR}/qwen_verified_{account_index}.png")

                    print(f"  🔑 {label} 正在提取认证令牌...")
                    tokens = extract_tokens(qwen)

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
                    if context is not None:
                        try:
                            context.close()
                        except Exception:
                            pass
        except Exception as e:
            print(f"  ❌ {label} Camoufox 启动失败: {e}")
            print("  💡 如首次使用 Camoufox，请先运行: python -m camoufox fetch")
            return False
    except Exception as e:
        print(f"  ❌ {label} 执行异常: {e}")

    return False


def main():
    """主函数 - Camoufox 自动化注册流程。"""
    args = parse_args()
    num_accounts = args.count
    if num_accounts is None:
        try:
            num_accounts = positive_int(input("📊 要创建多少个账号？ "))
        except Exception:
            print("❌ 数量无效")
            sys.exit(1)

    os.makedirs(IMAGES_DIR, exist_ok=True)

    proxy_rotator = ProxyRotator(PROXY_FILE)
    proxy_assignments = [proxy_rotator.get_next() for _ in range(num_accounts)]

    print(f"\n🎯 准备使用 Camoufox 创建 {num_accounts} 个 Qwen 账号...")
    print(f"📮 邮箱服务: {args.email_provider}")
    if args.api_proxy and not is_generator_provider(args.email_provider):
        print(f"🔌 API 代理: {args.api_proxy}")
    print(f"🚦 并发数量: {args.concurrency}")
    minutes = args.captcha_timeout // 60
    minute_text = f" / {minutes}分钟" if minutes else ""
    print(f"🤖 滑块等待: {args.captcha_timeout}s{minute_text}")
    if args.captcha_solver == "ai":
        fallback_text = "启用" if not args.no_captcha_ai_fallback_manual else "禁用"
        print(f"🧠 滑块 AI: 已启用（模型 {args.captcha_ai_model}，人工回退{fallback_text}）")
    else:
        print("🧠 滑块 AI: 未启用")
    if args.captcha_record_trace:
        print("🎥 滑块轨迹录制: 已启用（人工通过后会保存 manual_trace_*.json）")
    if args.captcha_replay_trace:
        print(f"🎞️ 滑块轨迹重放: {args.captcha_replay_trace}")
    print(f"🖱️ 滑块拖动后端: {args.captcha_drag_backend}")
    print(f"🧭 滑块拖动策略: {args.captcha_drag_strategy}")
    if args.captcha_callback_bypass:
        print("🧪 本地靶场回调实验: 已启用")
    if getattr(args, "captcha_force_verify_success", False):
        print("🧪 本地靶场 Verify 响应替换: 已启用")
    if args.sync_qwen2api:
        print(f"🔁 qwen2API 同步: 已启用（{args.qwen2api_base_url}）")
    else:
        print("🔁 qwen2API 同步: 未启用")
    print("🔒 严格模式: 已启用（不会自动降级）\n")

    success_count = 0
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


if __name__ == "__main__":
    main()
