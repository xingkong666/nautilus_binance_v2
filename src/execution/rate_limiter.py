"""速率限制.

防止 API 请求过于频繁, 避免被交易所限流.
"""

from __future__ import annotations

import time
from collections import deque
from typing import TYPE_CHECKING, Any

import structlog

if TYPE_CHECKING:
    from src.cache.redis_client import RedisClient

logger = structlog.get_logger(__name__)

# Redis keys
_RL_SECOND_KEY = "nautilus:rl:orders:second"
_RL_MINUTE_KEY = "nautilus:rl:orders:minute"

# Lua 原子脚本：滑动窗口限流（原子检查 + 计入）
_RATE_LIMIT_LUA = """
local now = tonumber(ARGV[1])
local window = tonumber(ARGV[2])
local limit = tonumber(ARGV[3])
redis.call('ZREMRANGEBYSCORE', KEYS[1], '-inf', now - window)
local count = redis.call('ZCARD', KEYS[1])
if count < limit then
    redis.call('ZADD', KEYS[1], now, tostring(now) .. '-' .. tostring(redis.call('INCR', 'nautilus:rl:seq')))
    redis.call('EXPIRE', KEYS[1], math.ceil(window) + 1)
    return 1
end
return 0
"""


class RateLimiter:
    """令牌桶速率限制器.

    当 redis_client 可用时使用 Redis ZSET 滑动窗口（支持多进程共享）；
    Redis 不可用时自动 fallback 到本地 deque（保持单进程限流）。
    """

    def __init__(
        self,
        config: dict[str, Any],
        redis_client: RedisClient | None = None,
    ) -> None:
        """Initialize the rate limiter.

        Args:
            config: Configuration values for the component.
            redis_client: Redis client.
        """
        self._max_per_second = config.get("max_orders_per_second", 5)
        self._max_per_minute = config.get("max_orders_per_minute", 100)
        self._burst_size = config.get("burst_size", 10)
        self._redis = redis_client

        # 本地 fallback 窗口
        self._second_window: deque[float] = deque()
        self._minute_window: deque[float] = deque()

    def can_proceed(self) -> bool:
        """检查是否允许发送请求."""
        if self._redis is not None and self._redis.is_available:
            return self._can_proceed_redis()
        return self._can_proceed_local()

    def record(self) -> None:
        """记录一次请求（仅在本地 fallback 模式下需要显式调用）.

        Redis 模式下，can_proceed() 内部的 Lua 脚本已原子记录。
        本地模式下仍需此方法。
        """
        if self._redis is not None and self._redis.is_available:
            # Redis 模式由 can_proceed 中 Lua 脚本原子处理，无需二次记录
            return
        now = time.time()
        self._second_window.append(now)
        self._minute_window.append(now)

    def _can_proceed_redis(self) -> bool:
        """使用 Redis ZSET 滑动窗口检查限流（原子操作）."""
        assert self._redis is not None
        now = time.time()

        # 秒级窗口
        result_sec = self._redis.execute_script(
            _RATE_LIMIT_LUA,
            keys=[_RL_SECOND_KEY],
            args=[str(now), "1.0", str(self._max_per_second)],
        )
        if result_sec == 0:
            logger.warning("rate_limit_per_second_redis")
            return False

        # 分钟级窗口
        result_min = self._redis.execute_script(
            _RATE_LIMIT_LUA,
            keys=[_RL_MINUTE_KEY],
            args=[str(now), "60.0", str(self._max_per_minute)],
        )
        if result_min == 0:
            # 回滚秒级计数（在 ZSET 中删除刚写入的成员不可精确回滚，
            # 此处直接拒绝并记录，不影响功能正确性）
            logger.warning("rate_limit_per_minute_redis")
            return False

        return True

    def _can_proceed_local(self) -> bool:
        """使用本地 deque 检查限流（单进程 fallback）."""
        now = time.time()
        self._cleanup(now)

        if len(self._second_window) >= self._max_per_second:
            logger.warning("rate_limit_per_second", current=len(self._second_window))
            return False

        if len(self._minute_window) >= self._max_per_minute:
            logger.warning("rate_limit_per_minute", current=len(self._minute_window))
            return False

        return True

    def _cleanup(self, now: float) -> None:
        """清理过期记录.

        Args:
            now: Now.
        """
        while self._second_window and now - self._second_window[0] > 1.0:
            self._second_window.popleft()
        while self._minute_window and now - self._minute_window[0] > 60.0:
            self._minute_window.popleft()

    def wait_if_needed(self) -> float:
        """如果需要限流, 返回需要等待的秒数."""
        if self.can_proceed():
            return 0.0

        if self._second_window:
            wait = 1.0 - (time.time() - self._second_window[0])
            return max(0.0, wait)
        return 0.0
