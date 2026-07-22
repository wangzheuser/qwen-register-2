"""在 per-account 动态代理下，逐一验证免配置邮箱渠道能否稳定创建邮箱。

背景：真实注册时邮箱 API 走的是静态 api_proxy（Clash 单 IP），所有账号共用一个出口，
被 FreeCustom 等渠道按 IP 限流 429 刷屏。本脚本改用浏览器那套动态代理模板
（http://baokemeng.{uuid}:admin2012@127.0.0.1:9200），每次创建邮箱前替换 {uuid}
生成一个独立出口 IP，模拟"每账号独立 IP"的目标状态，统计各渠道 create_inbox 的
成功率与耗时，判断哪些渠道在动态代理下真正稳定可用。

用法：python test/test_email_providers_dynamic_proxy.py [每渠道尝试次数] [代理模板]

作者：wangqiupei
"""

from __future__ import annotations

import sys
import time
import uuid
from pathlib import Path

# 允许直接以脚本方式运行（把项目根加入 import 路径）
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from email_providers import EmailProviderFactory  # noqa: E402
from email_providers.base import EmailProviderError  # noqa: E402

# 免配置（纯 API、无需浏览器 context）的渠道；generator.email 需要 BrowserContext，排除
API_PROVIDERS = ["mailtm", "mailporary", "gonebox", "tempmail_lol", "freecustom"]

DEFAULT_PROXY_TEMPLATE = "http://baokemeng.{uuid}:admin2012@127.0.0.1:9200"


def resolve_proxy(template: str) -> str:
    """把动态代理模板里的 {uuid} 替换成新随机值，得到一个独立出口 IP 的代理。"""
    return template.replace("{uuid}", uuid.uuid4().hex)


def probe_provider(provider_type: str, attempts: int, proxy_template: str) -> dict:
    """对单个渠道跑 attempts 次 create_inbox，每次用独立动态代理 IP，返回统计结果。"""
    ok = 0
    latencies: list[float] = []
    errors: list[str] = []

    for i in range(attempts):
        proxy = resolve_proxy(proxy_template)
        provider = None
        start = time.perf_counter()
        try:
            # verbose=False 保持输出干净；api_proxy 传入本次专属的动态代理
            provider = EmailProviderFactory.create(
                provider_type, verbose=False, api_proxy=proxy
            )
            address = provider.create_inbox()
            elapsed = time.perf_counter() - start
            latencies.append(elapsed)
            ok += 1
            print(f"  [{provider_type}] {i+1}/{attempts} ✓ {address}  ({elapsed:.1f}s)")
        except EmailProviderError as exc:
            elapsed = time.perf_counter() - start
            msg = str(exc)[:120]
            errors.append(msg)
            print(f"  [{provider_type}] {i+1}/{attempts} ✗ {msg}  ({elapsed:.1f}s)")
        except Exception as exc:  # noqa: BLE001  测试脚本要看清任何未预期异常
            elapsed = time.perf_counter() - start
            msg = f"{type(exc).__name__}: {str(exc)[:100]}"
            errors.append(msg)
            print(f"  [{provider_type}] {i+1}/{attempts} ✗ {msg}  ({elapsed:.1f}s)")
        finally:
            if provider is not None:
                try:
                    provider.cleanup()
                except Exception:  # noqa: BLE001  清理失败不影响测试结论
                    pass

    avg = sum(latencies) / len(latencies) if latencies else 0.0
    return {
        "provider": provider_type,
        "ok": ok,
        "attempts": attempts,
        "avg_latency": avg,
        "errors": errors,
    }


def main() -> int:
    attempts = int(sys.argv[1]) if len(sys.argv) > 1 else 3
    proxy_template = sys.argv[2] if len(sys.argv) > 2 else DEFAULT_PROXY_TEMPLATE

    print(f"动态代理模板: {proxy_template}")
    print(f"每渠道尝试次数: {attempts}\n")

    results = []
    for provider_type in API_PROVIDERS:
        print(f"== {provider_type} ==")
        results.append(probe_provider(provider_type, attempts, proxy_template))
        print()

    # 汇总表
    print("=" * 56)
    print(f"{'渠道':<16}{'成功/次数':<12}{'平均耗时':<12}结论")
    print("-" * 56)
    for r in results:
        rate = r["ok"] / r["attempts"] if r["attempts"] else 0
        verdict = "稳定" if rate == 1 else ("部分可用" if rate > 0 else "不可用")
        print(
            f"{r['provider']:<16}{r['ok']}/{r['attempts']:<10}"
            f"{r['avg_latency']:.1f}s{'':<8}{verdict}"
        )
    print("=" * 56)

    # 只要有一个渠道完全不可用就返回非 0，便于 CI / 调用方感知
    all_ok = all(r["ok"] == r["attempts"] for r in results)
    return 0 if all_ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
