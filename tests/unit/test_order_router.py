"""OrderRouter 单元测试.

使用 Mock 替换 Nautilus Strategy，完全不依赖真实引擎，
覆盖路由逻辑、错误处理和事件发布三条路径。
"""

from __future__ import annotations

from decimal import Decimal
from unittest.mock import MagicMock

import pytest

from src.core.events import EventBus, EventType
from src.execution.order_intent import OrderIntent
from src.execution.order_router import OrderRouter

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def event_bus():
    bus = EventBus()
    yield bus
    bus.clear()


@pytest.fixture
def router(event_bus):
    return OrderRouter(event_bus=event_bus)


def make_intent(
    instrument_id: str = "BTCUSDT-PERP.BINANCE",
    side: str = "BUY",
    quantity: str = "0.01",
    order_type: str = "MARKET",
    reduce_only: bool = False,
) -> OrderIntent:
    """构造测试用 OrderIntent。"""
    return OrderIntent(
        instrument_id=instrument_id,
        side=side,
        quantity=Decimal(quantity),
        order_type=order_type,
        reduce_only=reduce_only,
    )


def make_mock_strategy(instrument_id: str = "BTCUSDT-PERP.BINANCE") -> MagicMock:
    """构造 Mock Strategy，模拟 cache / order_factory / submit_order。"""
    strategy = MagicMock()

    # mock instrument
    instrument = MagicMock()
    instrument.id = MagicMock()
    instrument.id.__str__ = lambda self: instrument_id
    instrument.make_qty = lambda qty: qty  # 直接透传
    strategy.cache.instrument.return_value = instrument

    # mock order_factory.market() 返回一个假订单
    fake_order = MagicMock()
    strategy.order_factory.market.return_value = fake_order

    # submit_order 默认不抛异常
    strategy.submit_order.return_value = None

    return strategy


# ---------------------------------------------------------------------------
# 基础行为
# ---------------------------------------------------------------------------


class TestOrderRouterBasic:
    def test_route_without_strategy_returns_false(self, router):
        """未绑定策略时，route() 返回 False 且不报错。"""
        intent = make_intent()
        result = router.route(intent)
        assert result is False

    def test_bind_strategy_and_route_returns_true(self, router):
        """绑定策略后，正常订单 route() 返回 True。"""
        strategy = make_mock_strategy()
        router.bind_strategy(strategy)

        intent = make_intent()
        result = router.route(intent)

        assert result is True

    def test_route_calls_submit_order(self, router):
        """route() 必须调用 strategy.submit_order()。"""
        strategy = make_mock_strategy()
        router.bind_strategy(strategy)

        router.route(make_intent())

        strategy.submit_order.assert_called_once()

    def test_route_calls_order_factory_market(self, router):
        """MARKET 类型订单调用 order_factory.market()。"""
        strategy = make_mock_strategy()
        router.bind_strategy(strategy)

        router.route(make_intent(order_type="MARKET"))

        strategy.order_factory.market.assert_called_once()

    def test_route_instrument_not_found_returns_false(self, router):
        """cache.instrument() 返回 None 时，route() 返回 False。"""
        strategy = make_mock_strategy()
        strategy.cache.instrument.return_value = None
        router.bind_strategy(strategy)

        result = router.route(make_intent())

        assert result is False
        strategy.submit_order.assert_not_called()


# ---------------------------------------------------------------------------
# 买卖方向映射
# ---------------------------------------------------------------------------


class TestOrderSideMapping:
    def test_buy_side_mapped_correctly(self, router):
        """BUY 意图正确传递给 order_factory。"""
        from nautilus_trader.model.enums import OrderSide

        strategy = make_mock_strategy()
        router.bind_strategy(strategy)

        router.route(make_intent(side="BUY"))

        call_kwargs = strategy.order_factory.market.call_args
        assert call_kwargs.kwargs["order_side"] == OrderSide.BUY

    def test_sell_side_mapped_correctly(self, router):
        """SELL 意图正确传递给 order_factory。"""
        from nautilus_trader.model.enums import OrderSide

        strategy = make_mock_strategy()
        router.bind_strategy(strategy)

        router.route(make_intent(side="SELL"))

        call_kwargs = strategy.order_factory.market.call_args
        assert call_kwargs.kwargs["order_side"] == OrderSide.SELL

    def test_reduce_only_passed_through(self, router):
        """reduce_only=True 正确传递给 order_factory。"""
        strategy = make_mock_strategy()
        router.bind_strategy(strategy)

        router.route(make_intent(reduce_only=True))

        call_kwargs = strategy.order_factory.market.call_args
        assert call_kwargs.kwargs["reduce_only"] is True


