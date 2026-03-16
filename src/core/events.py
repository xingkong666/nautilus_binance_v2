"""统一事件模型.

贯穿 strategy → execution → risk → monitoring 的事件总线.
"""

from __future__ import annotations

import time
from collections import defaultdict
from collections.abc import Callable
from dataclasses import dataclass, field
from decimal import Decimal
from enum import Enum, unique
from typing import Any

import structlog

logger = structlog.get_logger()


@unique
class EventType(Enum):
    """事件类型."""

    # 行情
    MARKET_DATA = "market_data"

    # 策略信号
    SIGNAL = "signal"

    # 执行
    ORDER_INTENT = "order_intent"
    ORDER_SUBMITTED = "order_submitted"
    ORDER_FILLED = "order_filled"
    ORDER_CANCELLED = "order_cancelled"
    ORDER_REJECTED = "order_rejected"

    # 风控
    RISK_CHECK_PASSED = "risk_check_passed"
    RISK_CHECK_FAILED = "risk_check_failed"
    RISK_ALERT = "risk_alert"
    CIRCUIT_BREAKER = "circuit_breaker"

    # 仓位
    POSITION_OPENED = "position_opened"
    POSITION_CLOSED = "position_closed"
    POSITION_CHANGED = "position_changed"

    # 系统
    RECONCILIATION = "reconciliation"
    HEALTH_CHECK = "health_check"
    STATE_SNAPSHOT = "state_snapshot"


@unique
class SignalDirection(Enum):
    """信号方向."""

    LONG = "long"
    SHORT = "short"
    FLAT = "flat"


@dataclass(frozen=True)
class Event:
    """基础事件."""

    event_type: EventType = EventType.MARKET_DATA  # 子类在 __post_init__ 中覆盖
    timestamp_ns: int = field(default_factory=lambda: time.time_ns())
    source: str = ""
    payload: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class SignalEvent(Event):
    """策略信号事件."""

    instrument_id: str = ""
    direction: SignalDirection = SignalDirection.FLAT
    strength: float = 0.0  # 信号强度 0-1
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        """Populate derived fields after initialization."""
        object.__setattr__(self, "event_type", EventType.SIGNAL)


@dataclass(frozen=True)
class OrderIntentEvent(Event):
    """订单意图事件 (策略产出 → 风控审核 → 执行)."""

    instrument_id: str = ""
    side: str = ""  # BUY / SELL
    quantity: Decimal = Decimal(0)
    order_type: str = "MARKET"
    price: Decimal | None = None
    stop_loss: Decimal | None = None
    take_profit: Decimal | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        """Populate derived fields after initialization."""
        object.__setattr__(self, "event_type", EventType.ORDER_INTENT)


@dataclass(frozen=True)
class RiskAlertEvent(Event):
    """风控告警事件."""

    level: str = "WARNING"  # WARNING / ERROR / CRITICAL
    rule_name: str = ""
    message: str = ""
    details: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        """Populate derived fields after initialization."""
        object.__setattr__(self, "event_type", EventType.RISK_ALERT)


# ---------------------------------------------------------------------------
# 事件总线
# ---------------------------------------------------------------------------

EventHandler = Callable[[Event], None]


class EventBus:
    """进程内事件总线.

    Usage:
        bus = EventBus()
        bus.subscribe(EventType.SIGNAL, my_handler)
        bus.publish(signal_event)
    """

    def __init__(self) -> None:
        """Initialize the event bus."""
        self._handlers: dict[EventType, list[EventHandler]] = defaultdict(list)
        self._global_handlers: list[EventHandler] = []

    def subscribe(self, event_type: EventType, handler: EventHandler) -> None:
        """订阅特定事件类型.

        Args:
            event_type: Event type.
            handler: Handler.
        """
        self._handlers[event_type].append(handler)

    def subscribe_all(self, handler: EventHandler) -> None:
        """订阅所有事件(适合监控/审计).

        Args:
            handler: Handler.
        """
        self._global_handlers.append(handler)

    def publish(self, event: Event) -> None:
        """发布事件.

        Args:
            event: Event instance being processed.
        """
        # 全局处理器
        for handler in self._global_handlers:
            try:
                handler(event)
            except Exception:
                logger.exception("global_event_handler_error", event_type=event.event_type.value)

        # 特定类型处理器
        for handler in self._handlers.get(event.event_type, []):
            try:
                handler(event)
            except Exception:
                logger.exception("event_handler_error", event_type=event.event_type.value)

    def clear(self) -> None:
        """清除所有订阅."""
        self._handlers.clear()
        self._global_handlers.clear()
