r"""Playwright 版 AI 滑块验证码处理模块。

实现思路参考本地学习项目 ``D:\projects\github\ai-captcha-bypass``：
截图验证码区域，交给 OpenAI 兼容视觉模型估算水平拖动距离，再用浏览器鼠标事件分段拖动。

注意：API 密钥只从参数或环境变量读取，不在代码中写死。
"""

from __future__ import annotations

import base64
import json
import math
import os
import random
import re
import time
from dataclasses import dataclass
from io import BytesIO
from pathlib import Path
from typing import Any, Optional
from urllib.parse import parse_qs, unquote_plus, urlparse

import httpx
from captcha_solvers.window_focus import ensure_page_topmost_foreground
try:  # pragma: no cover - 依赖缺失时运行时会自动降级到 AI 估算。
    from PIL import Image
except Exception:  # pragma: no cover
    Image = None  # type: ignore

DEFAULT_CAPTCHA_AI_BASE_URL = os.getenv("CAPTCHA_AI_BASE_URL", "http://127.0.0.1:8000/v1")
DEFAULT_CAPTCHA_AI_MODEL = os.getenv("CAPTCHA_AI_MODEL", "gpt-5.5")
DEFAULT_CAPTCHA_AI_TIMEOUT = 120
DEFAULT_CAPTCHA_AI_ATTEMPTS = 3
ALIYUN_PUZZLE_MOVE_RATIO = 0.94
ALIYUN_AI_DISTANCE_FACTOR = 1.10
_DDDDOCR_SLIDE: Optional[Any] = None
_DDDDOCR_LOAD_FAILED = False

_DISTANCE_PROMPT = """
你正在本地授权靶场中分析一个滑块验证码截图。请只完成视觉测量任务：
1. 忽略日志 ID、时间、说明文字中的所有数字。
2. 找到需要拖动的滑块按钮或滑块把手，通常在底部滑轨左侧。
3. 找到拼图目标位置，可能是蓝色/深色缺口、轮廓框，或需要重合的位置。
4. 计算“滑块按钮需要向右拖动”的水平像素距离，使拼图块与缺口重合。

输出要求：只返回一个 10 到 600 之间的整数，不要单位，不要解释。如果看不清但明显是“拖到最右侧”的无痕滑块，请估算拖到最右侧的距离。
""".strip()


_ADJUSTMENT_PROMPT = """
你正在本地授权靶场中检查一个滑块验证码“拖动中、尚未松手”的截图。
拼图块已经被拖到目标附近。请判断拼图块与白色/灰色缺口是否对齐：
- 如果拼图块还在目标左侧，返回正整数，表示还需要继续向右微调的像素数。
- 如果拼图块已经在目标右侧，返回负整数，表示需要向左回拉的像素数。
- 如果已经基本对齐，返回 0。

输出要求：只返回 -25 到 25 之间的一个整数，不要单位，不要解释。
""".strip()


@dataclass(frozen=True)
class CaptchaSolverConfig:
    """AI 滑块求解配置。"""

    enabled: bool = False
    mode: str = ""
    base_url: str = DEFAULT_CAPTCHA_AI_BASE_URL
    api_key: str = ""
    model: str = DEFAULT_CAPTCHA_AI_MODEL
    timeout: int = DEFAULT_CAPTCHA_AI_TIMEOUT
    attempts: int = DEFAULT_CAPTCHA_AI_ATTEMPTS
    fallback_manual: bool = True
    overall_timeout: int = 0


@dataclass(frozen=True)
class CaptchaSolverResult:
    """AI 滑块处理结果。"""

    ok: bool
    message: str
    attempts: int = 0
    distance: Optional[int] = None
    screenshot_path: Optional[str] = None


class CaptchaAiClient:
    """最小 OpenAI 兼容视觉接口客户端。"""

    def __init__(
        self,
        *,
        base_url: str = DEFAULT_CAPTCHA_AI_BASE_URL,
        api_key: str = "",
        model: str = DEFAULT_CAPTCHA_AI_MODEL,
        timeout: int = DEFAULT_CAPTCHA_AI_TIMEOUT,
        client: Optional[httpx.Client] = None,
    ):
        self.base_url = (base_url or DEFAULT_CAPTCHA_AI_BASE_URL).rstrip("/")
        self.api_key = api_key or ""
        self.model = model or DEFAULT_CAPTCHA_AI_MODEL
        self.timeout = timeout
        self._client = client

    def ask_slider_distance(self, image_path: str) -> int:
        """请求视觉模型返回滑块需要向右移动的像素距离。"""
        return _parse_distance(self._ask_vision(image_path, _DISTANCE_PROMPT))

    def ask_slider_adjustment(self, image_path: str) -> int:
        """请求视觉模型返回当前拖动中状态还需要微调的有符号像素。"""
        return _parse_signed_adjustment(self._ask_vision(image_path, _ADJUSTMENT_PROMPT))

    def _ask_vision(self, image_path: str, prompt: str) -> str:
        if not self.api_key:
            raise ValueError("未配置滑块 AI API 密钥，请设置 --captcha-ai-api-key 或环境变量 CAPTCHA_AI_API_KEY")

        image_b64 = _image_to_base64(image_path)
        payload = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": "你是本地靶场验证码视觉测量助手。"},
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": prompt},
                        {
                            "type": "image_url",
                            "image_url": {"url": f"data:image/png;base64,{image_b64}"},
                        },
                    ],
                },
            ],
            "temperature": 0,
            "max_tokens": 32,
        }
        headers = {"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"}

        owns_client = self._client is None
        client = self._client or httpx.Client(timeout=self.timeout)
        try:
            response = client.post(f"{self.base_url}/chat/completions", headers=headers, json=payload)
            if response.status_code in {301, 302, 303, 307, 308} and response.headers.get("location"):
                # 某些 OpenAI 兼容网关会把 http 跳转到 https；手动重发 POST，避免 301 被客户端改成 GET。
                redirect_url = str(response.request.url.join(response.headers["location"]))
                response = client.post(redirect_url, headers=headers, json=payload)
            response.raise_for_status()
            data = response.json()
            return str(data["choices"][0]["message"]["content"])
        finally:
            if owns_client:
                client.close()


def solve_slider_captcha(
    page: Any,
    config: CaptchaSolverConfig,
    *,
    label: str = "",
    image_dir: str = "images",
    client: Optional[CaptchaAiClient] = None,
    stop_event: Optional[Any] = None,
) -> CaptchaSolverResult:
    """尝试在当前 Playwright 页面上处理滑块验证码。"""
    prefix = f"{label} " if label else ""
    if not config.enabled:
        return CaptchaSolverResult(ok=False, message="AI 滑块处理未启用")
    Path(image_dir).mkdir(parents=True, exist_ok=True)
    ai_client: Optional[CaptchaAiClient] = client
    mode = (config.mode or ("ai" if (config.api_key or client is not None) else "ddddocr")).strip().lower()
    if mode not in {"ddddocr", "ai"}:
        mode = "ai"
    if mode == "ai" and not config.api_key:
        return CaptchaSolverResult(ok=False, message="未配置滑块 AI API 密钥", attempts=0)
    deadline = None
    if mode == "ai" and int(config.overall_timeout or 0) > 0:
        deadline = time.monotonic() + int(config.overall_timeout)

    def timed_out() -> bool:
        return deadline is not None and time.monotonic() >= deadline

    last_message = "尚未尝试"
    last_distance: Optional[int] = None
    last_screenshot: Optional[str] = None
    last_corrected_target_x: Optional[float] = None
    last_target_signature: Optional[tuple[Optional[float], Optional[float], Optional[float], str]] = None
    repeated_target_index = 0
    network_debug = _attach_captcha_network_debug(page, prefix) if os.getenv("CAPTCHA_DEBUG_NETWORK") else None

    for attempt in range(1, max(1, config.attempts) + 1):
        try:
            if timed_out():
                return CaptchaSolverResult(ok=False, message="AI 滑块处理整体超时", attempts=attempt - 1)
            if _is_cancelled(stop_event):
                return CaptchaSolverResult(ok=False, message="AI 滑块处理已取消", attempts=attempt - 1)
            mode_text = "ddddocr" if mode == "ddddocr" else "AI"
            print(f"  🤖 {prefix}{mode_text} 正在尝试处理滑块（第 {attempt}/{config.attempts} 次）")
            root = _find_captcha_root(page)
            if root is None:
                return CaptchaSolverResult(ok=True, message="未检测到可见滑块验证码", attempts=attempt - 1)

            if not _wait_for_captcha_ready(page, stop_event=stop_event, timeout=8):
                last_message = "验证码图片加载超时"
                print(f"  ⚠️ {prefix}{last_message}，尝试刷新后重试")
                _try_refresh_captcha(root)
                if not _wait_for_captcha_ready(page, stop_event=stop_event, timeout=10):
                    if _is_cancelled(stop_event):
                        return CaptchaSolverResult(ok=False, message="AI 滑块处理已取消", attempts=attempt - 1)
                    continue
                if _is_cancelled(stop_event):
                    return CaptchaSolverResult(ok=False, message="AI 滑块处理已取消", attempts=attempt - 1)

            if os.getenv("CAPTCHA_CALLBACK_BYPASS", "").strip():
                try:
                    page.evaluate(
                        "mode => { window.CAPTCHA_CALLBACK_BYPASS_MODE = mode || 'auto'; }",
                        os.getenv("CAPTCHA_CALLBACK_BYPASS_MODE", "auto").strip() or "auto",
                    )
                except Exception:
                    pass
                if _try_aliyun_callback_success(page):
                    _sleep(5, stop_event)
                    if _captcha_solved(page):
                        return CaptchaSolverResult(ok=True, message="本地靶场回调实验已通过滑块", attempts=attempt)
                if network_debug:
                    _dump_captcha_network_debug(network_debug, prefix)
                    _dump_captcha_dom_debug(page, prefix)

            if (
                os.name == "nt"
                and os.getenv("CAPTCHA_DRAG_BACKEND", "playwright").strip().lower() == "os"
                and hasattr(page, "bring_to_front")
                and hasattr(page, "evaluate")
                and not ensure_page_topmost_foreground(page, label=label, attempts=2, delay=0.2)
            ):
                last_message = "OS 前台焦点确认失败"
                print(f"  ⚠️ {prefix}{last_message}，重新聚焦后重试")
                _sleep(0.5, stop_event)
                continue

            screenshot_path = str(Path(image_dir) / f"captcha_ai_{int(time.time() * 1000)}_{attempt}.png")
            _screenshot_locator(root, screenshot_path)
            last_screenshot = screenshot_path
            print(f"  🖼️ {prefix}验证码截图: {screenshot_path}")

            drag_plan = _build_drag_plan(page, root, 0)
            drag_plan["label"] = label
            ai_distance = 0
            if mode == "ai" and os.getenv("CAPTCHA_AI_FORCE_DISTANCE", "").strip() and config.api_key:
                if ai_client is None:
                    ai_client = CaptchaAiClient(
                        base_url=config.base_url,
                        api_key=config.api_key,
                        model=config.model,
                        timeout=config.timeout,
                )
                print(f"  🌐 {prefix}实验模式：强制请求滑块 AI 距离...")
                ai_distance = ai_client.ask_slider_distance(screenshot_path)
                try:
                    ai_scale = float(os.getenv("CAPTCHA_AI_DISTANCE_SCALE", "1.0"))
                except Exception:
                    ai_scale = 1.0
                scaled_ai_distance = max(20.0, float(ai_distance) * ai_scale)
                drag_plan["distance"] = scaled_ai_distance
                drag_plan["calibrate_target_x"] = None
                drag_plan["source"] = "AI强制距离"
                drag_plan["alternatives"] = [scaled_ai_distance]
                drag_plan["ai_distance_scale"] = ai_scale
            local_source = drag_plan.get("source")
            if mode == "ddddocr" and local_source not in {"ddddocr", "图像匹配校准"}:
                last_message = "ddddocr 本地识别未能得到可用滑块缺口"
                print(f"  ⚠️ {prefix}{last_message}，尝试刷新后重试")
                _try_refresh_captcha(root)
                _sleep(0.5, stop_event)
                continue
            if mode == "ai":
                if ai_client is None:
                    ai_client = CaptchaAiClient(
                        base_url=config.base_url,
                        api_key=config.api_key,
                        model=config.model,
                        timeout=config.timeout,
                    )
                if timed_out():
                    return CaptchaSolverResult(ok=False, message="AI 滑块处理整体超时", attempts=attempt - 1)
                print(f"  🌐 {prefix}正在请求滑块 AI 识别距离...")
                ai_distance = ai_client.ask_slider_distance(screenshot_path)
                drag_plan = _build_drag_plan(page, root, ai_distance)
                drag_plan["label"] = label
                local_source = drag_plan.get("source")
            elif local_source in {"ddddocr", "图像匹配校准", "AI强制距离"}:
                print(f"  🧩 {prefix}{local_source} 已识别滑块缺口，优先使用本地距离")
            else:
                if ai_client is None:
                    if not config.api_key:
                        return CaptchaSolverResult(
                            ok=False,
                            message="未配置滑块 AI API 密钥，且 ddddocr 未能识别滑块缺口",
                            attempts=attempt - 1,
                            screenshot_path=last_screenshot,
                        )
                    ai_client = CaptchaAiClient(
                        base_url=config.base_url,
                        api_key=config.api_key,
                        model=config.model,
                        timeout=config.timeout,
                    )
                print(f"  🌐 {prefix}正在请求滑块 AI 识别距离...")
                ai_distance = ai_client.ask_slider_distance(screenshot_path)
                drag_plan = _build_drag_plan(page, root, ai_distance)
                drag_plan["label"] = label
            current_signature = _drag_plan_target_signature(drag_plan)
            same_target_signature = current_signature == last_target_signature
            if same_target_signature:
                repeated_target_index += 1
            else:
                repeated_target_index = 0
                last_corrected_target_x = None
            last_target_signature = current_signature
            target_alternatives = drag_plan.get("target_x_alternatives")
            if last_corrected_target_x is not None:
                if isinstance(target_alternatives, list):
                    base_targets = list(target_alternatives)
                    if all(abs(float(last_corrected_target_x) - float(seen)) >= 3.0 for seen in base_targets):
                        target_alternatives = base_targets[:2] + [last_corrected_target_x] + base_targets[2:]
                    else:
                        target_alternatives = base_targets
                else:
                    target_alternatives = [last_corrected_target_x]
            if isinstance(target_alternatives, list) and target_alternatives and drag_plan.get("calibrate_target_x") is not None:
                selected_target_index = repeated_target_index % len(target_alternatives)
                raw_target_x = float(target_alternatives[selected_target_index])
                max_distance = float(drag_plan.get("max_distance", max(float(drag_plan["distance"]), 20.0)))
                scale = float(drag_plan.get("target_scale", 1.0) or 1.0)
                target_x = _apply_target_right_bias(raw_target_x, max_distance)
                drag_plan["calibrate_target_x"] = target_x
                drag_plan["distance"] = max(20.0, min(target_x * scale, max_distance))
                drag_plan["selected_target_x"] = target_x
                drag_plan["raw_selected_target_x"] = raw_target_x
                drag_plan["target_right_bias"] = target_x - raw_target_x
                drag_plan["selected_target_index"] = selected_target_index
            else:
                alternatives = drag_plan.get("alternatives") or [drag_plan["distance"]]
                if isinstance(alternatives, list) and alternatives:
                    drag_plan["distance"] = float(alternatives[(attempt - 1) % len(alternatives)])
            source = drag_plan.get("source", "AI")
            planned_distance = int(round(float(drag_plan["distance"])))
            if any(drag_plan.get(key) is not None for key in ("ddddocr_target_x", "white_gap_target_x", "match_target_x")):
                print(
                    f"  🔎 {prefix}候选位置: "
                    f"ddddocr={_format_optional_px(drag_plan.get('ddddocr_target_x'))}, "
                    f"亮色缺口={_format_optional_px(drag_plan.get('white_gap_target_x'))}, "
                    f"模板匹配={_format_optional_px(drag_plan.get('match_target_x'))}"
                )
            target_right_bias = drag_plan.get("target_right_bias")
            raw_selected_target_x = drag_plan.get("raw_selected_target_x")
            selected_target_x = drag_plan.get("selected_target_x")
            if (
                target_right_bias is not None
                and raw_selected_target_x is not None
                and selected_target_x is not None
                and abs(float(target_right_bias)) >= 0.1
            ):
                print(
                    f"  🎚️ {prefix}释放目标右偏: "
                    f"{float(raw_selected_target_x):.1f}px -> {float(selected_target_x):.1f}px "
                    f"({float(target_right_bias):+.1f}px)"
                )
            if ai_distance:
                print(f"  📏 {prefix}AI 估算距离: {ai_distance}px，{source} 计划拖动: {planned_distance}px")
            else:
                print(f"  📏 {prefix}{source} 计划拖动: {planned_distance}px")

            if _is_cancelled(stop_event):
                return CaptchaSolverResult(ok=False, message="AI 滑块处理已取消", attempts=attempt - 1)

            actual_distance = _perform_drag_with_focus_retry(
                page,
                drag_plan,
                prefix=prefix,
                stop_event=stop_event,
            )
            last_distance = int(round(actual_distance))
            adjustment = drag_plan.get("local_adjustment")
            if adjustment:
                applied_adjustment = drag_plan.get("applied_local_adjustment", adjustment)
                print(f"  🧭 {prefix}本地微调: 建议 {float(adjustment):+.1f}px，实际 {float(applied_adjustment):+.1f}px")
            release_backoff = drag_plan.get("release_backoff")
            if isinstance(release_backoff, dict):
                print(
                    f"  ↩️ {prefix}释放前回拉: "
                    f"delta {float(release_backoff.get('before_delta', 0)):+.1f}px"
                    f" -> {float(release_backoff.get('after_delta', release_backoff.get('before_delta', 0))):+.1f}px, "
                    f"回拉 {float(release_backoff.get('backoff', 0)):.1f}px"
                )
            hold_state = drag_plan.get("hold_state")
            probe_state = drag_plan.get("probe_state")
            if isinstance(probe_state, dict):
                print(
                    f"  🧪 {prefix}探测状态: "
                    f"sliderLeft={_format_optional_px(probe_state.get('sliderLeft'))}, "
                    f"puzzleLeft={_format_optional_px(probe_state.get('puzzleLeft'))}"
                )
            if drag_plan.get("probe_calibrated_distance") is not None:
                print(f"  🎯 {prefix}比例校准后距离: {float(drag_plan['probe_calibrated_distance']):.1f}px")
            if drag_plan.get("ratio_human_distance") is not None:
                print(
                    f"  🎯 {prefix}探测比例人类轨迹: "
                    f"ratio={float(drag_plan.get('ratio_human_ratio', 0)):.3f}, "
                    f"probe={float(drag_plan.get('ratio_human_probe_distance', 0)):.1f}px, "
                    f"distance={float(drag_plan['ratio_human_distance']):.1f}px"
                )
            elif drag_plan.get("ratio_human_ignored_ratio") is not None:
                print(
                    f"  ⚠️ {prefix}探测比例异常，已忽略: "
                    f"ratio={float(drag_plan['ratio_human_ignored_ratio']):.3f}, "
                    f"probe={float(drag_plan.get('ratio_human_probe_distance', 0)):.1f}px"
                )
            if drag_plan.get("quadratic_strategy_distance") is not None:
                strategy_text = "快速三段" if drag_plan.get("fast_quadratic") else "二次曲线"
                print(f"  🎯 {prefix}{strategy_text}反算距离: {float(drag_plan['quadratic_strategy_distance']):.1f}px")
            if drag_plan.get("scaled_strategy_distance") is not None:
                print(f"  🎯 {prefix}缩放轨迹距离: {float(drag_plan['scaled_strategy_distance']):.1f}px")
            if drag_plan.get("drag_backend"):
                print(f"  🖱️ {prefix}拖动后端: {drag_plan['drag_backend']}")
            if isinstance(drag_plan.get("os_probe"), dict):
                probe = drag_plan["os_probe"]
                print(
                    f"  🧷 {prefix}OS 坐标探测: "
                    f"client=({_format_optional_px(probe.get('clientX'))}, {_format_optional_px(probe.get('clientY'))}), "
                    f"screen=({_format_optional_px(probe.get('screenX'))}, {_format_optional_px(probe.get('screenY'))}), "
                    f"scale=({float(probe.get('scaleX', 0)):.2f}, {float(probe.get('scaleY', 0)):.2f})"
                )
            if isinstance(hold_state, dict):
                print(
                    f"  📌 {prefix}松手前状态: "
                    f"sliderLeft={_format_optional_px(hold_state.get('sliderLeft'))}, "
                    f"puzzleLeft={_format_optional_px(hold_state.get('puzzleLeft'))}, "
                    f"sliderBoxX={_format_optional_px(hold_state.get('sliderBoxX'))}, "
                    f"puzzleBoxX={_format_optional_px(hold_state.get('puzzleBoxX'))}"
                )
                target_x = drag_plan.get("calibrate_target_x")
                puzzle_left = hold_state.get("puzzleLeft")
                if target_x is not None and puzzle_left is not None:
                    try:
                        delta = float(target_x) - float(puzzle_left)
                        print(f"  📐 {prefix}松手前对齐: target={float(target_x):.1f}px puzzle={float(puzzle_left):.1f}px delta={delta:+.1f}px")
                        if math.isfinite(delta) and abs(delta) > _alignment_target_tolerance() and abs(delta) <= 40.0:
                            last_corrected_target_x = max(20.0, min(float(target_x) + delta, float(drag_plan.get("max_distance", 300.0))))
                    except Exception:
                        pass
            print(f"  🖱️ {prefix}实际拖动距离: {last_distance}px")
            success_source = _wait_for_captcha_success(
                page,
                network_debug,
                stop_event=stop_event,
                timeout=_captcha_success_wait_seconds(),
            )
            if success_source:
                success_message = "AI 滑块处理成功"
                if success_source != "页面":
                    success_message = f"{success_message}（{success_source}已确认）"
                return CaptchaSolverResult(
                    ok=True,
                    message=success_message,
                    attempts=attempt,
                    distance=last_distance,
                    screenshot_path=last_screenshot,
                )

            last_message = "滑块仍未通过"
            if network_debug:
                _dump_captcha_network_debug(network_debug, prefix)
                _dump_captcha_dom_debug(page, prefix)
            print(f"  ⚠️ {prefix}{last_message}，刷新验证码后重试")
            try:
                _try_refresh_captcha(root)
            except Exception:
                pass
            _sleep(_captcha_retry_reset_wait_seconds(), stop_event)
            _wait_for_captcha_ready(page, stop_event=stop_event, timeout=5)
        except Exception as exc:
            last_message = str(exc)
            print(f"  ⚠️ {prefix}AI 滑块处理异常: {last_message}")
            try:
                root = _find_captcha_root(page)
                if root is not None:
                    _try_refresh_captcha(root)
                    _wait_for_captcha_ready(page, stop_event=stop_event, timeout=5)
            except Exception:
                pass
            _sleep(1, stop_event)

    return CaptchaSolverResult(
        ok=False,
        message=f"AI 滑块处理失败: {last_message}",
        attempts=max(1, config.attempts),
        distance=last_distance,
        screenshot_path=last_screenshot,
    )


