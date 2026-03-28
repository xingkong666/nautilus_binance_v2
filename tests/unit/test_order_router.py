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
    """Run event bus."""
    bus = EventBus()
    yield bus
    bus.clear()


@pytest.fixture
def router(event_bus):
    """Run router.

    Args:
        event_bus: Event bus used for cross-module communication.
    """
    return OrderRouter(event_bus=event_bus)


def make_intent(
    instrument_id: str = "BTCUSDT-PERP.BINANCE",
    side: str = "BUY",
    quantity: str = "0.01",
    order_type: str = "MARKET",
    price: str | None = None,
    reduce_only: bool = False,
) -> OrderIntent:
    """构造测试用 OrderIntent。.

    Args:
        instrument_id: Instrument identifier to target.
        side: Side.
        quantity: Order quantity to use.
        order_type: Order type.
        price: Price.
        reduce_only: Reduce only.
    """
    return OrderIntent(
        instrument_id=instrument_id,
        side=side,
        quantity=Decimal(quantity),
        order_type=order_type,
        price=Decimal(price) if price is not None else None,
        reduce_only=reduce_only,
    )


def make_mock_strategy(instrument_id: str = "BTCUSDT-PERP.BINANCE") -> MagicMock:
    """构造 Mock Strategy，模拟 cache / order_factory / submit_order。.

    Args:
        instrument_id: Instrument identifier to target.
    """
    strategy = MagicMock()
    strategy.config.instrument_id = instrument_id

    # mock instrument
    instrument = MagicMock()
    instrument.id = MagicMock()
    instrument.id.__str__ = lambda self: instrument_id
    instrument.make_qty = lambda qty: qty  # 直接透传
    instrument.make_price = lambda price: price  # 直接透传
    strategy.cache.instrument.return_value = instrument

    # mock order_factory.market() 返回一个假订单
    fake_order = MagicMock()
    strategy.order_factory.market.return_value = fake_order
    strategy.order_factory.limit.return_value = fake_order

    # submit_order 默认不抛异常
    strategy.submit_order.return_value = None

    return strategy


# ---------------------------------------------------------------------------
# 基础行为
# ---------------------------------------------------------------------------


class TestOrderRouterBasic:
    """Test cases for order router basic."""

    def test_route_without_strategy_returns_false(self, router):
        """未绑定策略时，route() 返回 False 且不报错。.

        Args:
            router: Order router fixture under test.
        """
        intent = make_intent()
        result = router.route(intent)
        assert result is False

    def test_bind_strategy_and_route_returns_true(self, router):
        """绑定策略后，正常订单 route() 返回 True。.

        Args:
            router: Order router fixture under test.
        """
        strategy = make_mock_strategy()
        router.bind_strategy(strategy)

        intent = make_intent()
        result = router.route(intent)

        assert result is True

    def test_route_calls_submit_order(self, router):
        """route() 必须调用 strategy.submit_order()。.

        Args:
            router: Order router fixture under test.
        """
        strategy = make_mock_strategy()
        router.bind_strategy(strategy)

        router.route(make_intent())

        strategy.submit_order.assert_called_once()

    def test_route_calls_order_factory_market(self, router):
        """MARKET 类型订单调用 order_factory.market()。.

        Args:
            router: Order router fixture under test.
        """
        strategy = make_mock_strategy()
        router.bind_strategy(strategy)

        router.route(make_intent(order_type="MARKET"))

        strategy.order_factory.market.assert_called_once()

    def test_route_limit_calls_order_factory_limit(self, router):
        """Verify that route limit calls order factory limit.

        Args:
            router: Router.
        """
        strategy = make_mock_strategy()
        router.bind_strategy(strategy)

        router.route(make_intent(order_type="LIMIT", price="50000"))

        strategy.order_factory.limit.assert_called_once()
        strategy.order_factory.market.assert_not_called()

    def test_route_instrument_not_found_returns_false(self, router):
        """cache.instrument() 返回 None 时，route() 返回 False。.

        Args:
            router: Order router fixture under test.
        """
        strategy = make_mock_strategy()
        strategy.cache.instrument.return_value = None
        router.bind_strategy(strategy)

        result = router.route(make_intent())

        assert result is False
        strategy.submit_order.assert_not_called()

    def test_route_uses_bound_strategy_for_matching_instrument(self, router):
        """Verify that route uses bound strategy for matching instrument.

        Args:
            router: Router.
        """
        btc_strategy = make_mock_strategy("BTCUSDT-PERP.BINANCE")
        eth_strategy = make_mock_strategy("ETHUSDT-PERP.BINANCE")
        router.bind_strategy(btc_strategy)
        router.bind_strategy(eth_strategy)

        result = router.route(make_intent(instrument_id="ETHUSDT-PERP.BINANCE"))

        assert result is True
        eth_strategy.submit_order.assert_called_once()
        btc_strategy.submit_order.assert_not_called()