# ---------------------------------------------------------------------------
# 事件发布
# ---------------------------------------------------------------------------


class TestOrderRouterEvents:
    def test_successful_route_publishes_order_intent_event(self, router, event_bus):
        """成功路由后发布 ORDER_INTENT 事件。"""
        received = []
        event_bus.subscribe(EventType.ORDER_INTENT, received.append)

        strategy = make_mock_strategy()
        router.bind_strategy(strategy)
        router.route(make_intent())

        assert len(received) == 1

    def test_failed_route_no_instrument_no_event(self, router, event_bus):
        """instrument 未找到时，不发布事件。"""
        received = []
        event_bus.subscribe(EventType.ORDER_INTENT, received.append)

        strategy = make_mock_strategy()
        strategy.cache.instrument.return_value = None
        router.bind_strategy(strategy)
        router.route(make_intent())

        assert len(received) == 0

    def test_submit_order_exception_returns_false(self, router, event_bus):
        """submit_order 抛出异常时，route() 返回 False，不崩溃。"""
        strategy = make_mock_strategy()
        strategy.submit_order.side_effect = RuntimeError("exchange down")
        router.bind_strategy(strategy)

        result = router.route(make_intent())

        assert result is False

    def test_submit_order_exception_no_event_published(self, router, event_bus):
        """submit_order 异常时，不发布 ORDER_INTENT 事件。"""
        received = []
        event_bus.subscribe(EventType.ORDER_INTENT, received.append)

        strategy = make_mock_strategy()
        strategy.submit_order.side_effect = RuntimeError("exchange down")
        router.bind_strategy(strategy)
        router.route(make_intent())

        assert len(received) == 0


# ---------------------------------------------------------------------------
# OrderIntent.from_signal 工厂方法
# ---------------------------------------------------------------------------


class TestOrderIntentFromSignal:
    def test_long_signal_creates_buy_intent(self):
        """LONG 信号 → BUY 意图。"""
        from src.core.events import SignalDirection

        intent = OrderIntent.from_signal(
            instrument_id="BTCUSDT-PERP.BINANCE",
            direction=SignalDirection.LONG,
            quantity=Decimal("0.01"),
        )
        assert intent.side == "BUY"
        assert intent.reduce_only is False

    def test_short_signal_creates_sell_intent(self):
        """SHORT 信号 → SELL 意图。"""
        from src.core.events import SignalDirection

        intent = OrderIntent.from_signal(
            instrument_id="BTCUSDT-PERP.BINANCE",
            direction=SignalDirection.SHORT,
            quantity=Decimal("0.01"),
        )
        assert intent.side == "SELL"
        assert intent.reduce_only is False

    def test_flat_signal_creates_reduce_only_intent(self):
        """FLAT 信号 → reduce_only=True（平仓意图）。"""
        from src.core.events import SignalDirection

        intent = OrderIntent.from_signal(
            instrument_id="BTCUSDT-PERP.BINANCE",
            direction=SignalDirection.FLAT,
            quantity=Decimal("0.01"),
        )
        assert intent.reduce_only is True

    def test_strategy_id_propagated(self):
        """strategy_id 正确传递到 intent。"""
        from src.core.events import SignalDirection

        intent = OrderIntent.from_signal(
            instrument_id="BTCUSDT-PERP.BINANCE",
            direction=SignalDirection.LONG,
            quantity=Decimal("0.01"),
            strategy_id="ema_cross",
        )
        assert intent.strategy_id == "ema_cross"

    def test_intent_is_immutable(self):
        """OrderIntent 是 frozen dataclass，不允许修改字段。"""
        from src.core.events import SignalDirection

        intent = OrderIntent.from_signal(
            instrument_id="BTCUSDT-PERP.BINANCE",
            direction=SignalDirection.LONG,
            quantity=Decimal("0.01"),
        )
        with pytest.raises((AttributeError, TypeError)):
            intent.side = "SELL"  # type: ignore