def _is_focus_miss_drag_exception(exc: Exception, plan: dict[str, Any]) -> bool:
    if bool(plan.get("focus_missed")):
        return True
    message = str(exc)
    return any(
        text in message
        for text in (
            "未命中滑块窗口",
            "前台焦点确认失败",
            "放弃按下滑块",
        )
    )


def _perform_drag_with_focus_retry(
    page: Any,
    plan: dict[str, Any],
    *,
    prefix: str = "",
    stop_event: Optional[Any] = None,
) -> float:
    """执行拖动；OS 焦点抖动时不消耗验证码识别次数，短重聚焦再试一次。"""
    last_exc: Optional[Exception] = None
    for retry_index in range(2):
        try:
            return _perform_drag(page, plan)
        except Exception as exc:
            last_exc = exc
            if retry_index > 0 or not _is_focus_miss_drag_exception(exc, plan) or _is_cancelled(stop_event):
                raise
            plan["focus_retry_count"] = int(plan.get("focus_retry_count") or 0) + 1
            plan.pop("focus_missed", None)
            label = str(plan.get("label") or "").strip()
            print(
                f"  🔁 {prefix}OS 鼠标未命中滑块窗口，正在重新激活窗口并重试本次拖动",
                flush=True,
            )
            ensure_page_topmost_foreground(page, label=label, attempts=3, delay=0.2)
            time.sleep(0.2)
    if last_exc is not None:
        raise last_exc
    raise RuntimeError("滑块拖动未执行")



def install_aliyun_verify_success_route(page: Any) -> None:
    """本地靶场实验：把 Aliyun VerifyCaptchaV2 响应替换为成功。

    默认不调用。该函数用于验证 Qwen 前端成功链路和本地靶场集成，不改变真实
    Aliyun 轨迹生成逻辑。
    """
    def handler(route: Any) -> None:
        try:
            request = route.request
            post_data = str(getattr(request, "post_data", "") or "")
            if "VerifyCaptchaV2" not in post_data:
                return route.continue_()
            summary = _summarize_verify_record({"post_data": post_data})
            certify_id = str(summary.get("certifyId") or "")
            body = json.dumps(
                {
                    "RequestId": "LOCAL-CTF-BYPASS",
                    "Message": "success",
                    "HttpStatusCode": 200,
                    "Code": "Success",
                    "Success": True,
                    "Result": {
                        "VerifyCode": "PASS",
                        "VerifyResult": True,
                        "certifyId": certify_id,
                    },
                },
                ensure_ascii=False,
            )
            route.fulfill(
                status=200,
                headers={"content-type": "application/json;charset=utf-8"},
                body=body,
            )
        except Exception:
            try:
                route.continue_()
            except Exception:
                pass

    try:
        page.route("**/*", handler)
    except Exception:
        pass


def install_aliyun_callback_probe(page: Any) -> None:
    """注入 AliyunCaptcha/Qwen 注册探针，记录验证码业务层入参、回调源码和注册请求。"""
    script = r"""
    (() => {
      if (window.__captchaCallbackProbeInstalled) return;
      window.__captchaCallbackProbeInstalled = true;
      window.__captchaCallbackProbe = [];
      window.__qwenSignupProbe = [];
      const redact = value => {
        try {
          const text = typeof value === 'string' ? value : JSON.stringify(value);
          if (!text) return text;
          return text.length > 1200 ? text.slice(0, 1200) + '...[truncated]' : text;
        } catch (e) {
          return String(value);
        }
      };
      const sourceOf = fn => {
        try { return redact(String(fn)); } catch (e) { return String(e); }
      };
      const extractUrlSummary = rawUrl => {
        const summary = {};
        try {
          const parsed = new URL(String(rawUrl || ''), location.href);
          summary.path = parsed.pathname;
          for (const key of ['u_atoken', 'u_asig', 'u_aref']) {
            if (parsed.searchParams.has(key)) {
              const value = parsed.searchParams.get(key);
              summary[key] = value === null ? null : String(value).slice(0, 220);
            }
          }
        } catch (e) {}
        return summary;
      };
      const shouldRecordSignup = (url, body) => {
        const text = `${url || ''}\n${body || ''}`.toLowerCase();
        return text.includes('/auths/signup') || text.includes('signup') || text.includes('u_asig') || text.includes('u_aref');
      };
      const recordSignup = item => {
        try {
          item.t = performance.now();
          item.stack = redact(new Error().stack);
          item.urlSummary = extractUrlSummary(item.url);
          window.__qwenSignupProbe.push(item);
          if (window.__qwenSignupProbe.length > 80) window.__qwenSignupProbe.shift();
        } catch (e) {}
        return item;
      };
      const hookFetch = () => {
        if (window.__qwenSignupFetchHooked || typeof window.fetch !== 'function') return;
        window.__qwenSignupFetchHooked = true;
        const originalFetch = window.fetch;
        window.fetch = function(input, init) {
          const url = typeof input === 'string' ? input : (input && input.url) || '';
          const method = (init && init.method) || (input && input.method) || 'GET';
          const body = init && init.body !== undefined ? init.body : '';
          const bodyText = typeof body === 'string' ? body : (body ? Object.prototype.toString.call(body) : '');
          const item = shouldRecordSignup(url, bodyText) ? recordSignup({
            kind: 'fetch',
            method: String(method),
            url: String(url),
            body: redact(bodyText),
          }) : null;
          try {
            const promise = originalFetch.apply(this, arguments);
            if (item && promise && typeof promise.then === 'function') {
              promise.then(response => {
                try {
                  item.status = response.status;
                  item.responseUrl = response.url;
                  const clone = response.clone && response.clone();
                  if (clone && typeof clone.text === 'function') {
                    clone.text().then(text => { item.responseText = redact(text); }).catch(error => { item.responseError = String(error); });
                  }
                } catch (error) {
                  item.responseError = String(error);
                }
              }).catch(error => { item.error = String(error); });
            }
            return promise;
          } catch (error) {
            if (item) item.error = String(error && (error.stack || error.message) || error);
            throw error;
          }
        };
      };
      const hookXhr = () => {
        if (window.__qwenSignupXhrHooked || !window.XMLHttpRequest) return;
        window.__qwenSignupXhrHooked = true;
        const originalOpen = XMLHttpRequest.prototype.open;
        const originalSend = XMLHttpRequest.prototype.send;
        XMLHttpRequest.prototype.open = function(method, url) {
          try {
            this.__qwenSignupProbeInfo = { method: String(method), url: String(url) };
          } catch (e) {}
          return originalOpen.apply(this, arguments);
        };
        XMLHttpRequest.prototype.send = function(body) {
          const info = this.__qwenSignupProbeInfo || {};
          const bodyText = typeof body === 'string' ? body : (body ? Object.prototype.toString.call(body) : '');
          const item = shouldRecordSignup(info.url, bodyText) ? recordSignup({
            kind: 'xhr',
            method: info.method || '',
            url: info.url || '',
            body: redact(bodyText),
          }) : null;
          if (item) {
            try {
              this.addEventListener('loadend', () => {
                try {
                  item.status = this.status;
                  item.responseUrl = this.responseURL;
                  item.responseText = redact(this.responseText || '');
                } catch (error) {
                  item.responseError = String(error);
                }
              });
            } catch (e) {}
          }
          return originalSend.apply(this, arguments);
        };
      };
      hookFetch();
      hookXhr();
      const wrapConfig = config => {
        if (!config || config.__captchaProbeWrapped) return config;
        window.__captchaLastConfig = config;
        try {
          Object.defineProperty(config, '__captchaProbeWrapped', { value: true, configurable: true });
        } catch (e) {}
        const configSources = {};
        for (const name of ['captchaVerifyCallback', 'success', 'fail', 'onBizResultCallback']) {
          if (typeof config[name] === 'function') configSources[name] = sourceOf(config[name]);
        }
        window.__captchaCallbackProbe.push({
          kind: 'configSources',
          t: performance.now(),
          configKeys: Object.keys(config || {}),
          sources: configSources,
          userCertifyId: redact(config.UserCertifyId || config.CertifyId || ''),
        });
        const originalVerify = config.captchaVerifyCallback;
        if (typeof originalVerify === 'function') {
          config.captchaVerifyCallback = async function(param, callback) {
            const item = {
              kind: 'captchaVerifyCallback',
              t: performance.now(),
              param: redact(param),
              stack: redact(new Error().stack),
              source: sourceOf(originalVerify),
            };
            window.__captchaCallbackProbe.push(item);
            const wrappedCallback = function(result) {
              item.callbackResult = redact(result);
              window.__captchaCallbackProbe.push({
                kind: 'captchaVerifyCallback.callback',
                t: performance.now(),
                result: redact(result),
                stack: redact(new Error().stack),
              });
              return callback && callback.apply(this, arguments);
            };
            try {
              const ret = await originalVerify.call(this, param, wrappedCallback);
              item.returnValue = redact(ret);
              return ret;
            } catch (error) {
              item.error = String(error && (error.stack || error.message) || error);
              throw error;
            }
          };
        }
        const originalGetInstance = config.getInstance;
        if (typeof originalGetInstance === 'function') {
          config.getInstance = function(instance) {
            try {
              window.__captchaLastInstance = instance;
              window.__captchaCallbackProbe.push({
                kind: 'getInstance',
                t: performance.now(),
                instanceKeys: instance ? Object.keys(instance).slice(0, 80) : [],
                hasOnBizSuccess: !!(instance && typeof instance.onBizSuccess === 'function'),
                hasOnBizFail: !!(instance && typeof instance.onBizFail === 'function'),
                verifyType: redact(config.verifyType || ''),
                sceneId: redact(config.SceneId || config.sceneId || ''),
                securityTokenLength: String(config.securityToken || '').length,
              });
            } catch (e) {}
            return originalGetInstance.apply(this, arguments);
          };
        }
        for (const name of ['success', 'fail', 'onBizResultCallback']) {
          const original = config[name];
          if (typeof original === 'function') {
            config[name] = function() {
              window.__captchaCallbackProbe.push({
                kind: name,
                t: performance.now(),
                args: Array.from(arguments).map(redact),
                stack: redact(new Error().stack),
                source: redact(String(original)),
              });
              return original.apply(this, arguments);
            };
          }
        }
        return config;
      };
      let currentInit = window.initAliyunCaptcha;
      Object.defineProperty(window, 'initAliyunCaptcha', {
        configurable: true,
        get() { return currentInit; },
        set(fn) {
          if (typeof fn !== 'function') {
            currentInit = fn;
            return;
          }
          currentInit = function(config) {
            window.__captchaCallbackProbe.push({ kind: 'initAliyunCaptcha', t: performance.now(), configKeys: Object.keys(config || {}) });
            const instance = fn.call(this, wrapConfig(config));
            try { window.__captchaLastInstance = instance; } catch (e) {}
            return instance;
          };
        }
      });
      if (typeof currentInit === 'function') {
        window.initAliyunCaptcha = currentInit;
      }
    })();
    """
    try:
        page.add_init_script(script)
    except Exception:
        pass