# ---------------------------------------------------------------------------
# 买卖方向映射
# ---------------------------------------------------------------------------


class TestOrderSideMapping:
    """Test cases for order side mapping."""

    def test_buy_side_mapped_correctly(self, router):
        """BUY 意图正确传递给 order_factory。.

        Args:
            router: Order router fixture under test.
        """
        from nautilus_trader.model.enums import OrderSide

        strategy = make_mock_strategy()
        router.bind_strategy(strategy)

        router.route(make_intent(side="BUY"))

        call_kwargs = strategy.order_factory.market.call_args
        assert call_kwargs.kwargs["order_side"] == OrderSide.BUY

    def test_sell_side_mapped_correctly(self, router):
        """SELL 意图正确传递给 order_factory。.

        Args:
            router: Order router fixture under test.
        """
        from nautilus_trader.model.enums import OrderSide

        strategy = make_mock_strategy()
        router.bind_strategy(strategy)

        router.route(make_intent(side="SELL"))

        call_kwargs = strategy.order_factory.market.call_args
        assert call_kwargs.kwargs["order_side"] == OrderSide.SELL

    def test_reduce_only_passed_through(self, router):
        """reduce_only=True 正确传递给 order_factory。.

        Args:
            router: Order router fixture under test.
        """
        strategy = make_mock_strategy()
        router.bind_strategy(strategy)

        router.route(make_intent(reduce_only=True))

        call_kwargs = strategy.order_factory.market.call_args
        assert call_kwargs.kwargs["reduce_only"] is True

    def test_limit_post_only_passed_through(self, router):
        """Verify that limit post only passed through.

        Args:
            router: Router.
        """
        strategy = make_mock_strategy()
        router.bind_strategy(strategy)

        intent = make_intent(order_type="LIMIT", price="50000")
        intent = OrderIntent(
            instrument_id=intent.instrument_id,
            side=intent.side,
            quantity=intent.quantity,
            order_type=intent.order_type,
            price=intent.price,
            metadata={"post_only": True},
        )
        router.route(intent)

        call_kwargs = strategy.order_factory.limit.call_args
        assert call_kwargs.kwargs["post_only"] is True

    def test_limit_without_price_returns_false(self, router):
        """Verify that limit without price returns false.

        Args:
            router: Router.
        """
        strategy = make_mock_strategy()
        router.bind_strategy(strategy)

        result = router.route(make_intent(order_type="LIMIT"))

        assert result is False
        strategy.submit_order.assert_not_called()

    def test_limit_chase_ticks_adjusts_price(self, router):
        """Verify that limit chase ticks adjusts price.

        Args:
            router: Router.
        """
        strategy = make_mock_strategy()
        strategy.cache.instrument.return_value.price_increment = "0.5"
        router.bind_strategy(strategy)

        intent = make_intent(order_type="LIMIT", price="50000")
        intent = OrderIntent(
            instrument_id=intent.instrument_id,
            side="BUY",
            quantity=intent.quantity,
            order_type=intent.order_type,
            price=intent.price,
            metadata={"chase_ticks": 2},
        )
        router.route(intent)

        call_kwargs = strategy.order_factory.limit.call_args
        assert str(call_kwargs.kwargs["price"]) == "50001.0"

    def test_limit_ttl_maps_to_ioc_when_not_post_only(self, router):
        """Verify that limit ttl maps to ioc when not post only.

        Args:
            router: Router.
        """
        from nautilus_trader.model.enums import TimeInForce

        strategy = make_mock_strategy()
        router.bind_strategy(strategy)

        intent = make_intent(order_type="LIMIT", price="50000")
        intent = OrderIntent(
            instrument_id=intent.instrument_id,
            side="BUY",
            quantity=intent.quantity,
            order_type=intent.order_type,
            price=intent.price,
            metadata={"limit_ttl_ms": 2500, "post_only": False},
        )
        router.route(intent)

        call_kwargs = strategy.order_factory.limit.call_args
        assert call_kwargs.kwargs["time_in_force"] == TimeInForce.IOC

    def test_limit_ttl_ignored_when_post_only(self, router):
        """Verify that limit ttl ignored when post only.

        Args:
            router: Router.
        """
        from nautilus_trader.model.enums import TimeInForce

        strategy = make_mock_strategy()
        router.bind_strategy(strategy)

        intent = make_intent(order_type="LIMIT", price="50000")
        intent = OrderIntent(
            instrument_id=intent.instrument_id,
            side=intent.side,
            quantity=intent.quantity,
            order_type=intent.order_type,
            price=intent.price,
            metadata={"limit_ttl_ms": 2500, "post_only": True},
        )
        router.route(intent)

        call_kwargs = strategy.order_factory.limit.call_args
        assert call_kwargs.kwargs["time_in_force"] == TimeInForce.GTC

    def test_quantity_normalized_to_size_increment(self, router):
        """Verify that quantity normalized to size increment.

        Args:
            router: Router.
        """
        strategy = make_mock_strategy()
        strategy.cache.instrument.return_value.size_increment = "0.001"
        router.bind_strategy(strategy)

        router.route(make_intent(quantity="0.0014"))

        call_kwargs = strategy.order_factory.market.call_args
        assert str(call_kwargs.kwargs["quantity"]) == "0.001"

    def test_quantity_below_size_increment_rejected(self, router):
        """Verify that quantity below size increment rejected.

        Args:
            router: Router.
        """
        strategy = make_mock_strategy()
        strategy.cache.instrument.return_value.size_increment = "0.001"
        router.bind_strategy(strategy)

        result = router.route(make_intent(quantity="0.0004"))

        assert result is False
        strategy.submit_order.assert_not_called()


