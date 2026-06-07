"""滑块验证码求解器包。"""

from .ai_slider import (
    DEFAULT_CAPTCHA_AI_BASE_URL,
    DEFAULT_CAPTCHA_AI_MODEL,
    CaptchaAiClient,
    CaptchaSolverConfig,
    CaptchaSolverResult,
    solve_slider_captcha,
)

__all__ = [
    "DEFAULT_CAPTCHA_AI_BASE_URL",
    "DEFAULT_CAPTCHA_AI_MODEL",
    "CaptchaAiClient",
    "CaptchaSolverConfig",
    "CaptchaSolverResult",
    "solve_slider_captcha",
]