def _try_aliyun_callback_success(page: Any, certify_id: str = "") -> bool:
    """本地靶场实验：尝试直接触发 AliyunCaptcha 成功回调。默认不启用。

    Aliyun 前端成功路径会把 certifyId 包装成 base64 JSON 字符串后传给业务 success 回调；
    Qwen 业务回调会把这个字符串作为 u_asig。旧的对象入参会变成 [object Object]。
    """
    script = """
    (certifyId) => {
      const config = window.__captchaLastConfig;
      if (!config) return false;
      const realCertifyId = certifyId || config.UserCertifyId || config.CertifyId || '';
      const callbackBypassMode = String(window.CAPTCHA_CALLBACK_BYPASS_MODE || 'auto');
      const signPayload = window.btoa(JSON.stringify({
        certifyId: realCertifyId,
        sceneId: config.SceneId || config.sceneId || '',
        isSign: true,
        securityToken: config.securityToken || '',
      }));
      const objectPayload = {
        success: true,
        verifyResult: true,
        verifyCode: 'PASS',
        certifyId: realCertifyId,
      };
      window.__captchaCallbackProbe = window.__captchaCallbackProbe || [];
      window.__captchaCallbackProbe.push({
        kind: 'callbackBypassAttempt',
        t: performance.now(),
        mode: callbackBypassMode,
        signPayload,
        objectPayload: JSON.stringify(objectPayload),
        stack: String(new Error().stack || ''),
      });
      let called = false;
      const instance = window.__captchaLastInstance;
      if ((callbackBypassMode === 'auto' || callbackBypassMode === 'onBizSuccess') && instance && typeof instance.onBizSuccess === 'function') {
        instance.onBizSuccess(realCertifyId);
        called = true;
      }
      if (!called && typeof config.success === 'function') {
        if (callbackBypassMode === 'object') {
          config.success(objectPayload);
        } else {
          config.success(signPayload);
        }
        called = true;
      }
      if (typeof config.onBizResultCallback === 'function') {
        config.onBizResultCallback(true);
        called = true;
      }
      try {
        const root = document.querySelector('#waf_nc_block');
        if (root) root.style.display = 'none';
      } catch (e) {}
      return called;
    }
    """
    try:
        return bool(page.evaluate(script, certify_id))
    except TypeError:
        try:
            return bool(page.evaluate(script))
        except Exception:
            return False
    except Exception:
        return False



def _image_to_base64(image_path: str) -> str:
    with open(image_path, "rb") as image_file:
        return base64.b64encode(image_file.read()).decode("utf-8")


def _parse_distance(content: Any) -> int:
    text = str(content).strip()
    matches = re.findall(r"-?\d+", text)
    if not matches:
        raise ValueError(f"AI 未返回有效距离: {text[:80]}")
    # 从后往前选一个合理像素值，避免误取日志 ID 或说明文字里的数字。
    distance = None
    for raw in reversed(matches):
        candidate = abs(int(raw))
        if 10 <= candidate <= 600:
            distance = candidate
            break
    if distance is None:
        distance = min(abs(int(matches[-1])), 600)
    if distance < 10:
        raise ValueError(f"AI 返回距离过小，无法执行滑动: {text[:80]}")
    return min(distance, 600)


def _parse_signed_adjustment(content: Any) -> int:
    text = str(content).strip()
    lower = text.lower()
    signed = re.findall(r"[+-]\s*\d+", text)
    if signed:
        value = int(signed[-1].replace(" ", ""))
    else:
        matches = re.findall(r"\d+", text)
        if not matches:
            if any(word in lower for word in ("aligned", "对齐", "正好", "无需", "不用")):
                return 0
            if any(word in lower for word in ("left", "左")):
                return -10
            if any(word in lower for word in ("right", "右")):
                return 10
            return 0
        value = int(matches[-1])
        if any(word in lower for word in ("left", "左")) and not any(word in lower for word in ("right", "右")):
            value = -value
    return max(-25, min(25, value))


def _attach_captcha_network_debug(page: Any, prefix: str = "") -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []

    def on_response(response: Any) -> None:
        try:
            url = str(response.url)
            lower = url.lower()
            if not any(key in lower for key in (
                "aliyun",
                "captcha",
                "risk",
                "nvc",
                "umid",
                "verify",
                "/auth",
                "register",
                "signup",
                "account",
            )):
                return
            record: dict[str, Any] = {"status": response.status, "url": url}
            try:
                req = response.request
                record["method"] = req.method
                post_data = req.post_data
                if post_data:
                    record["post_data"] = str(post_data)[:5000]
                    summary = _summarize_verify_record({"post_data": str(post_data), "url": url})
                    if summary:
                        record["summary"] = summary
            except Exception:
                pass
            try:
                ctype = response.headers.get("content-type", "")
                if "json" in ctype or "text" in ctype or "javascript" in ctype:
                    text = response.text() if callable(getattr(response, "text", None)) else ""
                    record["body"] = text[:500]
                    summary = _summarize_verify_record(record)
                    if summary:
                        record["summary"] = summary
            except Exception as exc:
                record["body_error"] = str(exc)[:120]
            records.append(record)
            print(f"  🌐 {prefix}验证码/注册网络: {record['status']} {url[:160]}")
            body = str(record.get("body") or "")
            if "VerifyResult" in body or "VerifyCode" in body:
                print(f"  🧾 {prefix}验证码结果: {body[:260]}")
        except Exception:
            pass

    try:
        page.on("response", on_response)
    except Exception:
        pass
    return records


def _summarize_verify_record(record: dict[str, Any]) -> dict[str, Any]:
    """从 Aliyun VerifyCaptchaV2 / Qwen signup 请求响应中提取便于对比的摘要字段。"""
    summary: dict[str, Any] = {}
    post_data = str(record.get("post_data") or "")
    body = str(record.get("body") or "")
    url = str(record.get("url") or "")
    if url:
        try:
            parsed_url = urlparse(url)
            if parsed_url.query:
                query = parse_qs(parsed_url.query, keep_blank_values=True)
                for key in ("u_atoken", "u_asig", "u_aref"):
                    value = (query.get(key) or [""])[0]
                    if value:
                        summary[key] = value[:220]
        except Exception:
            pass
    if post_data:
        try:
            params = parse_qs(post_data, keep_blank_values=True)
            action = (params.get("Action") or [""])[0]
            if action:
                summary["action"] = action
            for key in ("SceneId", "CertifyId"):
                value = (params.get(key) or [""])[0]
                if value:
                    summary[key[0].lower() + key[1:]] = value
            raw_verify_param = (params.get("CaptchaVerifyParam") or [""])[0]
            if raw_verify_param:
                try:
                    verify_param = json.loads(unquote_plus(raw_verify_param))
                except Exception:
                    verify_param = {}
                if isinstance(verify_param, dict):
                    for key in ("sceneId", "certifyId"):
                        value = verify_param.get(key)
                        if value:
                            summary[key] = value
                    data_value = str(verify_param.get("data") or "")
                    device_token = str(verify_param.get("deviceToken") or "")
                    if data_value:
                        summary["dataLength"] = len(data_value)
                        summary["dataHead"] = data_value[:24]
                    if device_token:
                        summary["deviceTokenLength"] = len(device_token)
        except Exception:
            pass

    if body:
        try:
            parsed = json.loads(body)
            result = parsed.get("Result") if isinstance(parsed, dict) else None
            if isinstance(result, dict):
                if "VerifyCode" in result:
                    summary["verifyCode"] = result.get("VerifyCode")
                if "VerifyResult" in result:
                    summary["verifyResult"] = result.get("VerifyResult")
                if "certifyId" in result:
                    summary.setdefault("certifyId", result.get("certifyId"))
        except Exception:
            pass
    return summary

def attach_manual_trace_recorder(page: Any, *, label: str = "", image_dir: str = "images") -> Optional[list[dict[str, Any]]]:
    """启用人工滑块轨迹录制；用于先人工成功一次，再复用曲线自动重放。"""
    if not os.getenv("CAPTCHA_RECORD_TRACE", "").strip():
        return None
    prefix = f"{label} " if label else ""
    try:
        page.evaluate(
            """
            () => {
              if (window.__captchaManualTraceInstalled) return;
              window.__captchaManualTraceInstalled = true;
              window.__captchaManualTrace = { points: [], down: null, up: null };
              const record = (kind, event) => {
                const now = performance.now();
                const item = {
                  kind,
                  t: now,
                  x: event.clientX,
                  y: event.clientY,
                  screenX: event.screenX,
                  screenY: event.screenY,
                  buttons: event.buttons,
                };
                if (kind === 'down') window.__captchaManualTrace.down = item;
                if (kind === 'up') window.__captchaManualTrace.up = item;
                window.__captchaManualTrace.points.push(item);
              };
              document.addEventListener('mousedown', event => record('down', event), true);
              document.addEventListener('mousemove', event => {
                if (event.buttons) record('move', event);
              }, true);
              document.addEventListener('mouseup', event => record('up', event), true);
              document.addEventListener('touchstart', event => {
                const t = event.touches && event.touches[0];
                if (t) record('down', t);
              }, true);
              document.addEventListener('touchmove', event => {
                const t = event.touches && event.touches[0];
                if (t) record('move', t);
              }, true);
              document.addEventListener('touchend', event => {
                const t = event.changedTouches && event.changedTouches[0];
                if (t) record('up', t);
              }, true);
            }
            """
        )
        records = _attach_captcha_network_debug(page, prefix)
        print(f"  🎥 {prefix}已启用人工滑块轨迹录制")
        return records
    except Exception as exc:
        print(f"  ⚠️ {prefix}启用人工轨迹录制失败: {exc}")
        return None


