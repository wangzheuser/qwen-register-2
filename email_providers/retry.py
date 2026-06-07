"""邮箱服务限流重试工具。"""

from __future__ import annotations

import random
import time

RATE_LIMIT_MAX_RETRIES = 9
RATE_LIMIT_MIN_DELAY_SECONDS = 0.01
RATE_LIMIT_MAX_DELAY_SECONDS = 0.1


def random_rate_limit_delay() -> float:
    """返回 429 限流后的随机短等待秒数。"""

    return random.uniform(RATE_LIMIT_MIN_DELAY_SECONDS, RATE_LIMIT_MAX_DELAY_SECONDS)


def sleep_rate_limit_retry() -> float:
    """等待一次 429 限流重试间隔，并返回实际等待秒数。"""

    delay = random_rate_limit_delay()
    time.sleep(delay)
    return delay


def delay_to_milliseconds(delay: float) -> int:
    """把秒数转换为便于日志展示的毫秒整数。"""

    return int(round(delay * 1000))
