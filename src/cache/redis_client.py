"""Redis 客户端封装.

带连接池和降级的 Redis 客户端封装。
Redis 不可用时所有操作静默降级（返回 None/False），
保证主交易链路不因 Redis 故障中断。

Key 命名规范（前缀 ``nautilus:``）:
  - ``nautilus:cb:state``                          — 熔断器状态 (Hash)
  - ``nautilus:account:balance:{asset}``           — 账户余额 (Hash, TTL 30s)
  - ``nautilus:account:position:{symbol}:{side}``  — 持仓快照 (Hash, TTL 30s)
  - ``nautilus:rl:orders:second``                  — 限流滑动窗口秒级 (ZSET, TTL 2s)
  - ``nautilus:rl:orders:minute``                  — 限流滑动窗口分钟级 (ZSET, TTL 120s)
  - ``nautilus:risk:metrics``                      — 实时风控指标 (Hash, 不设TTL)
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import structlog
from redis import Redis as _Redis
from redis.connection import ConnectionPool
from redis.exceptions import RedisError

if TYPE_CHECKING:
    from src.core.config import RedisConfig

logger = structlog.get_logger()


class RedisClient:
    """带连接池和降级的 Redis 客户端封装.

    Redis 不可用时所有操作静默降级（返回 None/False），
    保证主交易链路不因 Redis 故障中断。
    """

    def __init__(self, config: RedisConfig) -> None:
        """Initialize the redis client.

        Args:
            config: Configuration values for the component.
        """
        self._config = config
        self._pool: ConnectionPool | None = None
        self._redis: _Redis | None = None
        self._available = False
        self._connect()

    def _connect(self) -> None:
        """建立 Redis 连接池，失败时设置降级模式."""
        try:
            self._pool = ConnectionPool.from_url(
                self._config.url,
                socket_timeout=self._config.socket_timeout,
                socket_connect_timeout=self._config.socket_connect_timeout,
                decode_responses=True,
                max_connections=20,
            )
            self._redis = _Redis(connection_pool=self._pool)
            # 测试连通性
            self._redis.ping()
            self._available = True
            logger.info(
                "redis_connected",
                host=self._config.host,
                port=self._config.port,
                db=self._config.db,
            )
        except (ConnectionError, OSError, RedisError) as exc:
            self._available = False
            logger.warning(
                "redis_connect_failed",
                host=self._config.host,
                port=self._config.port,
                error=str(exc),
                hint="Redis 降级为不可用，所有操作将静默跳过",
            )

    @property
    def is_available(self) -> bool:
        """Redis 是否可用."""
        return self._available and self._redis is not None

    def get(self, key: str) -> str | None:
        """获取字符串值，失败返回 None.

        Args:
            key: Cache key to read or write.
        """
        if not self.is_available:
            return None
        try:
            assert self._redis is not None
            return self._redis.get(key)  # type: ignore[return-value]
        except RedisError as exc:
            logger.warning("redis_get_failed", key=key, error=str(exc))
            return None

    def set(self, key: str, value: str, ex: int | None = None) -> bool:
        """设置字符串值，失败返回 False.

        Args:
            key: Cache key to read or write.
            value: Input value to convert or evaluate.
            ex: Optional expiration time in seconds.
        """
        if not self.is_available:
            return False
        try:
            assert self._redis is not None
            result = self._redis.set(key, value, ex=ex)
            return bool(result)
        except RedisError as exc:
            logger.warning("redis_set_failed", key=key, error=str(exc))
            return False

    def delete(self, *keys: str) -> int:
        """删除一个或多个 key，失败返回 0.

        Args:
            *keys: Keys passed to the backend operation.
        """
        if not self.is_available:
            return 0
        try:
            assert self._redis is not None
            return self._redis.delete(*keys)  # type: ignore[return-value]
        except RedisError as exc:
            logger.warning("redis_delete_failed", keys=keys, error=str(exc))
            return 0

    def hset(self, name: str, mapping: dict[str, str]) -> int:
        """设置 Hash 字段，失败返回 0.

        Args:
            name: Redis key or resource name.
            mapping: Mapping payload written to the backend.
        """
        if not self.is_available:
            return 0
        try:
            assert self._redis is not None
            return self._redis.hset(name, mapping=mapping)  # type: ignore[return-value]
        except RedisError as exc:
            logger.warning("redis_hset_failed", name=name, error=str(exc))
            return 0

    def hgetall(self, name: str) -> dict[str, str]:
        """获取 Hash 全部字段，失败返回空字典.

        Args:
            name: Redis key or resource name.
        """
        if not self.is_available:
            return {}
        try:
            assert self._redis is not None
            return self._redis.hgetall(name)  # type: ignore[return-value]
        except RedisError as exc:
            logger.warning("redis_hgetall_failed", name=name, error=str(exc))
            return {}

    def zadd(self, name: str, mapping: dict[str, float]) -> int:
        """向 ZSET 添加成员，失败返回 0.

        Args:
            name: Redis key or resource name.
            mapping: Mapping payload written to the backend.
        """
        if not self.is_available:
            return 0
        try:
            assert self._redis is not None
            return self._redis.zadd(name, mapping)  # type: ignore[return-value]
        except RedisError as exc:
            logger.warning("redis_zadd_failed", name=name, error=str(exc))
            return 0

    def zrangebyscore(self, name: str, min_score: float, max_score: float) -> list[str]:
        """按分值范围查询 ZSET，失败返回空列表.

        Args:
            name: Redis key or resource name.
            min_score: Minimum score threshold.
            max_score: Max score.
        """
        if not self.is_available:
            return []
        try:
            assert self._redis is not None
            return self._redis.zrangebyscore(name, min_score, max_score)  # type: ignore[return-value]
        except RedisError as exc:
            logger.warning("redis_zrangebyscore_failed", name=name, error=str(exc))
            return []

    def zremrangebyscore(self, name: str, min_score: float, max_score: float) -> int:
        """按分值范围删除 ZSET 成员，失败返回 0.

        Args:
            name: Redis key or resource name.
            min_score: Minimum score threshold.
            max_score: Max score.
        """
        if not self.is_available:
            return 0
        try:
            assert self._redis is not None
            return self._redis.zremrangebyscore(name, min_score, max_score)  # type: ignore[return-value]
        except RedisError as exc:
            logger.warning("redis_zremrangebyscore_failed", name=name, error=str(exc))
            return 0

    def zcard(self, name: str) -> int:
        """返回 ZSET 成员数量，失败返回 0.

        Args:
            name: Redis key or resource name.
        """
        if not self.is_available:
            return 0
        try:
            assert self._redis is not None
            return self._redis.zcard(name)  # type: ignore[return-value]
        except RedisError as exc:
            logger.warning("redis_zcard_failed", name=name, error=str(exc))
            return 0

    def execute_script(self, script: str, keys: list[str], args: list[str]) -> object:
        """执行 Lua 脚本，失败返回 None.

        Args:
            script: Script content to execute on the backend.
            keys: Keys passed to the backend operation.
            args: Parsed command-line arguments or runtime options.
        """
        if not self.is_available:
            return None
        try:
            assert self._redis is not None
            lua = self._redis.register_script(script)
            return lua(keys=keys, args=args)
        except RedisError as exc:
            logger.warning("redis_script_failed", error=str(exc))
            return None

    def expire(self, name: str, seconds: int) -> bool:
        """设置 key 过期时间，失败返回 False.

        Args:
            name: Redis key or resource name.
            seconds: Seconds.
        """
        if not self.is_available:
            return False
        try:
            assert self._redis is not None
            return bool(self._redis.expire(name, seconds))
        except RedisError as exc:
            logger.warning("redis_expire_failed", name=name, error=str(exc))
            return False

    def close(self) -> None:
        """关闭连接池."""
        if self._pool is not None:
            try:
                self._pool.disconnect()
                logger.info("redis_connection_pool_closed")
            except (ConnectionError, OSError, RedisError) as exc:
                logger.warning("redis_close_failed", error=str(exc))
        self._available = False
        self._redis = None
        self._pool = None