def dump_manual_trace_recording(
    page: Any,
    records: Optional[list[dict[str, Any]]] = None,
    *,
    label: str = "",
    image_dir: str = "images",
) -> Optional[str]:
    """保存人工滑块轨迹和最近验证码网络结果。"""
    if records is None and not os.getenv("CAPTCHA_RECORD_TRACE", "").strip():
        return None
    prefix = f"{label} " if label else ""
    try:
        trace = page.evaluate(
            """
            () => {
              const trace = window.__captchaManualTrace || { points: [] };
              const points = Array.isArray(trace.points) ? trace.points : [];
              const first = points[0] || null;
              const last = points[points.length - 1] || null;
              return {
                points,
                start: first,
                end: last,
                duration: first && last ? Math.max(0, last.t - first.t) : 0,
                distance: first && last ? last.x - first.x : 0,
                url: location.href,
                userAgent: navigator.userAgent,
              };
            }
            """
        )
    except Exception:
        trace = {"points": []}
    if not isinstance(trace, dict):
        trace = {"points": []}
    if not trace or not trace.get("points"):
        print(f"  ⚠️ {prefix}没有采集到人工滑块轨迹")
        return None

    verify_records = []
    for record in records or []:
        if not isinstance(record, dict):
            continue
        body = str(record.get("body") or "")
        post_data = str(record.get("post_data") or "")
        url = str(record.get("url") or "")
        if "VerifyCaptchaV2" in post_data or "VerifyResult" in body or "captcha-open" in url:
            summary = _summarize_verify_record(record)
            if summary:
                record = dict(record)
                record["summary"] = summary
            verify_records.append(record)
    trace["verify_records"] = verify_records[-5:]
    trace["verify_summaries"] = [record.get("summary") for record in trace["verify_records"] if record.get("summary")]

    out_dir = Path(image_dir) / "aliyun_probe"
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"manual_trace_{int(time.time() * 1000)}.json"
    try:
        path.write_text(json.dumps(trace, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"  🎥 {prefix}人工滑块轨迹已保存: {path}")
        return str(path)
    except Exception as exc:
        print(f"  ⚠️ {prefix}保存人工轨迹失败: {exc}")
        return None


def _dump_captcha_network_debug(records: list[dict[str, Any]], prefix: str = "") -> None:
    if not records:
        print(f"  🌐 {prefix}验证码网络: 未捕获到相关响应")
        return
    out_dir = Path("images") / "aliyun_probe"
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"network_{int(time.time() * 1000)}.json"
    try:
        import json

        path.write_text(json.dumps(records[-20:], ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"  🌐 {prefix}验证码网络记录: {path}")
    except Exception:
        pass


def _dump_captcha_dom_debug(page: Any, prefix: str = "") -> None:
    try:
        data = page.evaluate(
            """
            () => {
              const pickOuter = selector => {
                const el = document.querySelector(selector);
                return el ? el.outerHTML.slice(0, 2000) : null;
              };
              const resources = performance.getEntriesByType('resource')
                .map(r => r.name)
                .filter(name => /captcha|aliyun|cloudauth|umid|nvc|risk/i.test(name))
                .slice(-80);
              const scripts = Array.from(document.scripts)
                .map(s => s.src || s.textContent.slice(0, 200))
                .filter(Boolean)
                .filter(src => /captcha|aliyun|cloudauth|umid|nvc|risk|滑块|验证码/i.test(src))
                .slice(-80);
              const winKeys = Object.keys(window)
                .filter(k => /captcha|aliyun|nvc|umid|risk|滑块|验证/i.test(k))
                .slice(0, 120);
              const storage = {};
              for (const storeName of ['localStorage', 'sessionStorage']) {
                try {
                  storage[storeName] = {};
                  const store = window[storeName];
                  for (let i = 0; i < store.length; i++) {
                    const key = store.key(i);
                    if (/captcha|aliyun|nvc|umid|risk/i.test(key)) {
                      storage[storeName][key] = String(store.getItem(key)).slice(0, 500);
                    }
                  }
                } catch (e) {}
              }
              return {
                url: location.href,
                title: document.title,
                bodyText: document.body ? document.body.innerText.slice(0, 2000) : '',
                root: pickOuter('#aliyunCaptcha-window-popup, #waf_nc_block, [class*=captcha], [class*=Captcha]'),
                img: pickOuter('#aliyunCaptcha-img'),
                puzzle: pickOuter('#aliyunCaptcha-puzzle'),
                slider: pickOuter('#aliyunCaptcha-sliding-slider'),
                resources,
                scripts,
                winKeys,
                storage,
                callbackProbe: window.__captchaCallbackProbe || [],
                signupProbe: window.__qwenSignupProbe || [],
              };
            }
            """
        )
        out_dir = Path("images") / "aliyun_probe"
        out_dir.mkdir(parents=True, exist_ok=True)
        path = out_dir / f"dom_debug_{int(time.time() * 1000)}.json"
        import json

        path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"  🧬 {prefix}验证码 DOM 记录: {path}")
    except Exception:
        pass


def _format_optional_px(value: Any) -> str:
    if value is None:
        return "无"
    try:
        return f"{float(value):.1f}px"
    except Exception:
        return str(value)


def _find_captcha_root(page: Any):
    selectors = [
        "#waf_nc_block",
        ".geetest_window",
        "[class*='captcha']",
        "[class*='Captcha']",
    ]
    for selector in selectors:
        try:
            locator = _first_locator(page.locator(selector))
            if _is_visible(locator):
                return locator
        except Exception:
            continue
    return None


def _is_visible(locator: Any) -> bool:
    try:
        return bool(locator.is_visible(timeout=1000))
    except TypeError:
        try:
            return bool(locator.is_visible())
        except Exception:
            return False
    except Exception:
        return False


def _screenshot_locator(locator: Any, path: str) -> None:
    locator.screenshot(path=path)


def _wait_for_captcha_ready(page: Any, *, stop_event: Optional[Any] = None, timeout: float = 8.0) -> bool:
    """等待 AliyunCaptcha 图片和滑块从加载态进入可操作态；非 Aliyun 验证码直接视为就绪。"""
    end_time = time.time() + timeout
    saw_aliyun = False
    while time.time() < end_time:
        if _is_cancelled(stop_event):
            return False
        try:
            state = page.evaluate(
                """
                () => {
                  const img = document.getElementById('aliyunCaptcha-img');
                  const puzzle = document.getElementById('aliyunCaptcha-puzzle');
                  const slider = document.getElementById('aliyunCaptcha-sliding-slider');
                  const bodyText = document.body ? String(document.body.innerText || '') : '';
                  const looksLikeAliyunLoading = (
                    bodyText.includes('访问验证') ||
                    bodyText.includes('验证您是真人') ||
                    bodyText.includes('拖动滑块完成拼图') ||
                    Boolean(document.querySelector('[id^="aliyunCaptcha"], [class*="aliyunCaptcha"]'))
                  );
                  if (!img && !puzzle && !slider) {
                    return {
                      ready: !looksLikeAliyunLoading,
                      aliyun: looksLikeAliyunLoading,
                      reason: looksLikeAliyunLoading ? 'Aliyun 验证码图片加载中' : '非 Aliyun 验证码'
                    };
                  }
                  const imageReady = el => {
                    if (!el) return false;
                    const src = String(el.currentSrc || el.src || '');
                    if (!src) return false;
                    if (src.startsWith('data:image/')) return true;
                    return el.complete !== false && (el.naturalWidth || 0) > 20 && (el.naturalHeight || 0) > 20;
                  };
                  const visibleBox = el => {
                    if (!el) return null;
                    const r = el.getBoundingClientRect();
                    return {width: r.width, height: r.height};
                  };
                  const imgBox = visibleBox(img);
                  const puzzleBox = visibleBox(puzzle);
                  const sliderBox = visibleBox(slider);
                  const ready = Boolean(
                    img && puzzle && slider &&
                    imageReady(img) && imageReady(puzzle) &&
                    imgBox && imgBox.width > 100 && imgBox.height > 80 &&
                    puzzleBox && puzzleBox.width > 10 && puzzleBox.height > 50 &&
                    sliderBox && sliderBox.width > 10 && sliderBox.height > 10
                  );
                  return {ready, aliyun: true, reason: ready ? '已就绪' : '图片加载中'};
                }
                """
            )
            if not state:
                return True
            saw_aliyun = bool(state.get("aliyun"))
            if bool(state.get("ready")):
                return True
        except Exception:
            if not saw_aliyun:
                return True
        _sleep(0.25, stop_event)
    return not saw_aliyun


def _build_drag_plan(page: Any, root: Any, ai_distance: int) -> dict[str, float]:
    slider = _find_slider_handle(root)
    slider_box = slider.bounding_box() if slider is not None else None
    root_box = root.bounding_box()
    if root_box is None:
        raise ValueError("无法获取验证码区域位置")

    if slider_box is None:
        start_x = root_box["x"] + max(20, root_box["width"] * 0.12)
        start_y = root_box["y"] + root_box["height"] * 0.78
        slider_width = 40
    else:
        start_x = slider_box["x"] + slider_box["width"] / 2
        start_y = slider_box["y"] + slider_box["height"] / 2
        slider_width = slider_box["width"]

    geometry_distance = _estimate_track_distance(root, root_box, start_x, slider_width)
    aliyun_plan = _estimate_aliyun_slider_distance(page)
    if aliyun_plan is not None:
        plan = dict(aliyun_plan)
        max_distance = float(plan.get("max_distance") or geometry_distance)
        image_distance = max(20.0, min(float(plan.get("distance", ai_distance)), max_distance))
        if plan.get("source") == "ddddocr":
            distance = image_distance
            source = "ddddocr"
        elif ai_distance > 0:
            ai_based_distance = max(20.0, min(float(ai_distance) * ALIYUN_AI_DISTANCE_FACTOR, max_distance))
            lower = max(20.0, image_distance - 60.0)
            upper = min(max_distance, image_distance + 60.0)
            distance = max(lower, min(ai_based_distance, upper))
            source = "AI+图像校验"
        else:
            distance = image_distance
            source = "图像匹配校准"
        alternatives = _distance_alternatives(distance, max_distance)
        plan.update({
            "start_x": start_x,
            "start_y": start_y,
            "distance": distance,
            "image_distance": image_distance,
            "max_distance": max_distance,
            "alternatives": alternatives,
            "source": source,
        })
        return plan
    distance = _choose_drag_distance(ai_distance, geometry_distance)
    return {"start_x": start_x, "start_y": start_y, "distance": distance, "max_distance": geometry_distance, "source": "AI"}


def _drag_plan_target_signature(plan: dict[str, Any]) -> tuple[Optional[float], Optional[float], Optional[float], str]:
    def rounded(value: Any) -> Optional[float]:
        try:
            if value is None:
                return None
            number = float(value)
            if not math.isfinite(number):
                return None
            return round(number, 1)
        except Exception:
            return None

    return (
        rounded(plan.get("ddddocr_target_x")),
        rounded(plan.get("white_gap_target_x")),
        rounded(plan.get("match_target_x")),
        str(plan.get("source") or ""),
    )


def _distance_alternatives(distance: float, max_distance: float) -> list[float]:
    """为同一验证码生成少量保守偏移，避免单次视觉估算边界偏差。"""
    candidates = [distance, distance - 10, distance + 10, distance - 18, distance + 18]
    result: list[float] = []
    for value in candidates:
        value = max(20.0, min(float(value), float(max_distance)))
        if all(abs(value - seen) >= 3 for seen in result):
            result.append(value)
    return result


def _find_slider_handle(root: Any):
    selectors = [
        "#aliyunCaptcha-sliding-slider",
        "#nc_1_n1z",
        ".nc_iconfont.btn_slide",
        ".btn_slide",
        ".nc_iconfont",
        "[class*='btn_slide']",
        "[class*='slide-btn']",
        "[class*='slideBtn']",
        "[class*='slide']",
        "[class*='slider_button']",
        "[class*='slider-button']",
        "[class*='sliderBtn']",
        "[class*='handle']",
        "[role='slider']",
        "button",
    ]
    root_box = root.bounding_box()
    best = None
    best_score = -10_000.0
    for selector in selectors:
        try:
            for locator in _iter_locators(root.locator(selector)):
                if not _is_visible(locator):
                    continue
                box = locator.bounding_box()
                if box is None:
                    continue
                score = _score_slider_candidate(box, root_box)
                if score > best_score:
                    best = locator
                    best_score = score
        except Exception:
            continue
    return best


def _choose_drag_distance(ai_distance: int, geometry_distance: int) -> int:
    if ai_distance <= 0:
        return geometry_distance
    # 拼图类验证码应优先信任视觉模型给出的缺口距离；只在明显超过滑轨时裁剪。
    return max(20, min(ai_distance, geometry_distance))


def _estimate_track_distance(root: Any, root_box: dict[str, float], start_x: float, slider_width: float) -> int:
    track_box = _find_track_box(root, root_box)
    if track_box is not None:
        distance = track_box["x"] + track_box["width"] - start_x - slider_width / 2
        return max(20, int(distance))
    distance = root_box["x"] + root_box["width"] - start_x - slider_width / 2 - 8
    return max(20, int(distance))


def _find_track_box(root: Any, root_box: dict[str, float]) -> Optional[dict[str, float]]:
    selectors = [
        "#aliyunCaptcha-sliding-slider",
        ".nc_scale",
        "[class*='nc_scale']",
        "[class*='scale']",
        "[class*='slider']",
        "[class*='slide']",
    ]
    candidates = []
    for selector in selectors:
        try:
            for locator in _iter_locators(root.locator(selector)):
                if not _is_visible(locator):
                    continue
                box = locator.bounding_box()
                if box is None:
                    continue
                width = float(box.get("width", 0))
                height = float(box.get("height", 0))
                rel_y = (float(box["y"]) - float(root_box["y"])) / max(float(root_box.get("height", 1)), 1.0)
                if width >= 120 and 18 <= height <= 80 and rel_y >= 0.55:
                    candidates.append((width, box))
        except Exception:
            continue
    if candidates:
        return max(candidates, key=lambda item: item[0])[1]
    return None


def _estimate_aliyun_slider_distance(page: Any, *, debug_prefix: str = "") -> Optional[dict[str, float]]:
    """基于 AliyunCaptcha 的背景图/拼图图像估算目标位置，并交给拖动阶段动态校准。"""
    if Image is None:
        return None
    try:
        data = page.evaluate(
            """
            () => {
              const img = document.getElementById('aliyunCaptcha-img');
              const puzzle = document.getElementById('aliyunCaptcha-puzzle');
              const slider = document.getElementById('aliyunCaptcha-sliding-slider');
              if (!img || !puzzle || !slider || !img.src || !puzzle.src) return null;
              const box = el => {
                const r = el.getBoundingClientRect();
                return {x: r.x, y: r.y, width: r.width, height: r.height};
              };
              return {
                imgSrc: img.src,
                puzzleSrc: puzzle.src,
                imgBox: box(img),
                puzzleBox: box(puzzle),
                sliderBox: box(slider),
              };
            }
            """
        )
    except Exception:
        return None
    if not data:
        return None
    try:
        bg = _image_from_data_url(data["imgSrc"])
        puzzle = _image_from_data_url(data["puzzleSrc"])
        _save_debug_aliyun_images(str(Path("images") / "aliyun_probe" / f"dom_{int(time.time() * 1000)}"), bg, puzzle)
        content_bbox = _alpha_bbox(puzzle)
        if content_bbox is None:
            return None

        ddddocr_x = _ddddocr_target_x(bg, puzzle, content_bbox)
        raw_white_gap_x = _detect_white_gap_target_x(bg)
        white_gap_x = None
        if raw_white_gap_x is not None:
            white_gap_x = max(0, int(raw_white_gap_x) - int(content_bbox[0]))
        match_x = _match_puzzle_target_x(bg, puzzle)
        target_x, source = _choose_aliyun_target_x(
            ddddocr_x=ddddocr_x,
            white_gap_x=white_gap_x,
            match_x=match_x,
            image_width=bg.width,
        )
        if target_x is None:
            return None

        image_width = float(bg.width)
        track_width = float(data["imgBox"]["width"] or image_width)
        scale = track_width / max(1.0, image_width)
        if source == "ddddocr":
            # ddddocr 的 slide_match 返回的是滑块应到达的目标横向位置；真实运行中再次按
            # 固定 Aliyun DOM 位移比例放大会造成误差，因此 ddddocr 路径先直接使用该距离，
            # 拖动阶段会用短距离探测得到当前验证码的真实 slider/puzzle 比例再校准。
            distance = float(target_x) * scale
            calibrate_target_x = float(target_x)
        else:
            # 本地缺口检测已经返回背景中的目标横向位置；真实运行中固定放大会把滑块推到
            # 右侧极限，后续回拉也可能被组件钳制，因此先直接使用目标坐标。
            distance = float(target_x) * scale
            calibrate_target_x = float(target_x)
        max_distance = max(20.0, track_width)
        distance = max(20.0, min(distance, max_distance))
        target_alternatives = _target_x_alternatives(
            chosen=float(target_x),
            image_width=image_width,
            ddddocr_x=ddddocr_x,
            white_gap_x=float(white_gap_x) if white_gap_x is not None else None,
            match_x=float(match_x) if match_x is not None else None,
        )
        return {
            "distance": distance,
            "max_distance": float(max_distance),
            "calibrate_target_x": calibrate_target_x,
            "target_x_alternatives": target_alternatives,
            "target_scale": float(scale),
            "source": source,
            "ddddocr_target_x": float(ddddocr_x) if ddddocr_x is not None else None,
            "white_gap_target_x": float(white_gap_x) if white_gap_x is not None else None,
            "match_target_x": float(match_x) if match_x is not None else None,
        }
    except Exception:
        return None


def _image_from_data_url(data_url: str):
    src = str(data_url or "")
    if src.startswith(("http://", "https://")):
        response = httpx.get(src, timeout=10, follow_redirects=True)
        response.raise_for_status()
        raw = response.content
    elif "," in src:
        raw = base64.b64decode(src.split(",", 1)[1])
    else:
        raise ValueError("图片不是 data URL 或 HTTP URL")

    return Image.open(BytesIO(raw)).convert("RGBA")  # type: ignore[union-attr]


def _image_to_png_bytes(image: Any) -> bytes:
    buffer = BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


def _save_debug_aliyun_images(prefix: str, bg: Any, puzzle: Any) -> None:
    try:
        path = Path(prefix)
        path.parent.mkdir(parents=True, exist_ok=True)
        bg.save(f"{prefix}_bg.png")
        puzzle.save(f"{prefix}_puzzle.png")
    except Exception:
        pass


def _get_ddddocr_slide() -> Optional[Any]:
    """懒加载 ddddocr 滑块匹配器；未安装或初始化失败时返回 None 并降级。"""
    global _DDDDOCR_LOAD_FAILED, _DDDDOCR_SLIDE
    if _DDDDOCR_LOAD_FAILED:
        return None
    if _DDDDOCR_SLIDE is not None:
        return _DDDDOCR_SLIDE
    try:
        import ddddocr  # type: ignore

        try:
            _DDDDOCR_SLIDE = ddddocr.DdddOcr(det=False, ocr=False, show_ad=False)
        except TypeError:
            _DDDDOCR_SLIDE = ddddocr.DdddOcr(det=False, ocr=False)
        return _DDDDOCR_SLIDE
    except Exception:
        _DDDDOCR_LOAD_FAILED = True
        return None


def _ddddocr_target_x(
    bg: Any,
    puzzle: Any,
    content_bbox: tuple[int, int, int, int],
) -> Optional[float]:
    slide = _get_ddddocr_slide()
    if slide is None:
        return None
    try:
        standard = slide.slide_match(
            _image_to_png_bytes(puzzle),
            _image_to_png_bytes(bg),
            simple_target=False,
        )
        standard_candidate = _extract_ddddocr_candidate(standard, content_bbox)
        if standard_candidate is None:
            return None
        standard_x, standard_confidence = standard_candidate
        if standard_confidence >= 0.2:
            return standard_x

        simple = slide.slide_match(
            _image_to_png_bytes(puzzle),
            _image_to_png_bytes(bg),
            simple_target=True,
        )
        simple_candidate = _extract_ddddocr_candidate(simple, content_bbox)
        if simple_candidate is None:
            return standard_x
        simple_x, simple_confidence = simple_candidate
        if simple_confidence > standard_confidence + 0.05:
            return simple_x
        return standard_x
    except Exception:
        return None


def _extract_ddddocr_candidate(
    result: Any,
    content_bbox: tuple[int, int, int, int],
) -> Optional[tuple[float, float]]:
    if not isinstance(result, dict):
        return None
    target = result.get("target") or result.get("target_box")
    content_width = max(1.0, float(content_bbox[2]) - float(content_bbox[0]))
    if isinstance(target, (list, tuple)) and len(target) >= 2:
        # ddddocr 当前 slide_match 实现返回匹配区域中心点；Aliyun 拖动校准使用
        # 拼图主体左边界，因此需要减去主体半宽和透明左边距。
        x = float(target[0]) - content_width / 2.0 - float(content_bbox[0])
    elif "target_x" in result:
        x = float(result["target_x"]) - content_width / 2.0 - float(content_bbox[0])
    else:
        return None
    if not math.isfinite(x):
        return None
    try:
        confidence = float(result.get("confidence", 1.0))
    except Exception:
        confidence = 1.0
    if not math.isfinite(confidence):
        confidence = 1.0
    return max(0.0, x), confidence

def _extract_ddddocr_target_x(
    result: Any,
    content_bbox: tuple[int, int, int, int],
) -> Optional[float]:
    candidate = _extract_ddddocr_candidate(result, content_bbox)
    if candidate is None:
        return None
    return candidate[0]


def _choose_local_target_x(
    white_gap_x: Optional[int],
    match_x: Optional[int],
) -> Optional[float]:
    if white_gap_x is not None and match_x is not None:
        if abs(float(white_gap_x) - float(match_x)) <= 8.0:
            return min(float(white_gap_x), float(match_x))
        return float(white_gap_x)
    if white_gap_x is not None:
        return float(white_gap_x)
    if match_x is not None:
        return float(match_x)
    return None


def _choose_aliyun_target_x(
    *,
    ddddocr_x: Optional[float],
    white_gap_x: Optional[int],
    match_x: Optional[int],
    image_width: float,
) -> tuple[Optional[float], str]:
    forced_source = os.getenv("CAPTCHA_TARGET_SOURCE", "").strip().lower()
    forced_candidates = {
        "ddddocr": (ddddocr_x, "ddddocr"),
        "ocr": (ddddocr_x, "ddddocr"),
        "white": (white_gap_x, "亮色缺口强制"),
        "white_gap": (white_gap_x, "亮色缺口强制"),
        "gap": (white_gap_x, "亮色缺口强制"),
        "match": (match_x, "模板匹配强制"),
        "template": (match_x, "模板匹配强制"),
    }
    if forced_source in forced_candidates:
        value, source = forced_candidates[forced_source]
        if value is not None:
            return float(value), source

    fallback_x = _choose_local_target_x(white_gap_x, match_x)
    if ddddocr_x is None:
        return fallback_x, "图像匹配校准"

    ddddocr_x = float(ddddocr_x)
    min_real_target_x = max(70.0, float(image_width) * 0.22)
    # Aliyun 拼图初始块在最左侧，ddddocr 偶尔会把左侧拼图自身当成目标。
    # 这类结果通常落在图片左 1/5 区域；若没有更可靠的本地候选，直接拒绝，避免错误拖动后验证码卡加载态。
    if ddddocr_x < min_real_target_x:
        if white_gap_x is not None and match_x is not None and abs(float(white_gap_x) - float(match_x)) > 35.0:
            rightmost = max(float(white_gap_x), float(match_x))
            if rightmost >= min_real_target_x:
                return rightmost, "图像匹配校准"
        if fallback_x is not None and float(fallback_x) >= min_real_target_x:
            return float(fallback_x), "图像匹配校准"
        return None, "图像匹配校准"

    # ddddocr 有时会把左侧待拖动拼图本身识别成目标；这类值通常只略高于
    # min_real_target_x，而模板匹配会指向右侧真实缺口。没有白色缺口候选时，
    # 若模板位置明显在右侧，优先使用模板，避免把滑块停在左半区。
    if (
        match_x is not None
        and white_gap_x is None
        and ddddocr_x < float(image_width) * 0.35
        and float(match_x) >= min_real_target_x
        and float(match_x) > ddddocr_x + 80.0
    ):
        return float(match_x), "图像匹配校准"

    if white_gap_x is not None and float(white_gap_x) >= min_real_target_x:
        white_delta = abs(float(white_gap_x) - ddddocr_x)
        local_candidates_right_of_ddddocr = (
            match_x is not None
            and float(match_x) >= min_real_target_x
            and float(match_x) > ddddocr_x + 35.0
            and float(white_gap_x) > ddddocr_x + 55.0
            and abs(float(match_x) - float(white_gap_x)) <= 45.0
        )
        white_clearly_right = (
            float(white_gap_x) > ddddocr_x + 35.0
            and not local_candidates_right_of_ddddocr
            and (
                match_x is None
                or float(match_x) <= ddddocr_x + 12.0
                or abs(float(match_x) - float(white_gap_x)) <= 45.0
            )
        )
        if white_clearly_right:
            return float(white_gap_x), "图像匹配校准"
        match_supports_white = (
            match_x is not None
            and float(match_x) >= min_real_target_x
            and abs(float(match_x) - float(white_gap_x)) <= 28.0
        )
        if local_candidates_right_of_ddddocr:
            return float(min(float(match_x), float(white_gap_x))), "图像匹配校准"
        white_rescues_left_self_match = (
            float(white_gap_x) > ddddocr_x + 45.0
            and ddddocr_x < float(image_width) * 0.48
            and (
                match_x is None
                or float(match_x) <= ddddocr_x + 90.0
                or float(white_gap_x) > float(match_x) + 35.0
            )
        )
        if white_rescues_left_self_match:
            return float(white_gap_x), "图像匹配校准"
        # 白色缺口检测在浅色背景/水面上容易误检到真实缺口左侧。只有非常接近
        # ddddocr，或模板匹配也支持该位置时才优先使用它。
        template_points_right = (
            match_x is not None
            and float(match_x) > ddddocr_x
            and float(white_gap_x) < ddddocr_x
            and 30.0 <= abs(float(match_x) - ddddocr_x) <= 60.0
        )
        template_and_white_left = (
            match_x is not None
            and float(match_x) < float(white_gap_x) < ddddocr_x
            and 20.0 <= white_delta <= 32.0
            and abs(float(match_x) - float(white_gap_x)) >= 32.0
            and ddddocr_x < float(image_width) * 0.72
        )
        if (
            not template_points_right
            and not template_and_white_left
            and (white_delta <= 45.0 or (white_delta <= 70.0 and match_supports_white))
        ):
            return float(white_gap_x), "图像匹配校准"

    if fallback_x is not None and _local_target_is_more_consistent(ddddocr_x, white_gap_x, match_x):
        return float(fallback_x), "图像匹配校准"

    return ddddocr_x, "ddddocr"


def _local_target_is_more_consistent(
    ddddocr_x: float,
    white_gap_x: Optional[int],
    match_x: Optional[int],
) -> bool:
    if white_gap_x is None or match_x is None:
        return False
    local_x = _choose_local_target_x(white_gap_x, match_x)
    if local_x is None:
        return False
    local_agrees = abs(float(white_gap_x) - float(match_x)) <= 8.0
    ddddocr_disagrees = abs(float(ddddocr_x) - float(local_x)) > 20.0
    return local_agrees and ddddocr_disagrees


def _target_x_alternatives(
    *,
    chosen: float,
    image_width: float,
    ddddocr_x: Optional[float],
    white_gap_x: Optional[float],
    match_x: Optional[float],
) -> list[float]:
    """生成滑块目标坐标候选，供重试时真正更换 target_x。

    优先尝试首选目标；随后尝试距离首选目标较近的模板/缺口/ocr 候选，且优先
    右侧相邻候选。这样避免 ddddocr 偏左时三次都拖到同一个错误位置。
    """
    min_real_target_x = max(70.0, float(image_width) * 0.22)
    max_real_target_x = max(min_real_target_x, float(image_width) - 20.0)

    result: list[float] = []

    def add(value: Optional[float]) -> None:
        if value is None:
            return
        try:
            number = float(value)
        except Exception:
            return
        if not math.isfinite(number):
            return
        if number < min_real_target_x or number > max_real_target_x:
            return
        if all(abs(number - seen) >= 3.0 for seen in result):
            result.append(number)

    add(float(chosen))
    raw_candidates = []
    for value in (match_x, white_gap_x, ddddocr_x):
        if value is None:
            continue
        try:
            number = float(value)
        except Exception:
            continue
        if not math.isfinite(number) or number < min_real_target_x or number > max_real_target_x:
            continue
        raw_candidates.append(number)

    right_near = sorted(
        [value for value in raw_candidates if value > float(chosen)],
        key=lambda value: (abs(value - float(chosen)), value),
    )
    left_near = sorted(
        [value for value in raw_candidates if value <= float(chosen)],
        key=lambda value: (abs(value - float(chosen)), -value),
    )
    for value in right_near + left_near:
        add(value)

    # 如果视觉候选过少，最后用小幅左右偏移补齐，覆盖“只差一点点”的场景。
    for offset in (6.0, -6.0, 10.0, -10.0):
        add(float(chosen) + offset)
    return result[:5]


def _configured_target_right_bias() -> float:
    raw = os.getenv("CAPTCHA_TARGET_RIGHT_BIAS", "").strip()
    if not raw:
        return 0.0
    try:
        value = float(raw)
    except Exception:
        return 0.0
    if not math.isfinite(value):
        return 0.0
    return max(-12.0, min(value, 12.0))


def _apply_target_right_bias(target_x: float, max_distance: float) -> float:
    bias = _configured_target_right_bias()
    if not bias:
        return float(target_x)
    return max(20.0, min(float(target_x) + bias, float(max_distance)))


def _alpha_bbox(image: Any) -> Optional[tuple[int, int, int, int]]:
    if image.mode != "RGBA":
        image = image.convert("RGBA")
    alpha = image.getchannel("A")
    return alpha.getbbox()


def _detect_white_gap_target_x(bg: Any) -> Optional[int]:
    """识别 Aliyun 背景图中靠右的白色拼图缺口左边界。"""
    bg = bg.convert("RGBA")
    width, height = bg.size
    pixels = bg.load()
    mask: set[tuple[int, int]] = set()
    for y in range(height):
        for x in range(width):
            r, g, b, a = pixels[x, y]
            if a < 80:
                continue
            mx = max(r, g, b)
            mn = min(r, g, b)
            # 真实缺口在雪山/天空背景里经常是偏蓝白色，不能仅因 b/g 高就排除；
            # 后续连通域尺寸、位置和形状过滤会排除大片天空/云层。
            if mx >= 180 and (mx - mn) <= 95:
                mask.add((x, y))

    if not mask:
        return None

    seen: set[tuple[int, int]] = set()
    candidates: list[tuple[float, int, int, int, int, int]] = []
    for point in list(mask):
        if point in seen:
            continue
        stack = [point]
        seen.add(point)
        xs: list[int] = []
        ys: list[int] = []
        while stack:
            cx, cy = stack.pop()
            xs.append(cx)
            ys.append(cy)
            for nx, ny in ((cx + 1, cy), (cx - 1, cy), (cx, cy + 1), (cx, cy - 1)):
                nxt = (nx, ny)
                if nxt in mask and nxt not in seen:
                    seen.add(nxt)
                    stack.append(nxt)
        area = len(xs)
        x1, x2 = min(xs), max(xs) + 1
        y1, y2 = min(ys), max(ys) + 1
        comp_w = x2 - x1
        comp_h = y2 - y1
        if not (28 <= comp_w <= 90 and 28 <= comp_h <= 90 and area >= 500):
            continue
        if x1 < width * 0.45:
            continue
        aspect_penalty = abs(comp_w - comp_h) * 2.0
        score = area + x1 * 4.0 - aspect_penalty
        candidates.append((score, x1, y1, x2, y2, area))

    if not candidates:
        return None
    _score, x1, _y1, _x2, _y2, _area = max(candidates, key=lambda item: item[0])
    return int(x1)


def _match_puzzle_target_x(bg: Any, puzzle: Any) -> Optional[int]:
    bg = bg.convert("RGBA")
    puzzle = puzzle.convert("RGBA")
    bbox = _alpha_bbox(puzzle)
    if bbox is None:
        return None

    px = puzzle.load()
    bx = bg.load()
    mask_points = []
    # 只使用拼图主体的非透明像素，降低透明区域和灰色描边干扰。
    for y in range(bbox[1], bbox[3]):
        for x in range(bbox[0], bbox[2]):
            r, g, b, a = px[x, y]
            if a > 80:
                mask_points.append((x, y, r, g, b))
    if not mask_points:
        return None

    max_x = bg.width - puzzle.width
    if max_x <= 0:
        return None

    best_x = None
    best_score = float("inf")
    # 模板匹配拼图片段在背景中的原始位置；背景缺口为半透明/亮色时，周围纹理仍能提供足够信号。
    for xoff in range(0, max_x + 1):
        score = 0.0
        count = 0
        for x, y, r, g, b in mask_points[::2]:
            if 0 <= x + xoff < bg.width and 0 <= y < bg.height:
                br, bgc, bb, _ba = bx[x + xoff, y]
                score += abs(br - r) + abs(bgc - g) + abs(bb - b)
                count += 1
        if count == 0:
            continue
        score /= count
        if score < best_score:
            best_score = score
            best_x = xoff
    return best_x


def _perform_drag(page: Any, plan: dict[str, float], adjustment_callback: Optional[Any] = None) -> float:
    start_x = float(plan["start_x"])
    start_y = float(plan["start_y"])
    distance = float(plan["distance"])
    max_distance = float(plan.get("max_distance", max(distance, 20.0)))
    mouse = _create_os_mouse_adapter(page, start_x, start_y, plan=plan) or page.mouse
    plan["drag_backend"] = "os" if isinstance(mouse, _WindowsOsMouseAdapter) else "playwright"
    _install_drag_event_debug(page)
    uses_os_mouse = isinstance(mouse, _WindowsOsMouseAdapter)

    calibrate_target_x = plan.get("calibrate_target_x")
    use_probe_calibration = plan.get("source") == "ddddocr" and calibrate_target_x is not None
    distance = max(20.0, min(distance, max_distance))

    if uses_os_mouse and not ensure_page_topmost_foreground(page, label=str(plan.get("label") or ""), attempts=2, delay=0.15):
        plan["focus_missed"] = True
        raise RuntimeError("OS 前台焦点确认失败，放弃拖动")

    _mouse_move(mouse, start_x, start_y, steps=3)
    time.sleep(random.uniform(0.22, 0.42))
    if uses_os_mouse and not ensure_page_topmost_foreground(page, label=str(plan.get("label") or ""), attempts=1, delay=0.1):
        plan["focus_missed"] = True
        raise RuntimeError("OS 前台焦点确认失败，放弃按下滑块")
    mouse.down()
    time.sleep(random.uniform(0.25, 0.45))
    if uses_os_mouse and not _drag_has_down_event(page):
        plan["focus_missed"] = True
        try:
            mouse.up()
        except Exception:
            pass
        debug_path = _dump_drag_event_debug(page, label=str(plan.get("label") or ""))
        if debug_path:
            plan["drag_event_debug"] = debug_path
        raise RuntimeError("OS 鼠标按下未命中滑块窗口")

    trace_path = os.getenv("CAPTCHA_REPLAY_TRACE", "").strip()
    trace = _load_manual_trace(trace_path) if trace_path else None
    if trace is not None:
        distance = max(20.0, min(distance, max_distance))
        plan["manual_trace_path"] = trace_path
        _replay_manual_trace(mouse, start_x=start_x, start_y=start_y, target_distance=distance, trace=trace)
    elif calibrate_target_x is not None:
        drag_strategy = os.getenv("CAPTCHA_DRAG_STRATEGY", "").strip().lower()
        if drag_strategy == "scaled":
            try:
                scale = float(os.getenv("CAPTCHA_TARGET_DISTANCE_SCALE", "0.791"))
            except Exception:
                scale = 0.791
            distance = max(20.0, min(float(calibrate_target_x) / max(scale, 0.1), float(max_distance)))
            plan["scaled_strategy_distance"] = distance
            _drag_aliyun_human_like(mouse, start_x, start_y, distance)
        elif drag_strategy in {"quadratic", "nonlinear"}:
            distance = _estimate_aliyun_nonlinear_slider_distance(
                target_x=float(calibrate_target_x),
                max_distance=max_distance,
            )
            plan["quadratic_strategy_distance"] = distance
            _drag_aliyun_human_like(mouse, start_x, start_y, distance)
        elif drag_strategy in {"fast_quadratic", "quick_quadratic"}:
            distance = _estimate_aliyun_nonlinear_slider_distance(
                target_x=float(calibrate_target_x),
                max_distance=max_distance,
            )
            plan["quadratic_strategy_distance"] = distance
            plan["fast_quadratic"] = True
            _drag_aliyun_fast_three_stage(mouse, start_x, start_y, distance)
            if os.getenv("CAPTCHA_FAST_QUADRATIC_FINAL_ALIGNMENT", "").strip().lower() in {"1", "true", "yes", "y"}:
                distance = _apply_final_alignment(
                    page, mouse, plan, start_x, start_y, distance, max_distance, float(calibrate_target_x)
                )
        elif drag_strategy == "ratio_human":
            probe_distance = max(60.0, min(distance, max_distance * 0.72))
            _drag_segment(mouse, start_x, start_y, 0.0, probe_distance, steps=random.randint(5, 9))
            time.sleep(random.uniform(0.08, 0.16))
            ratio = _measure_aliyun_motion_ratio(page)
            calibrated_distance = _resolve_ratio_human_distance(
                target_x=float(calibrate_target_x),
                ratio=ratio,
                current_distance=probe_distance,
                max_distance=max_distance,
            )
            if calibrated_distance > probe_distance + 1.0:
                distance = calibrated_distance
                if ratio is not None:
                    plan["ratio_human_ratio"] = float(ratio)
                else:
                    plan["ratio_human_ratio"] = 0.0
                plan["ratio_human_probe_distance"] = float(probe_distance)
                plan["ratio_human_distance"] = float(distance)
            else:
                distance = max(20.0, min(calibrated_distance, max_distance))
                if ratio:
                    plan["ratio_human_ignored_ratio"] = float(ratio)
                plan["ratio_human_probe_distance"] = float(probe_distance)
            _drag_aliyun_human_like(mouse, start_x, start_y, distance, from_offset=probe_distance)
        elif drag_strategy in {"human", "fixed", "direct"}:
            distance = max(20.0, min(distance, max_distance))
            plan["direct_human_distance"] = distance
            _drag_aliyun_human_like(mouse, start_x, start_y, distance)
        else:
            distance = _drag_aliyun_closed_loop(
                page, mouse, start_x, start_y, float(calibrate_target_x), max_distance
            )
            distance = _apply_final_alignment(
                page, mouse, plan, start_x, start_y, distance, max_distance, float(calibrate_target_x)
            )
        plan["final_distance"] = distance
    else:
        total_steps = random.randint(24, 38)
        overshoot = random.uniform(2.0, 8.0)
        target_with_overshoot = min(distance + overshoot, max_distance)
        _drag_segment(mouse, start_x, start_y, 0.0, target_with_overshoot, steps=total_steps)
        if target_with_overshoot != distance:
            _mouse_move(mouse, start_x + distance, start_y + random.uniform(-0.6, 0.6), steps=4)
            time.sleep(random.uniform(0.05, 0.16))

    hold_state = _get_aliyun_motion_state(page)
    if uses_os_mouse and hold_state and calibrate_target_x is not None:
        distance, hold_state = _apply_release_backoff_if_needed(
            page,
            mouse,
            plan,
            start_x,
            start_y,
            distance,
            max_distance,
            float(calibrate_target_x),
            hold_state,
        )
    if hold_state:
        plan["hold_state"] = hold_state
        hold_screenshot = _capture_hold_screenshot(page, label=str(plan.get("label") or ""))
        if hold_screenshot:
            plan["hold_screenshot"] = hold_screenshot
        anomaly = _drag_events_out_of_bounds(page, start_x=start_x, start_y=start_y, max_distance=max_distance)
        if uses_os_mouse and anomaly is not None:
            plan["drag_anomaly"] = anomaly
            try:
                mouse.up()
            except Exception:
                pass
            debug_path = _dump_drag_event_debug(page, label=str(plan.get("label") or ""))
            if debug_path:
                plan["drag_event_debug"] = debug_path
            raise RuntimeError(f"OS 鼠标轨迹异常: {anomaly}")
        if uses_os_mouse and _motion_state_looks_focus_missed(hold_state):
            plan["focus_missed"] = True
            try:
                mouse.up()
            except Exception:
                pass
            debug_path = _dump_drag_event_debug(page, label=str(plan.get("label") or ""))
            if debug_path:
                plan["drag_event_debug"] = debug_path
            raise RuntimeError("OS 拖动未命中滑块窗口")
    time.sleep(random.uniform(0.55, 0.95))
    mouse.up()
    debug_path = _dump_drag_event_debug(page, label=str(plan.get("label") or ""))
    if debug_path:
        plan["drag_event_debug"] = debug_path
    return distance


def _drag_debug_artifacts_enabled() -> bool:
    return os.getenv("CAPTCHA_DEBUG_DRAG_ARTIFACTS", "").strip().lower() in {"1", "true", "yes", "y", "on"}


def _capture_hold_screenshot(page: Any, *, image_dir: str = "images", label: str = "") -> Optional[str]:
    if not _drag_debug_artifacts_enabled():
        return None
    try:
        root = _find_captcha_root(page)
        if root is None:
            return None
        out_dir = Path(image_dir) / "aliyun_probe"
        out_dir.mkdir(parents=True, exist_ok=True)
        path = out_dir / f"hold_state_{int(time.time() * 1000)}.png"
        _screenshot_locator(root, str(path))
        print(f"  🖼️ {label + ' ' if label else ''}松手前截图: {path}")
        return str(path)
    except Exception:
        return None


def _install_drag_event_debug(page: Any) -> None:
    """记录浏览器实际收到的拖动事件，用于诊断 F015 交互失败。"""
    try:
        page.evaluate(
            """
            () => {
              window.__captchaDragEvents = [];
              const parseLeft = el => {
                if (!el) return null;
                const styleLeft = Number.parseFloat(el.style.left || '');
                if (Number.isFinite(styleLeft)) return styleLeft;
                const transform = getComputedStyle(el).transform;
                if (transform && transform !== 'none') {
                  const match = transform.match(/matrix\\(([^)]+)\\)/);
                  if (match) {
                    const parts = match[1].split(',').map(v => Number.parseFloat(v.trim()));
                    if (Number.isFinite(parts[4])) return parts[4];
                  }
                }
                return 0;
              };
              const record = kind => event => {
                const slider = document.getElementById('aliyunCaptcha-sliding-slider');
                const puzzle = document.getElementById('aliyunCaptcha-puzzle');
                window.__captchaDragEvents.push({
                  kind,
                  t: performance.now(),
                  x: event.clientX,
                  y: event.clientY,
                  screenX: event.screenX,
                  screenY: event.screenY,
                  buttons: event.buttons,
                  button: event.button,
                  movementX: event.movementX,
                  movementY: event.movementY,
                  isTrusted: event.isTrusted,
                  target: event.target ? (event.target.id || event.target.className || event.target.tagName) : '',
                  sliderLeft: parseLeft(slider),
                  puzzleLeft: parseLeft(puzzle),
                });
              };
              if (!window.__captchaDragEventHandlers) {
                window.__captchaDragEventHandlers = {
                  mousedown: record('mousedown'),
                  mousemove: record('mousemove'),
                  mouseup: record('mouseup'),
                  pointerdown: record('pointerdown'),
                  pointermove: record('pointermove'),
                  pointerup: record('pointerup'),
                };
                for (const [name, handler] of Object.entries(window.__captchaDragEventHandlers)) {
                  document.addEventListener(name, handler, true);
                }
              }
            }
            """
        )
    except Exception:
        pass


def _drag_event_summary(events: list[dict[str, Any]]) -> dict[str, Any]:
    if not events:
        return {"count": 0}
    first = events[0]
    last = events[-1]
    xs = [float(item.get("x", 0)) for item in events]
    ys = [float(item.get("y", 0)) for item in events]
    move_events = [item for item in events if "move" in str(item.get("kind", ""))]
    return {
        "count": len(events),
        "moveCount": len(move_events),
        "durationMs": round(float(last.get("t", 0)) - float(first.get("t", 0)), 1),
        "distanceX": round(float(last.get("x", 0)) - float(first.get("x", 0)), 1),
        "distanceY": round(float(last.get("y", 0)) - float(first.get("y", 0)), 1),
        "minX": round(min(xs), 1),
        "maxX": round(max(xs), 1),
        "minY": round(min(ys), 1),
        "maxY": round(max(ys), 1),
        "allTrusted": all(bool(item.get("isTrusted")) for item in events),
        "kinds": sorted({str(item.get("kind")) for item in events}),
    }


def _dump_drag_event_debug(page: Any, *, image_dir: str = "images", label: str = "") -> Optional[str]:
    if not _drag_debug_artifacts_enabled():
        return None
    try:
        events = page.evaluate("() => window.__captchaDragEvents || []") or []
    except Exception:
        return None
    if not isinstance(events, list) or not events:
        return None
    out_dir = Path(image_dir) / "aliyun_probe"
    out_dir.mkdir(parents=True, exist_ok=True)
    payload = {
        "label": label,
        "summary": _drag_event_summary(events),
        "events": events[-300:],
    }
    path = out_dir / f"drag_events_{int(time.time() * 1000)}.json"
    try:
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"  🧭 {label + ' ' if label else ''}拖动事件记录: {path}")
        return str(path)
    except Exception:
        return None


