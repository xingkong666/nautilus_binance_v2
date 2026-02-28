"""熔断机制.

当触发熔断条件时, 暂停或限制交易活动.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from decimal import Decimal
from typing import TYPE_CHECKING, Any

import structlog

from src.core.constants import CB_HALT_ALL
from src.core.events import EventBus, RiskAlertEvent

if TYPE_CHECKING:
    from src.cache.redis_client import RedisClient

logger = structlog.get_logger()

# Redis key for circuit breaker state
_CB_STATE_KEY = "nautilus:cb:state"


@dataclass
class CircuitBreakerState:
    """熔断器状态."""

    is_triggered: bool = False
    action: str = ""  # halt_all / reduce_only / alert_only
    reason: str = ""
    triggered_at_ns: int = 0
    cooldown_until_ns: int = 0


@dataclass
class CircuitBreakerTrigger:
    """熔断触发条件."""

    trigger_type: str
    threshold: float
    action: str = CB_HALT_ALL
    cooldown_minutes: int = 60


class CircuitBreaker:
    """熔断器.

    支持多种触发条件:
    - daily_loss: 单日亏损
    - drawdown: 回撤
    - rapid_loss: 短时间连续亏损
    """

    def __init__(
        self,
        event_bus: EventBus,
        config: dict[str, Any],
        redis_client: RedisClient | None = None,
    ) -> None:
        self._event_bus = event_bus
        self._redis = redis_client
        self._state = CircuitBreakerState()
        self._triggers = self._parse_triggers(config)
        self._recent_losses: list[tuple[int, Decimal]] = []  # (timestamp_ns, loss_amount)

        # 启动时从 Redis 恢复状态（跨进程重启后延续冷却）
        self._restore_from_redis()

    @staticmethod
    def _parse_triggers(config: dict[str, Any]) -> list[CircuitBreakerTrigger]:
        """解析触发条件配置."""
        triggers = []
        for t in config.get("triggers", []):
            trigger = CircuitBreakerTrigger(
                trigger_type=t.get("type", ""),
                threshold=float(t.get("threshold_usd", t.get("threshold_pct", t.get("max_losses", 0)))),
                action=t.get("action", CB_HALT_ALL),
                cooldown_minutes=t.get("cooldown_minutes", 60),
            )
            triggers.append(trigger)
        return triggers

    @property
    def is_active(self) -> bool:
        """熔断是否激活中.

        优先从 Redis 读取跨进程共享状态；Redis 不可用时 fallback 到内存状态。
        """
        # 尝试从 Redis 读取状态（跨进程共享）
        if self._redis is not None and self._redis.is_available:
            try:
                data = self._redis.hgetall(_CB_STATE_KEY)
                if data and data.get("is_triggered") == "1":
                    cooldown_until_ns = int(data.get("cooldown_until_ns", "0"))
                    now_ns = time.time_ns()
                    if now_ns < cooldown_until_ns:
                        # 同步到内存（避免 Redis 每次都查）
                        if not self._state.is_triggered:
                            self._state = CircuitBreakerState(
                                is_triggered=True,
                                action=data.get("action", ""),
                                reason=data.get("reason", ""),
                                triggered_at_ns=int(data.get("triggered_at_ns", "0")),
                                cooldown_until_ns=cooldown_until_ns,
                            )
                        return True
                    else:
                        # 冷却结束，清理 Redis 和内存
                        self._redis.delete(_CB_STATE_KEY)
                        if self._state.is_triggered:
                            self._reset()
                        return False
            except Exception as exc:
                logger.warning("circuit_breaker_redis_read_failed", error=str(exc))
                # fallback 到内存状态

        # fallback: 内存状态
        if not self._state.is_triggered:
            return False

        if time.time_ns() >= self._state.cooldown_until_ns:
            self._reset()
            return False

        return True

    @property
    def state(self) -> CircuitBreakerState:
        return self._state

    def check_daily_loss(self, daily_pnl: Decimal) -> bool:
        """检查单日亏损熔断."""
        for trigger in self._triggers:
            if trigger.trigger_type == "daily_loss" and daily_pnl < -Decimal(str(trigger.threshold)):
                self._trip(trigger, f"单日亏损: {daily_pnl:.0f} USDT")
                return True
        return False

    def check_drawdown(self, drawdown_pct: float) -> bool:
        """检查回撤熔断."""
        for trigger in self._triggers:
            if trigger.trigger_type == "drawdown" and drawdown_pct >= trigger.threshold:
                self._trip(trigger, f"回撤: {drawdown_pct:.1f}%")
                return True
        return False

    def check_rapid_loss(self, loss_amount: Decimal) -> bool:
        """检查短时间连续亏损."""
        now_ns = time.time_ns()
        self._recent_losses.append((now_ns, loss_amount))

        for trigger in self._triggers:
            if trigger.trigger_type != "rapid_loss":
                continue

            window_ns = trigger.cooldown_minutes * 60 * 1_000_000_000
            cutoff = now_ns - window_ns
            recent = [loss for loss in self._recent_losses if loss[0] >= cutoff]
            self._recent_losses = recent

            if len(recent) >= int(trigger.threshold):
                self._trip(trigger, f"短时间连续亏损: {len(recent)} 笔")
                return True

        return False

    def _trip(self, trigger: CircuitBreakerTrigger, reason: str) -> None:
        """触发熔断."""
        now_ns = time.time_ns()
        cooldown_seconds = trigger.cooldown_minutes * 60
        cooldown_until_ns = now_ns + cooldown_seconds * 1_000_000_000
        self._state = CircuitBreakerState(
            is_triggered=True,
            action=trigger.action,
            reason=reason,
            triggered_at_ns=now_ns,
            cooldown_until_ns=cooldown_until_ns,
        )

        # 持久化到 Redis（跨进程共享）
        if self._redis is not None and self._redis.is_available:
            try:
                self._redis.hset(
                    _CB_STATE_KEY,
                    {
                        "is_triggered": "1",
                        "action": trigger.action,
                        "reason": reason,
                        "triggered_at_ns": str(now_ns),
                        "cooldown_until_ns": str(cooldown_until_ns),
                    },
                )
                self._redis.expire(_CB_STATE_KEY, cooldown_seconds + 60)
                logger.info("circuit_breaker_state_persisted_to_redis", cooldown_seconds=cooldown_seconds)
            except Exception as exc:
                logger.warning("circuit_breaker_redis_write_failed", error=str(exc))

        logger.critical("circuit_breaker_triggered", reason=reason, action=trigger.action)
        self._event_bus.publish(
            RiskAlertEvent(
                level="CRITICAL",
                rule_name="circuit_breaker",
                message=f"🚨 熔断触发: {reason}",
                details={"action": trigger.action, "cooldown_minutes": trigger.cooldown_minutes},
            )
        )

    def _reset(self) -> None:
        """重置熔断器."""
        logger.info("circuit_breaker_reset", previous_reason=self._state.reason)
        self._state = CircuitBreakerState()
        # 同步删除 Redis 状态
        if self._redis is not None and self._redis.is_available:
            try:
                self._redis.delete(_CB_STATE_KEY)
            except Exception as exc:
                logger.warning("circuit_breaker_redis_delete_failed", error=str(exc))

    def force_reset(self) -> None:
        """强制重置 (人工干预)."""
        logger.warning("circuit_breaker_force_reset")
        self._reset()

    def _restore_from_redis(self) -> None:
        """启动时从 Redis 恢复熔断状态（支持进程重启后继续冷却）."""
        if self._redis is None or not self._redis.is_available:
            return
        try:
            data = self._redis.hgetall(_CB_STATE_KEY)
            if not data or data.get("is_triggered") != "1":
                return

            cooldown_until_ns = int(data.get("cooldown_until_ns", "0"))
            now_ns = time.time_ns()
            if now_ns < cooldown_until_ns:
                self._state = CircuitBreakerState(
                    is_triggered=True,
                    action=data.get("action", ""),
                    reason=data.get("reason", ""),
                    triggered_at_ns=int(data.get("triggered_at_ns", "0")),
                    cooldown_until_ns=cooldown_until_ns,
                )
                remaining_sec = (cooldown_until_ns - now_ns) / 1_000_000_000
                logger.warning(
                    "circuit_breaker_restored_from_redis",
                    reason=self._state.reason,
                    remaining_seconds=round(remaining_sec, 1),
                )
        except Exception as exc:
            logger.warning("circuit_breaker_restore_failed", error=str(exc))