# ---------------------------------------------------------------------------
# 事件发布
# ---------------------------------------------------------------------------


class TestOrderRouterEvents:
    """Test cases for order router events."""

    def test_disabled_submission_keeps_routing_semantics(self, event_bus):
        """Verify that disabled submission keeps routing semantics."""
        alerts = []
        received = []
        event_bus.subscribe(EventType.RISK_ALERT, alerts.append)
        event_bus.subscribe(EventType.ORDER_INTENT, received.append)

        router = OrderRouter(event_bus=event_bus, submit_orders=False)
        strategy = make_mock_strategy()
        router.bind_strategy(strategy)

        result = router.route(make_intent())

        assert result is True
        strategy.order_factory.market.assert_called_once()
        strategy.submit_order.assert_not_called()
        assert len(received) == 1
        assert len(alerts) == 1
        alert = alerts[0]
        assert alert.level == "WARNING"
        assert alert.rule_name == "order_submission_disabled"

    def test_successful_route_publishes_order_intent_event(self, router, event_bus):
        """成功路由后发布 ORDER_INTENT 事件。.

        Args:
            router: Order router fixture under test.
            event_bus: Event bus fixture or instance used in the test.
        """
        received = []
        event_bus.subscribe(EventType.ORDER_INTENT, received.append)

        strategy = make_mock_strategy()
        router.bind_strategy(strategy)
        router.route(make_intent())

        assert len(received) == 1

    def test_failed_route_no_instrument_no_event(self, router, event_bus):
        """Instrument 未找到时，不发布事件。.

        Args:
            router: Order router fixture under test.
            event_bus: Event bus fixture or instance used in the test.
        """
        received = []
        event_bus.subscribe(EventType.ORDER_INTENT, received.append)

        strategy = make_mock_strategy()
        strategy.cache.instrument.return_value = None
        router.bind_strategy(strategy)
        router.route(make_intent())

        assert len(received) == 0

    def test_submit_order_exception_returns_false(self, router, event_bus):
        """submit_order 抛出异常时，route() 返回 False，不崩溃。.

        Args:
            router: Order router fixture under test.
            event_bus: Event bus fixture or instance used in the test.
        """
        strategy = make_mock_strategy()
        strategy.submit_order.side_effect = RuntimeError("exchange down")
        router.bind_strategy(strategy)

        result = router.route(make_intent())

        assert result is False

    def test_submit_order_exception_no_event_published(self, router, event_bus):
        """submit_order 异常时，不发布 ORDER_INTENT 事件。.

        Args:
            router: Order router fixture under test.
            event_bus: Event bus fixture or instance used in the test.
        """
        received = []
        event_bus.subscribe(EventType.ORDER_INTENT, received.append)

        strategy = make_mock_strategy()
        strategy.submit_order.side_effect = RuntimeError("exchange down")
        router.bind_strategy(strategy)
        router.route(make_intent())

        assert len(received) == 0

    def test_quantity_normalization_publishes_warning_alert(self, router, event_bus):
        """Verify that quantity normalization publishes warning alert.

        Args:
            router: Router.
            event_bus: Event bus used for cross-module communication.
        """
        from src.core.events import EventType

        alerts = []
        event_bus.subscribe(EventType.RISK_ALERT, alerts.append)

        strategy = make_mock_strategy()
        strategy.cache.instrument.return_value.size_increment = "0.001"
        router.bind_strategy(strategy)

        result = router.route(make_intent(quantity="0.0014"))

        assert result is True
        assert len(alerts) == 1
        alert = alerts[0]
        assert alert.level == "WARNING"
        assert alert.rule_name == "order_router_quantity_normalized"
        assert alert.details["raw_quantity"] == "0.0014"
        assert alert.details["normalized_quantity"] == "0.001"

    def test_quantity_below_step_publishes_error_alert(self, router, event_bus):
        """Verify that quantity below step publishes error alert.

        Args:
            router: Router.
            event_bus: Event bus used for cross-module communication.
        """
        from src.core.events import EventType

        alerts = []
        event_bus.subscribe(EventType.RISK_ALERT, alerts.append)

        strategy = make_mock_strategy()
        strategy.cache.instrument.return_value.size_increment = "0.001"
        router.bind_strategy(strategy)

        result = router.route(make_intent(quantity="0.0004"))

        assert result is False
        assert len(alerts) == 1
        alert = alerts[0]
        assert alert.level == "ERROR"
        assert alert.rule_name == "order_router_quantity_below_step"
        assert alert.details["raw_quantity"] == "0.0004"