def _drag_has_down_event(page: Any) -> bool:
    try:
        events = page.evaluate("() => window.__captchaDragEvents || []") or []
    except Exception:
        return False
    if not isinstance(events, list):
        return False
    for event in events:
        if not isinstance(event, dict):
            continue
        if str(event.get("kind")) in {"mousedown", "pointerdown"}:
            target = str(event.get("target") or "")
            if "aliyunCaptcha-sliding-slider" in target or "slider" in target.lower() or target:
                return True
    return False


def _drag_events_out_of_bounds(
    page: Any,
    *,
    start_x: float,
    start_y: float,
    max_distance: float,
) -> Optional[str]:
    """检测拖动中是否出现明显跑飞轨迹。

    正常滑块拖动应始终围绕滑轨附近小幅上下抖动；真实日志中的失败样本出现过
    x=1300+、y=-97/974 这类跳点，说明 OS 鼠标/窗口焦点/坐标映射已经异常。
    这类尝试基本必失败，应该立即结束本次重试，而不是继续等待十几秒。
    """
    try:
        events = page.evaluate("() => window.__captchaDragEvents || []") or []
    except Exception:
        return None
    if not isinstance(events, list) or not events:
        return None

    min_x = float(start_x) - 40.0
    max_x = float(start_x) + float(max_distance) + 90.0
    min_y = float(start_y) - 180.0
    max_y = float(start_y) + 180.0
    for event in events:
        if not isinstance(event, dict):
            continue
        if "move" not in str(event.get("kind", "")):
            continue
        try:
            x = float(event.get("x", 0.0))
            y = float(event.get("y", 0.0))
        except Exception:
            continue
        if not (math.isfinite(x) and math.isfinite(y)):
            continue
        if x < min_x or x > max_x or y < min_y or y > max_y:
            return (
                f"x={x:.1f}, y={y:.1f}, "
                f"允许范围 x=[{min_x:.1f},{max_x:.1f}] y=[{min_y:.1f},{max_y:.1f}]"
            )
    return None


