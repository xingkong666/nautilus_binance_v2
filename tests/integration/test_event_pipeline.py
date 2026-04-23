"""集成测试：事件总线端到端流水线.

验证 策略信号 → 风控检查 → 成交处理 → 持久化 的完整事件链路。
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from src.core.events import (
    EventBus,
    EventType,
    OrderIntentEvent,
    SignalDirection,
    SignalEvent,
)
from src.execution.fill_handler import FillHandler
from src.risk.pre_trade import PreTradeRiskManager
from src.state.persistence import TradePersistence

PG_URL = "postgresql://admin:Longmao!666@127.0.0.1:5432/nautilus_trader"


# ---------------------------------------------------------------------------
# 测试夹具
# ---------------------------------------------------------------------------


@pytest.fixture
def tmp_db():
    """使用真实 PostgreSQL，每次测试后清空表。."""
    persistence = TradePersistence(database_url=PG_URL)
    yield persistence
    # 清理：截断表
    with persistence._conn.cursor() as cur:
        cur.execute("TRUNCATE trades, events RESTART IDENTITY CASCADE")
    persistence._conn.commit()
    persistence.close()


@pytest.fixture
def event_bus():
    """Run event bus."""
    bus = EventBus()
    yield bus
    bus.clear()


@pytest.fixture
def pre_trade_risk(event_bus):
    """Run pre trade risk.

    Args:
        event_bus: Event bus used for cross-module communication.
    """
    config = {
        "max_order_size_usd": 10_000,
        "max_position_size_usd": 50_000,
        "max_leverage": 10,
        "min_order_interval_ms": 0,
        "max_open_orders": 10,
    }
    return PreTradeRiskManager(event_bus=event_bus, config=config)


@pytest.fixture
def fill_handler(event_bus, tmp_db):
    """Run fill handler.

    Args:
        event_bus: Event bus used for cross-module communication.
        tmp_db: Tmp db.
    """
    return FillHandler(event_bus=event_bus, persistence=tmp_db)


# ---------------------------------------------------------------------------
# 事件总线基础测试
# ---------------------------------------------------------------------------


class TestEventBus:
    """Test cases for event bus."""

    def test_subscribe_and_receive(self, event_bus):
        """订阅后能收到对应类型的事件。.

        Args:
            event_bus: Event bus fixture or instance used in the test.
        """
        received = []
        event_bus.subscribe(EventType.SIGNAL, received.append)

        sig = SignalEvent(
            instrument_id="BTCUSDT-PERP.BINANCE",
            direction=SignalDirection.LONG,
            strength=0.8,
        )
        event_bus.publish(sig)

        assert len(received) == 1
        assert received[0].instrument_id == "BTCUSDT-PERP.BINANCE"

    def test_subscribe_all_receives_every_event(self, event_bus):
        """subscribe_all 处理器收到所有类型的事件。.

        Args:
            event_bus: Event bus fixture or instance used in the test.
        """
        all_events = []
        event_bus.subscribe_all(all_events.append)

        event_bus.publish(SignalEvent(instrument_id="BTCUSDT-PERP.BINANCE", direction=SignalDirection.LONG))
        event_bus.publish(OrderIntentEvent(instrument_id="BTCUSDT-PERP.BINANCE", side="BUY", quantity=Decimal("0.01")))

        assert len(all_events) == 2

    def test_wrong_event_type_not_received(self, event_bus):
        """订阅 SIGNAL 不会收到 ORDER_INTENT 类型的事件。.

        Args:
            event_bus: Event bus fixture or instance used in the test.
        """
        signal_events = []
        event_bus.subscribe(EventType.SIGNAL, signal_events.append)

        event_bus.publish(OrderIntentEvent(instrument_id="BTCUSDT-PERP.BINANCE", side="BUY", quantity=Decimal("0.01")))

        assert len(signal_events) == 0

    def test_handler_exception_does_not_crash_bus(self, event_bus):
        """某个 handler 抛异常不影响其他 handler 和后续发布。.

        Args:
            event_bus: Event bus fixture or instance used in the test.
        """
        received = []

        def bad_handler(e):
            raise RuntimeError("故意报错")

        event_bus.subscribe(EventType.SIGNAL, bad_handler)
        event_bus.subscribe(EventType.SIGNAL, received.append)

        event_bus.publish(SignalEvent(instrument_id="BTCUSDT-PERP.BINANCE", direction=SignalDirection.LONG))

        # 坏 处理程序 不应阻止正常 处理程序 运行
        assert len(received) == 1

    def test_clear_removes_all_handlers(self, event_bus):
        """clear() 后发布事件不再有任何响应。.

        Args:
            event_bus: Event bus fixture or instance used in the test.
        """
        received = []
        event_bus.subscribe(EventType.SIGNAL, received.append)
        event_bus.subscribe_all(received.append)
        event_bus.clear()

        event_bus.publish(SignalEvent(instrument_id="BTCUSDT-PERP.BINANCE", direction=SignalDirection.LONG))

        assert len(received) == 0


# ---------------------------------------------------------------------------
# 风控 → 事件总线集成
# ---------------------------------------------------------------------------


class TestPreTradeWithEventBus:
    """Test cases for pre trade with event bus."""

    def test_passing_check_does_not_emit_alert(self, pre_trade_risk, event_bus):
        """合规订单通过检查，不触发 RISK_ALERT 事件。.

        Args:
            pre_trade_risk: Pre-trade risk checker fixture under test.
            event_bus: Event bus fixture or instance used in the test.
        """
        alerts = []
        event_bus.subscribe(EventType.RISK_ALERT, alerts.append)

        intent = OrderIntentEvent(
            instrument_id="BTCUSDT-PERP.BINANCE",
            side="BUY",
            quantity=Decimal("0.01"),
        )
        result = pre_trade_risk.check(
            intent=intent,
            current_position_usd=Decimal(0),
            current_open_orders=0,
            current_price=Decimal("50000"),
        )

        assert result.passed is True
        assert len(alerts) == 0

    def test_exceeding_order_size_emits_risk_alert(self, pre_trade_risk, event_bus):
        """超过单笔订单限额，check 失败且发布 RISK_ALERT 事件。.

        Args:
            pre_trade_risk: Pre-trade risk checker fixture under test.
            event_bus: Event bus fixture or instance used in the test.
        """
        alerts = []
        event_bus.subscribe(EventType.RISK_ALERT, alerts.append)

        # 10000USDmax_order_size_usd; 1BTC@50000 = 50000USD
        intent = OrderIntentEvent(
            instrument_id="BTCUSDT-PERP.BINANCE",
            side="BUY",
            quantity=Decimal("1"),
        )
        result = pre_trade_risk.check(
            intent=intent,
            current_position_usd=Decimal(0),
            current_open_orders=0,
            current_price=Decimal("50000"),
        )

        assert result.passed is False
        assert len(alerts) == 1
        assert alerts[0].event_type == EventType.RISK_ALERT

    def test_exceeding_position_size_fails(self, pre_trade_risk, event_bus):
        """当前持仓+新订单超过总仓位上限时 check 失败。.

        Args:
            pre_trade_risk: Pre-trade risk checker fixture under test.
            event_bus: Event bus fixture or instance used in the test.
        """
        intent = OrderIntentEvent(
            instrument_id="BTCUSDT-PERP.BINANCE",
            side="BUY",
            quantity=Decimal("0.1"),  # 0.1BTC@50000 = 5000USD
        )
        result = pre_trade_risk.check(
            intent=intent,
            current_position_usd=Decimal(48_000),  # 已接近上限
            current_open_orders=0,
            current_price=Decimal("50000"),
        )

        assert result.passed is False
        assert "position" in result.reason.lower() or "仓位" in result.reason

    def test_too_many_open_orders_fails(self, pre_trade_risk, event_bus):
        """挂单数超过上限时 check 失败。.

        Args:
            pre_trade_risk: Pre-trade risk checker fixture under test.
            event_bus: Event bus fixture or instance used in the test.
        """
        intent = OrderIntentEvent(
            instrument_id="BTCUSDT-PERP.BINANCE",
            side="BUY",
            quantity=Decimal("0.001"),
        )
        result = pre_trade_risk.check(
            intent=intent,
            current_position_usd=Decimal(0),
            current_open_orders=11,  # 最大值为 10
            current_price=Decimal("50000"),
        )

        assert result.passed is False


# ---------------------------------------------------------------------------
# 填充处理程序 → 贸易持续性 集成
# ---------------------------------------------------------------------------


def query_trades(persistence, limit=10):
    """辅助函数：直接查 PostgreSQL 获取最近成交记录。.

    Args:
        persistence: Persistence component used by the operation.
        limit: Maximum number of items to process or return.
    """
    with persistence._conn.cursor() as cur:
        cur.execute(
            "SELECT instrument_id, side, quantity, price, order_id, strategy_id, fees FROM trades ORDER BY timestamp_ns DESC LIMIT %s",
            (limit,),
        )
        cols = [desc[0] for desc in cur.description]
        return [dict(zip(cols, row, strict=False)) for row in cur.fetchall()]


class TestFillHandlerWithPersistence:
    """Test cases for fill handler with persistence."""

    def test_fill_recorded_in_db(self, fill_handler, tmp_db):
        """on_fill() 后，交易记录写入 PostgreSQL。.

        Args:
            fill_handler: Fill handler under test.
            tmp_db: Temporary database path or fixture for persistence checks.
        """
        fill_handler.on_fill(
            instrument_id="BTCUSDT-PERP.BINANCE",
            side="BUY",
            quantity="0.01",
            price="50000",
            order_id="ORD-001",
            strategy_id="ema_cross",
            fees="0.1",
        )

        trades = query_trades(tmp_db)
        assert len(trades) == 1
        t = trades[0]
        assert t["instrument_id"] == "BTCUSDT-PERP.BINANCE"
        assert t["side"] == "BUY"
        assert t["order_id"] == "ORD-001"
        assert t["strategy_id"] == "ema_cross"

    def test_multiple_fills_all_recorded(self, fill_handler, tmp_db):
        """多笔成交都被正确持久化。.

        Args:
            fill_handler: Fill handler under test.
            tmp_db: Temporary database path or fixture for persistence checks.
        """
        fills = [
            ("BTCUSDT-PERP.BINANCE", "BUY", "0.01", "50000", "O1", "s1"),
            ("ETHUSDT-PERP.BINANCE", "SELL", "0.5", "3000", "O2", "s2"),
            ("BTCUSDT-PERP.BINANCE", "SELL", "0.01", "51000", "O3", "s1"),
        ]
        for inst, side, qty, price, oid, sid in fills:
            fill_handler.on_fill(
                instrument_id=inst,
                side=side,
                quantity=qty,
                price=price,
                order_id=oid,
                strategy_id=sid,
            )

        trades = query_trades(tmp_db)
        assert len(trades) == 3

    def test_fill_publishes_event(self, fill_handler, event_bus):
        """on_fill() 发布 ORDER_FILLED 事件到事件总线。.

        Args:
            fill_handler: Fill handler under test.
            event_bus: Event bus fixture or instance used in the test.
        """
        filled_events = []
        event_bus.subscribe(EventType.ORDER_FILLED, filled_events.append)

        fill_handler.on_fill(
            instrument_id="BTCUSDT-PERP.BINANCE",
            side="BUY",
            quantity="0.01",
            price="50000",
        )

        assert len(filled_events) == 1
        assert filled_events[0].event_type == EventType.ORDER_FILLED


# ---------------------------------------------------------------------------
# 完整流水线测试（信号 → 风险 → 成交 → DB）
# ---------------------------------------------------------------------------


class TestFullPipeline:
    """Test cases for full pipeline."""

    def test_signal_to_fill_pipeline(self, event_bus, pre_trade_risk, fill_handler, tmp_db):
        """模拟完整链路.

        1. 策略发布 SignalEvent
        2. 信号处理器构造 OrderIntentEvent 并经风控审核
        3. 通过风控后触发 FillHandler
        4. 成交写入 DB.

        Args:
            event_bus: Event bus fixture or instance used in the test.
            pre_trade_risk: Pre-trade risk checker fixture under test.
            fill_handler: Fill handler under test.
            tmp_db: Temporary database path or fixture for persistence checks.
        """
        pipeline_log: list[str] = []

        def on_signal(event: SignalEvent):
            pipeline_log.append("signal_received")
            intent = OrderIntentEvent(
                instrument_id=event.instrument_id,
                side="BUY" if event.direction == SignalDirection.LONG else "SELL",
                quantity=Decimal("0.01"),
                source="ema_cross",
            )
            result = pre_trade_risk.check(
                intent=intent,
                current_position_usd=Decimal(0),
                current_open_orders=0,
                current_price=Decimal("50000"),
            )
            if result.passed:
                pipeline_log.append("risk_passed")
                fill_handler.on_fill(
                    instrument_id=intent.instrument_id,
                    side=intent.side,
                    quantity=str(intent.quantity),
                    price="50000",
                    strategy_id=intent.source,
                )

        event_bus.subscribe(EventType.SIGNAL, on_signal)

        # 发布信号触发整个流水线
        event_bus.publish(
            SignalEvent(
                instrument_id="BTCUSDT-PERP.BINANCE",
                direction=SignalDirection.LONG,
                strength=0.9,
                source="ema_cross",
            )
        )

        assert "signal_received" in pipeline_log
        assert "risk_passed" in pipeline_log

        trades = query_trades(tmp_db)
        assert len(trades) == 1
        assert trades[0]["instrument_id"] == "BTCUSDT-PERP.BINANCE"

    def test_risk_blocked_signal_not_persisted(self, event_bus, pre_trade_risk, fill_handler, tmp_db):
        """被风控拦截的信号不应产生成交记录。.

        Args:
            event_bus: Event bus fixture or instance used in the test.
            pre_trade_risk: Pre-trade risk checker fixture under test.
            fill_handler: Fill handler under test.
            tmp_db: Temporary database path or fixture for persistence checks.
        """

        def on_signal(event: SignalEvent):
            intent = OrderIntentEvent(
                instrument_id=event.instrument_id,
                side="BUY",
                quantity=Decimal("10"),  # 超大订单，必然被拦截
            )
            result = pre_trade_risk.check(
                intent=intent,
                current_position_usd=Decimal(0),
                current_open_orders=0,
                current_price=Decimal("50000"),
            )
            if result.passed:
                fill_handler.on_fill(
                    instrument_id=intent.instrument_id,
                    side=intent.side,
                    quantity=str(intent.quantity),
                    price="50000",
                )

        event_bus.subscribe(EventType.SIGNAL, on_signal)
        event_bus.publish(
            SignalEvent(
                instrument_id="BTCUSDT-PERP.BINANCE",
                direction=SignalDirection.LONG,
            )
        )

        trades = query_trades(tmp_db)
        assert len(trades) == 0