# ---------------------------------------------------------------------------
# OrderIntent.from_signal 工厂方法
# ---------------------------------------------------------------------------


class TestOrderIntentFromSignal:
    """Test cases for order intent from signal."""

    def test_long_signal_creates_buy_intent(self):
        """LONG 信号 → BUY 意图。."""
        from src.core.events import SignalDirection

        intent = OrderIntent.from_signal(
            instrument_id="BTCUSDT-PERP.BINANCE",
            direction=SignalDirection.LONG,
            quantity=Decimal("0.01"),
        )
        assert intent.side == "BUY"
        assert intent.reduce_only is False

    def test_short_signal_creates_sell_intent(self):
        """SHORT 信号 → SELL 意图。."""
        from src.core.events import SignalDirection

        intent = OrderIntent.from_signal(
            instrument_id="BTCUSDT-PERP.BINANCE",
            direction=SignalDirection.SHORT,
            quantity=Decimal("0.01"),
        )
        assert intent.side == "SELL"
        assert intent.reduce_only is False

    def test_flat_signal_creates_reduce_only_intent(self):
        """FLAT 信号 → reduce_only=True（平仓意图）。."""
        from src.core.events import SignalDirection

        intent = OrderIntent.from_signal(
            instrument_id="BTCUSDT-PERP.BINANCE",
            direction=SignalDirection.FLAT,
            quantity=Decimal("0.01"),
        )
        assert intent.reduce_only is True

    def test_strategy_id_propagated(self):
        """strategy_id 正确传递到 intent。."""
        from src.core.events import SignalDirection

        intent = OrderIntent.from_signal(
            instrument_id="BTCUSDT-PERP.BINANCE",
            direction=SignalDirection.LONG,
            quantity=Decimal("0.01"),
            strategy_id="ema_cross",
        )
        assert intent.strategy_id == "ema_cross"

    def test_intent_is_immutable(self):
        """OrderIntent 是 frozen dataclass，不允许修改字段。."""
        from src.core.events import SignalDirection

        intent = OrderIntent.from_signal(
            instrument_id="BTCUSDT-PERP.BINANCE",
            direction=SignalDirection.LONG,
            quantity=Decimal("0.01"),
        )
        with pytest.raises((AttributeError, TypeError)):
            intent.side = "SELL"  # type: ignore