def _motion_state_looks_focus_missed(state: dict[str, Any]) -> bool:
    try:
        slider_left = float(state.get("sliderLeft", 0.0))
        puzzle_left = float(state.get("puzzleLeft", 0.0))
        slider_box_x = float(state.get("sliderBoxX", 0.0))
        puzzle_box_x = float(state.get("puzzleBoxX", 0.0))
    except Exception:
        return False
    return (
        abs(slider_left) <= 0.5
        and abs(puzzle_left) <= 0.5
        and abs(slider_box_x - puzzle_box_x) <= 0.5
        and slider_box_x > 0
    )


def _load_manual_trace(path: str) -> Optional[dict[str, Any]]:
    """读取人工成功轨迹，并归一化为 0..1 的水平/时间曲线。"""
    if not path:
        return None
    try:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
    except Exception:
        return None

    raw_points = payload.get("points") or []
    points: list[dict[str, float]] = []
    for item in raw_points:
        try:
            points.append({
                "t": float(item.get("t", 0)),
                "x": float(item["x"]),
                "y": float(item.get("y", 0)),
            })
        except Exception:
            continue
    if len(points) < 2:
        return None

    first = points[0]
    last = points[-1]
    duration = float(payload.get("duration") or max(1.0, last["t"] - first["t"]))
    distance = float(payload.get("distance") or (last["x"] - first["x"]))
    if abs(distance) < 10:
        return None
    normalized: list[dict[str, float]] = []
    for point in points:
        normalized.append({
            "rt": max(0.0, min(1.0, (point["t"] - first["t"]) / max(duration, 1.0))),
            "rx": max(0.0, min(1.25, (point["x"] - first["x"]) / distance)),
            "ry": point["y"] - first["y"],
        })

    return {
        "distance": distance,
        "duration": duration,
        "points": normalized,
    }


def _replay_manual_trace(
    mouse: Any,
    *,
    start_x: float,
    start_y: float,
    target_distance: float,
    trace: dict[str, Any],
) -> None:
    """按人工成功轨迹曲线缩放到当前目标距离；调用者负责按下/松开鼠标。"""
    points = trace.get("points") or []
    duration = max(120.0, float(trace.get("duration") or 900.0))
    speed = 1.0
    try:
        speed = float(os.getenv("CAPTCHA_REPLAY_SPEED", "1.0"))
    except Exception:
        speed = 1.0
    speed = max(0.2, min(speed, 3.0))
    last_t = 0.0
    for index, point in enumerate(points):
        rt = float(point.get("rt", 0.0))
        delay = max(0.0, (rt - last_t) * duration / 1000.0 / speed)
        if delay:
            time.sleep(delay)
        x = float(start_x) + float(target_distance) * float(point.get("rx", 0.0))
        y = float(start_y) if index == len(points) - 1 else float(start_y) + float(point.get("ry", 0.0))
        _mouse_move(mouse, x, y, steps=1)
        last_t = rt


def _drag_aliyun_closed_loop(
    page: Any,
    mouse: Any,
    start_x: float,
    start_y: float,
    target_x: float,
    max_distance: float,
) -> float:
    """边拖边读取拼图 DOM 位置，接近目标即停止，避免先过拖再大幅回拉。"""
    current = 0.0
    last_puzzle_left: Optional[float] = None
    for _ in range(36):
        state = _get_aliyun_motion_state(page)
        puzzle_left = None
        if state and state.get("puzzleLeft") is not None and math.isfinite(float(state["puzzleLeft"])):
            puzzle_left = float(state["puzzleLeft"])
            last_puzzle_left = puzzle_left
        if puzzle_left is not None:
            remaining = float(target_x) - puzzle_left
            if abs(remaining) <= 3.0:
                break
            if remaining < 0:
                step = max(-18.0, remaining * random.uniform(0.45, 0.75))
            elif remaining <= 18.0:
                step = max(2.0, remaining * random.uniform(0.45, 0.75))
            else:
                step = min(random.uniform(10.0, 22.0), remaining * random.uniform(0.45, 0.9))
        else:
            remaining = float(target_x) - current
            if remaining <= 3.0:
                break
            step = min(random.uniform(10.0, 20.0), remaining)

        new_distance = max(20.0, min(current + step, float(max_distance)))
        if abs(new_distance - current) < 1.0:
            break
        _drag_segment(mouse, start_x, start_y, current, new_distance, steps=random.randint(3, 6))
        current = new_distance
        time.sleep(random.uniform(0.035, 0.09))
        if current >= max_distance - 1:
            break
    if last_puzzle_left is not None:
        # 诊断字段复用 probe_state，表示闭环拖动最后一次观察到的状态。
        pass
    return current


def _drag_aliyun_human_like(
    mouse: Any,
    start_x: float,
    start_y: float,
    distance: float,
    *,
    from_offset: float = 0.0,
) -> None:
    """三段式人类拖动：快速接近、减速瞄准、末端微调。"""
    remaining = max(0.0, distance - from_offset)
    part1 = from_offset + remaining * random.uniform(0.70, 0.78)
    part2 = remaining * random.uniform(0.16, 0.23)
    current = from_offset

    _drag_segment(mouse, start_x, start_y, current, part1, steps=random.randint(8, 13))
    current = part1
    time.sleep(random.uniform(0.12, 0.22))

    _drag_segment(mouse, start_x, start_y, current, current + part2, steps=random.randint(7, 12))
    current += part2
    time.sleep(random.uniform(0.22, 0.38))

    overshoot = min(distance + random.uniform(2.0, 5.0), distance + 6.0)
    _drag_segment(mouse, start_x, start_y, current, overshoot, steps=random.randint(10, 16), y_amplitude=2.8)
    time.sleep(random.uniform(0.08, 0.16))
    _drag_segment(mouse, start_x, start_y, overshoot, distance - random.uniform(0.8, 1.8), steps=random.randint(3, 5), y_amplitude=2.2)
    _drag_segment(mouse, start_x, start_y, distance - random.uniform(0.5, 1.2), distance, steps=random.randint(3, 5), y_amplitude=1.8)
    time.sleep(random.uniform(0.12, 0.22))
    # 末端轻微抖动但回到精确距离，模拟人工瞄准，避免改变最终位置。
    for delta in (random.uniform(-1.4, 1.4), random.uniform(-0.9, 0.9), 0.0):
        _mouse_move(mouse, start_x + distance + delta, start_y + random.uniform(-1.8, 1.8), steps=2)
        time.sleep(random.uniform(0.04, 0.09))


ALIYUN_SUCCESS_PROFILE_POINTS: tuple[tuple[float, float, float], ...] = (
    (0.1609, 0.3134, 0.0),
    (0.1692, 0.5207, 2.0),
    (0.1931, 0.6498, 2.0),
    (0.2009, 0.7373, 0.0),
    (0.2172, 0.7419, 0.0),
    (0.2898, 0.8249, 0.0),
    (0.3062, 0.8710, 0.0),
    (0.3139, 0.8848, 1.0),
    (0.3215, 0.8894, 0.0),
    (0.4511, 0.9493, 1.0),
    (0.4591, 0.9816, -1.0),
    (0.4670, 1.0046, -3.0),
    (0.4768, 1.0092, -1.0),
    (0.6126, 0.9816, -1.0),
    (0.6205, 0.9724, 0.0),
    (0.6367, 0.9677, -2.0),
    (0.6770, 1.0000, 1.0),
    (0.6934, 1.0092, 1.0),
    (0.7416, 0.9770, -1.0),
    (0.7495, 0.9677, 1.0),
    (0.7576, 0.9677, -1.0),
    (0.8060, 0.9862, 0.0),
    (0.8224, 1.0046, -1.0),
    (0.8384, 1.0046, 1.0),
    (0.8870, 0.9862, 1.0),
    (0.8950, 0.9770, 0.0),
    (0.9111, 0.9724, -1.0),
    (0.9191, 0.9724, 1.0),
    (0.9755, 0.9862, -1.0),
    (0.9833, 0.9954, -1.0),
    (0.9915, 1.0000, 1.0),
    (1.0000, 1.0000, -1.0),
)


def _drag_aliyun_fast_three_stage(mouse: Any, start_x: float, start_y: float, distance: float) -> None:
    """短三段快速拖动，避免长时间按住和末端闭环校准暴露自动化特征。"""
    part1 = distance * random.uniform(0.72, 0.80)
    part2 = distance * random.uniform(0.14, 0.22)
    part3 = distance - part1 - part2
    current = 0.0
    _drag_segment(mouse, start_x, start_y, current, part1, steps=random.randint(4, 7), y_amplitude=1.4)
    current = part1
    time.sleep(random.uniform(0.12, 0.20))
    _drag_segment(mouse, start_x, start_y, current, current + part2, steps=random.randint(3, 6), y_amplitude=1.2)
    current += part2
    time.sleep(random.uniform(0.14, 0.24))
    _drag_segment(mouse, start_x, start_y, current, current + part3, steps=random.randint(2, 5), y_amplitude=0.9)
    time.sleep(random.uniform(0.18, 0.32))


def _resolve_ratio_human_distance(
    *,
    target_x: float,
    ratio: Optional[float],
    current_distance: float,
    max_distance: float,
) -> float:
    """根据 Aliyun 中后段位移比例计算滑块最终距离。

    实测该组件起步阶段存在明显非线性：小距离探测时 puzzle/slider 比例可低至
    0.23，但中后段稳定在约 0.68。因此只信任中等范围的实测比例，否则使用
    保守经验值，避免停在缺口左侧。
    """
    stable_ratio = None
    try:
        value = float(ratio) if ratio is not None else float("nan")
        if 0.55 <= value <= 0.95:
            stable_ratio = value
    except Exception:
        stable_ratio = None
    if stable_ratio is None:
        stable_ratio = 0.68
    distance = float(target_x) / stable_ratio
    distance = max(float(current_distance), distance)
    return max(20.0, min(distance, float(max_distance)))


def _estimate_aliyun_nonlinear_slider_distance(*, target_x: float, max_distance: float) -> float:
    """按 Aliyun 滑块的非线性位移曲线反算滑块应拖动距离。

    从多轮事件记录看，这个本地靶场的拼图位移接近：
    ``puzzleLeft ~= sliderLeft^2 / max_distance``。
    因此要让拼图到达背景坐标 target_x，需要拖动 sqrt(target_x * max_distance)。
    """
    max_distance = max(20.0, float(max_distance))
    # DOM 中滑块 style.left 的有效上限通常小于背景图宽度；近期事件样本中
    # 296px 背景对应约 260px 最大 left，约为 0.88。
    effective_max = max(20.0, max_distance * 0.88)
    target_x = max(0.0, min(float(target_x), effective_max))
    return max(20.0, min(math.sqrt(target_x * effective_max), effective_max))


def _drag_segment(
    mouse: Any,
    start_x: float,
    start_y: float,
    from_offset: float,
    to_offset: float,
    *,
    steps: int,
    y_amplitude: float = 2.2,
) -> None:
    steps = max(1, int(steps))
    delta = to_offset - from_offset
    phase = random.uniform(0, math.pi)
    for step in range(1, steps + 1):
        t = step / steps
        eased = 1 - math.pow(1 - t, 3)
        current_x = from_offset + delta * eased
        wave_y = math.sin(t * math.pi + phase) * float(y_amplitude)
        jitter_y = wave_y + random.uniform(-1.8, 1.8)
        _mouse_move(mouse, start_x + current_x, start_y + jitter_y, steps=2)
        time.sleep(random.uniform(0.008, 0.028))


class _WindowsOsMouseAdapter:
    """把 Playwright 视口坐标转换为 Windows 真实鼠标事件。"""

    MOVE = 0x0001
    LEFTDOWN = 0x0002
    LEFTUP = 0x0004

    def __init__(
        self,
        user32: Any,
        *,
        origin_css_x: float,
        origin_css_y: float,
        origin_screen_x: float,
        origin_screen_y: float,
        scale_x: float,
        scale_y: float,
    ):
        self.user32 = user32
        self.origin_css_x = float(origin_css_x)
        self.origin_css_y = float(origin_css_y)
        self.origin_screen_x = float(origin_screen_x)
        self.origin_screen_y = float(origin_screen_y)
        self.scale_x = float(scale_x)
        self.scale_y = float(scale_y)
        self.current_screen_x: Optional[float] = None
        self.current_screen_y: Optional[float] = None

    def _to_screen(self, x: float, y: float) -> tuple[int, int]:
        screen_x = self.origin_screen_x + (float(x) - self.origin_css_x) * self.scale_x
        screen_y = self.origin_screen_y + (float(y) - self.origin_css_y) * self.scale_y
        return int(round(screen_x)), int(round(screen_y))

    def move(self, x: float, y: float, steps: int = 1) -> None:
        target_x, target_y = self._to_screen(x, y)
        # 使用绝对坐标移动，避免 Windows 指针加速度影响相对 mouse_event 距离。
        self.user32.SetCursorPos(target_x, target_y)
        self.current_screen_x = float(target_x)
        self.current_screen_y = float(target_y)

    def down(self) -> None:
        self.user32.mouse_event(self.LEFTDOWN, 0, 0, 0, 0)

    def up(self) -> None:
        self.user32.mouse_event(self.LEFTUP, 0, 0, 0, 0)


def _create_os_mouse_adapter(
    page: Any,
    start_x: float,
    start_y: float,
    *,
    plan: Optional[dict[str, Any]] = None,
) -> Optional[_WindowsOsMouseAdapter]:
    if os.name != "nt":
        return None
    if os.getenv("CAPTCHA_DRAG_BACKEND", "playwright").lower() != "os":
        return None
    try:
        import ctypes

        user32 = ctypes.windll.user32
        metrics = _get_viewport_screen_metrics(page)
        if metrics:
            point_a = {
                "clientX": float(start_x),
                "clientY": float(start_y),
                "screenX": metrics["offsetX"] + float(start_x) * metrics["scaleX"],
                "screenY": metrics["offsetY"] + float(start_y) * metrics["scaleY"],
            }
            scale_x = metrics["scaleX"]
            scale_y = metrics["scaleY"]
        else:
            point_a = _probe_screen_point(page, start_x, start_y)
            point_b = _probe_screen_point(page, start_x + 80.0, start_y + 40.0)
            if not point_a or not point_b:
                return None
            css_dx = float(point_b["clientX"]) - float(point_a["clientX"])
            css_dy = float(point_b["clientY"]) - float(point_a["clientY"])
            screen_dx = float(point_b["screenX"]) - float(point_a["screenX"])
            screen_dy = float(point_b["screenY"]) - float(point_a["screenY"])
            scale_x = screen_dx / css_dx if abs(css_dx) >= 10 else 1.0
            scale_y = screen_dy / css_dy if abs(css_dy) >= 10 else scale_x
        if not point_a:
            return None
        if not (0.5 <= abs(scale_x) <= 3.0 and 0.5 <= abs(scale_y) <= 3.0):
            return None
        if plan is not None:
            plan["os_probe"] = {
                "clientX": float(point_a["clientX"]),
                "clientY": float(point_a["clientY"]),
                "screenX": float(point_a["screenX"]),
                "screenY": float(point_a["screenY"]),
                "scaleX": float(scale_x),
                "scaleY": float(scale_y),
            }
        return _WindowsOsMouseAdapter(
            user32,
            origin_css_x=float(point_a["clientX"]),
            origin_css_y=float(point_a["clientY"]),
            origin_screen_x=float(point_a["screenX"]),
            origin_screen_y=float(point_a["screenY"]),
            scale_x=scale_x,
            scale_y=scale_y,
        )
    except Exception:
        return None


def _get_viewport_screen_metrics(page: Any) -> Optional[dict[str, float]]:
    try:
        metrics = page.evaluate(
            """
            () => {
              const scale = window.devicePixelRatio || 1;
              const borderX = Math.max(0, (window.outerWidth - window.innerWidth) / 2);
              const topChrome = Math.max(0, window.outerHeight - window.innerHeight - borderX);
              return {
                offsetX: window.screenX + borderX,
                offsetY: window.screenY + topChrome,
                scaleX: scale,
                scaleY: scale,
                outerWidth: window.outerWidth,
                outerHeight: window.outerHeight,
                innerWidth: window.innerWidth,
                innerHeight: window.innerHeight,
              };
            }
            """
        )
        if not metrics:
            return None
        inner_width = float(metrics.get("innerWidth", 0))
        inner_height = float(metrics.get("innerHeight", 0))
        if inner_width <= 100 or inner_height <= 100:
            return None
        return {
            "offsetX": float(metrics["offsetX"]),
            "offsetY": float(metrics["offsetY"]),
            "scaleX": float(metrics.get("scaleX") or 1.0),
            "scaleY": float(metrics.get("scaleY") or 1.0),
        }
    except Exception:
        return None


def _probe_screen_point(page: Any, x: float, y: float) -> Optional[dict[str, float]]:
    try:
        page.evaluate(
            """
            () => {
              window.__captchaScreenProbe = null;
              const handler = event => {
                window.__captchaScreenProbe = {
                  clientX: event.clientX,
                  clientY: event.clientY,
                  screenX: event.screenX,
                  screenY: event.screenY,
                };
                document.removeEventListener('mousemove', handler, true);
              };
              document.addEventListener('mousemove', handler, true);
            }
            """
        )
        page.mouse.move(float(x), float(y))
        data = page.evaluate("() => window.__captchaScreenProbe")
        if not data:
            return None
        return {key: float(data[key]) for key in ("clientX", "clientY", "screenX", "screenY")}
    except Exception:
        return None


def _calculate_aliyun_local_adjustment(page: Any, target_x: float) -> float:
    """拖动中按当前拼图块 DOM 位置快速微调，避免松手前等待二次 AI 请求。"""
    state = _get_aliyun_motion_state(page)
    if not state:
        return 0.0
    puzzle_left = state.get("puzzleLeft")
    if puzzle_left is None or not math.isfinite(float(puzzle_left)):
        return 0.0
    visual_delta = float(target_x) - float(puzzle_left)
    if abs(visual_delta) < 0.8:
        return 0.0
    # 只做中小范围修正；真实 Aliyun 图上 20~80px 的末端偏差很常见，过早拒绝会导致
    # 拼图停在缺口左侧。再大的偏差往往意味着目标识别错误，避免越调越错。
    if abs(visual_delta) > 85.0:
        return 0.0
    ratio = ALIYUN_PUZZLE_MOVE_RATIO
    slider_left = state.get("sliderLeft")
    if abs(visual_delta) > 18.0 and slider_left is not None:
        try:
            measured = abs(float(puzzle_left)) / max(abs(float(slider_left)), 1.0)
            if 0.35 <= measured <= 1.4:
                ratio = measured
        except Exception:
            pass
    return max(-110.0, min(110.0, visual_delta / ratio))


def _apply_final_alignment(
    page: Any,
    mouse: Any,
    plan: dict[str, Any],
    start_x: float,
    start_y: float,
    distance: float,
    max_distance: float,
    target_x: float,
) -> float:
    total_suggested = 0.0
    total_applied = 0.0
    current = float(distance)
    steps: list[dict[str, float]] = []
    for _attempt in range(6):
        state = _get_aliyun_motion_state(page)
        if not state:
            local_adjustment = _calculate_aliyun_local_adjustment(page, target_x)
        else:
            if _motion_state_looks_focus_missed(state):
                break
            try:
                puzzle_left = float(state.get("puzzleLeft", float("nan")))
                slider_left = float(state.get("sliderLeft", float("nan")))
                visual_delta = float(target_x) - puzzle_left
            except Exception:
                visual_delta = float("nan")
                slider_left = float("nan")
                puzzle_left = float("nan")
            if not math.isfinite(visual_delta):
                local_adjustment = 0.0
            else:
                steps.append({
                    "target": float(target_x),
                    "puzzleLeft": float(puzzle_left),
                    "sliderLeft": float(slider_left) if math.isfinite(slider_left) else 0.0,
                    "delta": float(visual_delta),
                })
                # 真实样本显示“略微在目标左侧”比“压线或过右”更稳定：
                # 过右 1~4px 时经常失败，而 +1px 左右已有成功样本。
                if _alignment_delta_is_acceptable(visual_delta):
                    break
                ratio = ALIYUN_PUZZLE_MOVE_RATIO
                if math.isfinite(slider_left) and abs(slider_left) >= 5:
                    measured = abs(float(puzzle_left)) / max(abs(float(slider_left)), 1.0)
                    if 0.35 <= measured <= 1.4:
                        ratio = measured
                local_adjustment = (visual_delta - _alignment_release_bias()) / max(ratio, 0.25)
                local_adjustment = max(-45.0, min(45.0, local_adjustment))
        if not local_adjustment:
            break
        applied_adjustment = _dampen_local_adjustment(local_adjustment, fast_quadratic=False)
        new_distance = max(20.0, min(current + applied_adjustment, max_distance))
        if abs(new_distance - current) < 0.5:
            break
        _drag_segment(mouse, start_x, start_y, current, new_distance, steps=random.randint(3, 6), y_amplitude=1.1)
        current = new_distance
        total_suggested += float(local_adjustment)
        total_applied += float(applied_adjustment)
        time.sleep(random.uniform(0.04, 0.09))
    final_state = _get_aliyun_motion_state(page)
    if final_state and not _motion_state_looks_focus_missed(final_state):
        try:
            final_delta = float(target_x) - float(final_state.get("puzzleLeft", float("nan")))
            steps.append({
                "target": float(target_x),
                "puzzleLeft": float(final_state.get("puzzleLeft", 0.0)),
                "sliderLeft": float(final_state.get("sliderLeft", 0.0)),
                "delta": float(final_delta),
            })
        except Exception:
            pass
    if steps:
        plan["alignment_steps"] = steps
    if total_suggested:
        plan["local_adjustment"] = total_suggested
        plan["applied_local_adjustment"] = total_applied
    return current


def _apply_release_backoff_if_needed(
    page: Any,
    mouse: Any,
    plan: dict[str, Any],
    start_x: float,
    start_y: float,
    distance: float,
    max_distance: float,
    target_x: float,
    state: dict[str, Any],
) -> tuple[float, dict[str, Any]]:
    """释放鼠标前做一次保守回拉，避免 mouseup 时滑块状态向右结算。"""

    if _motion_state_looks_focus_missed(state):
        return distance, state
    try:
        puzzle_left = float(state.get("puzzleLeft", float("nan")))
        delta = float(target_x) - puzzle_left
    except Exception:
        return distance, state
    if not math.isfinite(delta):
        return distance, state

    min_delta = _alignment_release_min_delta()
    if delta >= min_delta:
        return distance, state

    ratio = _release_backoff_puzzle_ratio()
    backoff = math.ceil(max(0.0, min_delta - delta) / max(ratio, 0.4))
    backoff = max(1.0, min(float(backoff), _release_backoff_max_px(), max(0.0, float(distance) - 20.0)))
    if backoff <= 0:
        return distance, state
    new_distance = max(20.0, min(float(distance) - backoff, float(max_distance)))
    if abs(new_distance - float(distance)) < 0.5:
        return distance, state

    _drag_segment(mouse, start_x, start_y, float(distance), new_distance, steps=3, y_amplitude=0.8)
    time.sleep(random.uniform(0.05, 0.09))
    new_state = _get_aliyun_motion_state(page) or state
    plan["release_backoff"] = {
        "before_delta": float(delta),
        "min_delta": float(min_delta),
        "backoff": float(backoff),
        "from_distance": float(distance),
        "to_distance": float(new_distance),
    }
    try:
        new_delta = float(target_x) - float(new_state.get("puzzleLeft", float("nan")))
        if math.isfinite(new_delta):
            plan["release_backoff"]["after_delta"] = float(new_delta)
    except Exception:
        pass
    return new_distance, new_state


def _alignment_target_tolerance() -> float:
    try:
        value = float(os.getenv("CAPTCHA_ALIGNMENT_TOLERANCE", "1.5"))
    except Exception:
        value = 1.5
    return max(0.5, min(value, 3.0))


def _alignment_delta_is_acceptable(delta: float) -> bool:
    try:
        value = float(delta)
    except Exception:
        return False
    if not math.isfinite(value):
        return False
    return -_alignment_right_tolerance() <= value <= _alignment_release_bias()


def _alignment_release_bias() -> float:
    try:
        value = float(os.getenv("CAPTCHA_ALIGNMENT_RELEASE_BIAS", "2.5"))
    except Exception:
        value = 2.5
    return max(0.5, min(value, 4.0))


def _alignment_right_tolerance() -> float:
    try:
        value = float(os.getenv("CAPTCHA_ALIGNMENT_RIGHT_TOLERANCE", "2.0"))
    except Exception:
        value = 2.0
    return max(0.0, min(value, 4.0))


def _alignment_release_min_delta() -> float:
    try:
        value = float(os.getenv("CAPTCHA_ALIGNMENT_RELEASE_MIN_DELTA", "-10"))
    except Exception:
        value = -10.0
    return max(-10.0, min(value, 3.0))


def _release_backoff_puzzle_ratio() -> float:
    try:
        value = float(os.getenv("CAPTCHA_RELEASE_BACKOFF_PUZZLE_RATIO", "1.75"))
    except Exception:
        value = 1.75
    return max(0.4, min(value, 3.0))


def _release_backoff_max_px() -> float:
    try:
        value = float(os.getenv("CAPTCHA_RELEASE_BACKOFF_MAX_PX", "6"))
    except Exception:
        value = 6.0
    return max(1.0, min(value, 10.0))


def _dampen_local_adjustment(adjustment: float, *, fast_quadratic: bool = False) -> float:
    value = float(adjustment)
    if fast_quadratic:
        value *= 0.5
    if abs(value) <= 18.0:
        return value
    return value * 0.65


def _calibrate_distance_from_motion_probe(
    plan: dict[str, float],
    state: Optional[dict[str, float]],
) -> Optional[float]:
    if not state:
        return None
    target_x = plan.get("calibrate_target_x")
    max_distance = float(plan.get("max_distance", 300.0))
    if target_x is None:
        return None
    slider_left = float(state.get("sliderLeft", 0.0))
    puzzle_left = float(state.get("puzzleLeft", 0.0))
    if slider_left <= 8.0 or puzzle_left <= 3.0:
        return None
    ratio = puzzle_left / slider_left
    if not (0.35 <= ratio <= 1.4):
        return None
    calibrated = float(target_x) / ratio
    return max(20.0, min(calibrated, max_distance))


def _get_aliyun_motion_state(page: Any) -> Optional[dict[str, float]]:
    try:
        data = page.evaluate(
            """
            () => {
              const slider = document.getElementById('aliyunCaptcha-sliding-slider');
              const puzzle = document.getElementById('aliyunCaptcha-puzzle');
              const boxLeft = el => {
                if (!el) return NaN;
                const r = el.getBoundingClientRect();
                return r.x;
              };
              const parseLeft = el => {
                if (!el) return NaN;
                const styleLeft = Number.parseFloat(el.style.left || '');
                if (Number.isFinite(styleLeft)) return styleLeft;
                const transform = getComputedStyle(el).transform;
                if (transform && transform !== 'none') {
                  const match = transform.match(/matrix\\(([^)]+)\\)/);
                  if (match) {
                    const parts = match[1].split(',').map(v => Number.parseFloat(v.trim()));
                    if (Number.isFinite(parts[4])) return parts[4];
                  }
                }
                return 0;
              };
              return {
                sliderBoxX: boxLeft(slider),
                puzzleBoxX: boxLeft(puzzle),
                sliderLeft: parseLeft(slider),
                puzzleLeft: parseLeft(puzzle),
              };
            }
            """
        )
        if not data:
            return None
        return {key: float(value) for key, value in data.items() if value is not None}
    except Exception:
        return None


def _measure_aliyun_motion_ratio(page: Any, initial_state: Optional[dict[str, float]] = None) -> Optional[float]:
    """拖动过程中实测 Aliyun 拼图块位移 / 鼠标拖动位移比例。"""
    try:
        current = _get_aliyun_motion_state(page)
        if not current:
            return None
        if initial_state:
            slider_delta = current.get("sliderBoxX", 0.0) - initial_state.get("sliderBoxX", 0.0)
            puzzle_delta = current.get("puzzleBoxX", 0.0) - initial_state.get("puzzleBoxX", 0.0)
            if slider_delta <= 5 or puzzle_delta <= 1:
                slider_delta = current.get("sliderLeft", 0.0) - initial_state.get("sliderLeft", 0.0)
                puzzle_delta = current.get("puzzleLeft", 0.0) - initial_state.get("puzzleLeft", 0.0)
        else:
            slider_delta = current.get("sliderLeft", 0.0)
            puzzle_delta = current.get("puzzleLeft", 0.0)
        if slider_delta <= 5 or puzzle_delta <= 1:
            return None
        ratio = puzzle_delta / slider_delta
        if 0.2 <= ratio <= 2.5:
            return ratio
    except Exception:
        return None
    return None

def _captcha_solved(page: Any) -> bool:
    root = _find_captcha_root(page)
    if root is None:
        return True
    try:
        text = root.inner_text(timeout=1000).lower()
        pending_words = ["拖动滑块完成拼图", "请完成以下操作", "验证您是真人"]
        if any(word in text for word in pending_words):
            return False
        success_words = ["验证通过", "验证成功", "success", "passed", "verification success"]
        if any(word in text for word in success_words):
            return True
    except Exception:
        pass
    return False


def _captcha_success_wait_seconds() -> float:
    try:
        return max(1.0, float(os.getenv("CAPTCHA_SUCCESS_WAIT_SECONDS", "3.5")))
    except Exception:
        return 3.5


def _captcha_retry_reset_wait_seconds() -> float:
    try:
        return max(0.0, float(os.getenv("CAPTCHA_RETRY_RESET_WAIT_SECONDS", "0.8")))
    except Exception:
        return 0.8


def _network_has_aliyun_verify_success(records: Optional[list[dict[str, Any]]]) -> bool:
    for record in records or []:
        if not isinstance(record, dict):
            continue
        summary = record.get("summary")
        if not isinstance(summary, dict):
            summary = _summarize_verify_record(record)
        if summary.get("verifyResult") is True:
            return True
        body = str(record.get("body") or "").lower()
        if '"verifyresult":true' in body.replace(" ", ""):
            return True
    return False


def _wait_for_captcha_success(
    page: Any,
    network_records: Optional[list[dict[str, Any]]] = None,
    *,
    stop_event: Optional[Any] = None,
    timeout: float = 12.0,
) -> Optional[str]:
    """拖动松手后等待前端/服务端异步确认成功。

    Aliyun 成功响应有时会在 DOM 仍显示验证码几秒后才到达；如果只固定等待 3 秒
    再检查一次页面，会把实际已通过的滑块误判为失败。
    """
    deadline = time.time() + max(0.0, timeout)
    while True:
        if _is_cancelled(stop_event):
            return None
        if _captcha_solved(page):
            return "页面"
        if _network_has_aliyun_verify_success(network_records):
            return "服务端"
        if time.time() >= deadline:
            return None
        _sleep(0.5, stop_event)


def _try_refresh_captcha(root: Any) -> None:
    selectors = [
        "#aliyunCaptcha-btn-refresh",
        ".geetest_refresh_1",
        "[class*='refresh']",
        "[aria-label*='刷新']",
        "[title*='刷新']",
    ]
    for selector in selectors:
        try:
            locator = _first_locator(root.locator(selector))
            if _is_visible(locator):
                locator.click(timeout=1000)
                return
        except Exception:
            continue


def _first_locator(locator: Any) -> Any:
    first = getattr(locator, "first", locator)
    return first() if callable(first) else first


def _iter_locators(locator: Any, limit: int = 12):
    try:
        count = min(int(locator.count()), limit)
        for index in range(count):
            yield locator.nth(index)
        return
    except Exception:
        yield _first_locator(locator)


def _score_slider_candidate(box: dict[str, float], root_box: Optional[dict[str, float]]) -> float:
    if root_box is None:
        return 0.0
    root_width = max(float(root_box.get("width", 1)), 1.0)
    root_height = max(float(root_box.get("height", 1)), 1.0)
    rel_x = (float(box["x"]) - float(root_box["x"])) / root_width
    rel_y = (float(box["y"]) - float(root_box["y"])) / root_height
    width = float(box.get("width", 0))
    height = float(box.get("height", 0))

    score = 0.0
    # 滑块按钮通常在验证码区域底部左侧；刷新按钮通常在右上角。
    if rel_y >= 0.55:
        score += 60
    else:
        score -= 25
    if rel_x <= 0.35:
        score += 45
    elif rel_x >= 0.70:
        score -= 45
    if 18 <= width <= 90 and 18 <= height <= 70:
        score += 25
    if width < 10 or height < 10:
        score -= 50
    return score


def _mouse_move(mouse: Any, x: float, y: float, *, steps: int) -> None:
    try:
        mouse.move(x, y, steps=steps)
    except TypeError:
        mouse.move(x, y)


def _is_cancelled(stop_event: Optional[Any]) -> bool:
    try:
        return bool(stop_event is not None and stop_event.is_set())
    except Exception:
        return False


def _sleep(seconds: float, stop_event: Optional[Any]) -> None:
    end_time = time.time() + seconds
    while time.time() < end_time:
        if _is_cancelled(stop_event):
            return
        time.sleep(min(0.2, max(0.0, end_time - time.time())))
