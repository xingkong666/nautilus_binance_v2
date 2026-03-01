#!/usr/bin/env python3
# -*- coding: UTF-8 -*-
################################################################################
#
# Copyright (c) 2026 xx, Inc. All Rights Reserved
#
################################################################################
"""EMA方向判定 + 回撤/反弹触发入场 + ATR跟踪止损策略.


UUID=$(cat /proc/sys/kernel/random/uuid); echo $UUID
sudo xray x25519
dbf3f9be-00fb-4b24-ac83-4969bebc9dcd
PrivateKey: IMrqCe_wi-zw1JFwJaxcnOLNmL7auMBAcZAEdCnR7lI
Password: vXFczpVOLA7UGyNMZCe6S3XMvt0THyIuTodzj1aunRE
Hash32: 8gC0FGZ-ay3bS6WhynEh1Zg_WLQYU5yjcQSDsMZeSf0

Authors: Leo
File:   ema_cross_stop_entry.py
Date:   2026/02/09 23:40:12
"""

from decimal import Decimal

import pandas as pd
from nautilus_trader.common.enums import LogColor
from nautilus_trader.config import PositiveFloat, PositiveInt, StrategyConfig
from nautilus_trader.core.correctness import PyCondition
from nautilus_trader.core.data import Data
from nautilus_trader.core.message import Event
from nautilus_trader.indicators import AverageTrueRange, ExponentialMovingAverage
from nautilus_trader.model.book import OrderBook
from nautilus_trader.model.data import Bar, BarType, QuoteTick, TradeTick
from nautilus_trader.model.enums import OrderSide, TimeInForce, TrailingOffsetType, TriggerType
from nautilus_trader.model.events import OrderFilled
from nautilus_trader.model.identifiers import InstrumentId, PositionId
from nautilus_trader.model.instruments import Instrument
from nautilus_trader.model.objects import Price
from nautilus_trader.model.orders import MarketIfTouchedOrder, TrailingStopMarketOrder
from nautilus_trader.trading.strategy import Strategy


class EMACrossStopEntryConfig(StrategyConfig, frozen=True):
    """Configuration for ``EMACrossStopEntry`` instances.

    Parameters
    ----------
    instrument_id : InstrumentId
        The instrument ID for the strategy.
    bar_type : BarType
        The bar type for the strategy.
    atr_period : PositiveInt
        The period for the ATR indicator.
    trailing_atr_multiple : PositiveFloat
        The ATR multiple for the trailing stop.
    trailing_offset_type : str
        The trailing offset type (interpreted as `TrailingOffsetType`).
    trailing_offset : Decimal
        The trailing offset amount.
    trigger_type : str
        The trailing stop trigger type (interpreted as `TriggerType`).
    trade_size : Decimal
        The position size per trade.
    fast_ema_period : PositiveInt, default 10
        The fast EMA period.
    slow_ema_period : PositiveInt, default 20
        The slow EMA period.
    emulation_trigger : str, default 'NO_TRIGGER'
        The emulation trigger for submitting emulated orders.
        If 'NONE' then orders will not be emulated.

    """

    instrument_id: InstrumentId  # 交易对
    bar_type: BarType  # K线周期
    atr_period: PositiveInt  # ATR周期
    trailing_atr_multiple: PositiveFloat  # ATR倍数
    trailing_offset_type: str  # 偏移类型
    trailing_offset: Decimal  # 偏移量
    trigger_type: str  # 触发类型
    trade_size: Decimal  # 交易量
    fast_ema_period: PositiveInt = 10  # 快线周期
    slow_ema_period: PositiveInt = 20  # 慢线周期
    emulation_trigger: str = "NO_TRIGGER"  # 模拟触发器


class EMACrossStopEntry(Strategy):
    """A simple moving average cross example strategy with a `MARKET_IF_TOUCHED` entry and `TRAILING_STOP_MARKET` stop.

    When the fast EMA crosses the slow EMA then submits a `MARKET_IF_TOUCHED` order
    a couple of ticks below the current bar for BUY, or a couple of ticks above
    the current bar for SELL.

    If the entry order is filled then a `TRAILING_STOP_MARKET` at a specified
    ATR distance is submitted and managed.

    Cancels all orders and closes all positions on stop.

    Parameters
    ----------
    config : EMACrossStopEntryConfig
        The configuration for the instance.

    Raises:
    ------
    ValueError
        If `config.fast_ema_period` is not less than `config.slow_ema_period`.

    """

    def __init__(self, config: EMACrossStopEntryConfig) -> None:
        """初始化策略.

        Args:
            config (EMACrossStopEntryConfig): 配置
        """
        PyCondition.is_true(
            config.fast_ema_period < config.slow_ema_period,
            "{config.fast_ema_period=} must be less than {config.slow_ema_period=}",
        )
        super().__init__(config)

        # Create the indicators for the strategy
        self.fast_ema = ExponentialMovingAverage(config.fast_ema_period)
        self.slow_ema = ExponentialMovingAverage(config.slow_ema_period)
        self.atr = AverageTrueRange(config.atr_period)

        # Initialized in `on_start()`
        self.instrument: Instrument | None = None
        self.tick_size: Price | None = None

        # Users order management variables
        self.entry = None
        self.trailing_stop = None

    def on_start(self) -> None:
        """Actions to be performed on strategy start."""
        self.instrument = self.cache.instrument(self.config.instrument_id)
        if self.instrument is None:
            self.log.error(f"Could not find instrument for {self.config.instrument_id}")
            self.stop()
            return

        self.tick_size = self.instrument.price_increment

        # Register the indicators for updating
        self.register_indicator_for_bars(self.config.bar_type, self.fast_ema)
        self.register_indicator_for_bars(self.config.bar_type, self.slow_ema)
        self.register_indicator_for_bars(self.config.bar_type, self.atr)

        # Get historical data
        # self.request_bars(
        #     self.config.bar_type,
        #     start=self._clock.utc_now() - pd.Timedelta(days=1),
        # )

        # Subscribe to live data
        self.subscribe_bars(self.config.bar_type)
        self.subscribe_quote_ticks(self.config.instrument_id)
        self.subscribe_trade_ticks(self.config.instrument_id)

    def on_instrument(self, instrument: Instrument) -> None:
        """Actions to be performed when the strategy is running and receives an instrument.

        Parameters
        ----------
        instrument : Instrument
            The instrument received.

        """

    def on_order_book(self, order_book: OrderBook) -> None:
        """Actions to be performed when the strategy is running and receives an order book.

        Parameters
        ----------
        order_book : OrderBook
            The order book received.

        """
        # self.log.info(f"Received {order_book}")  # For debugging (must add a subscription)

    def on_quote_tick(self, tick: QuoteTick) -> None:
        """Actions to be performed when the strategy is running and receives a quote tick.

        Parameters
        ----------
        tick : QuoteTick
            The tick received.

        """

    def on_trade_tick(self, tick: TradeTick) -> None:
        """Actions to be performed when the strategy is running and receives a trade tick.

        Parameters
        ----------
        tick : TradeTick
            The tick received.

        """

    def on_bar(self, bar: Bar) -> None:
        """Actions to be performed when the strategy is running and receives a bar.

        Parameters
        ----------
        bar : Bar
            The bar received.

        """
        # self.log.info(f"Received {bar!r}")

        # Check if indicators ready
        if not self.indicators_initialized():
            self.log.info(
                f"Waiting for indicators to warm up [{self.cache.bar_count(self.config.bar_type)}]",
                color=LogColor.BLUE,
            )
            return  # Wait for indicators to warm up...

        if self.portfolio.is_flat(self.config.instrument_id):
            if self.entry is not None:
                self.cancel_order(self.entry)

            # BUY LOGIC
            if self.fast_ema.value >= self.slow_ema.value:
                self.entry_buy(bar)
            # SELL LOGIC
            else:  # fast_ema.value < self.slow_ema.value
                self.entry_sell(bar)

    def entry_buy(self, last_bar: Bar) -> None:
        """Users simple buy entry method (example).

        Parameters
        ----------
        last_bar : Bar
            The last bar received.

        """
        if not self.instrument:
            self.log.error("No instrument loaded")
            return

        if not self.tick_size:
            self.log.error("No tick size loaded")
            return

        order: MarketIfTouchedOrder = self.order_factory.market_if_touched(
            instrument_id=self.config.instrument_id,
            order_side=OrderSide.BUY,
            quantity=self.instrument.make_qty(self.config.trade_size),
            time_in_force=TimeInForce.GTC,
            trigger_price=self.instrument.make_price(last_bar.low - (self.tick_size * 2)),
            emulation_trigger=TriggerType[self.config.emulation_trigger],
        )
        # TODO: Uncomment below order for development
        # order: LimitIfTouchedOrder = self.order_factory.limit_if_touched(
        #     instrument_id=self.config.instrument_id,
        #     order_side=OrderSide.BUY,
        #     quantity=self.instrument.make_qty(self.config.trade_size),
        #     time_in_force=TimeInForce.IOC,
        #     price=self.instrument.make_price(last_bar.low - (self.tick_size * 2)),
        #     trigger_price=self.instrument.make_price(last_bar.high + (self.tick_size * 2)),
        # )

        self.entry = order
        self.submit_order(order)

    def entry_sell(self, last_bar: Bar) -> None:
        """Users simple sell entry method (example).

        Parameters
        ----------
        last_bar : Bar
            The last bar received.

        """
        if not self.instrument:
            self.log.error("No instrument loaded")
            return

        if not self.tick_size:
            self.log.error("No tick size loaded")
            return

        order: MarketIfTouchedOrder = self.order_factory.market_if_touched(
            instrument_id=self.config.instrument_id,
            order_side=OrderSide.SELL,
            quantity=self.instrument.make_qty(self.config.trade_size),
            time_in_force=TimeInForce.GTC,
            trigger_price=self.instrument.make_price(last_bar.high + (self.tick_size * 2)),
            emulation_trigger=TriggerType[self.config.emulation_trigger],
        )
        # TODO: Uncomment below order for development
        # order: LimitIfTouchedOrder = self.order_factory.limit_if_touched(
        #     instrument_id=self.config.instrument_id,
        #     order_side=OrderSide.SELL,
        #     quantity=self.instrument.make_qty(self.config.trade_size),
        #     time_in_force=TimeInForce.IOC,
        #     price=self.instrument.make_price(last_bar.low - (self.tick_size * 2)),
        #     trigger_price=self.instrument.make_price(last_bar.low - (self.tick_size * 2)),
        # )

        self.entry = order
        self.submit_order(order)

    def trailing_stop_buy(self, position_id: PositionId) -> None:
        """Users simple trailing stop BUY for (``SHORT`` positions)."""
        if not self.instrument:
            self.log.error("No instrument loaded")
            return

        offset = self.atr.value * self.config.trailing_atr_multiple
        order: TrailingStopMarketOrder = self.order_factory.trailing_stop_market(
            instrument_id=self.config.instrument_id,
            order_side=OrderSide.BUY,
            quantity=self.instrument.make_qty(self.config.trade_size),
            trailing_offset=Decimal(f"{offset:.{self.instrument.price_precision}f}"),
            trailing_offset_type=TrailingOffsetType[self.config.trailing_offset_type],
            trigger_type=TriggerType[self.config.trigger_type],
            reduce_only=True,
            emulation_trigger=TriggerType[self.config.emulation_trigger],
        )

        self.trailing_stop = order
        self.submit_order(order, position_id=position_id)

    def trailing_stop_sell(self, position_id: PositionId) -> None:
        """Users simple trailing stop SELL for (LONG positions)."""
        if not self.instrument:
            self.log.error("No instrument loaded")
            return

        offset = self.atr.value * self.config.trailing_atr_multiple
        order: TrailingStopMarketOrder = self.order_factory.trailing_stop_market(
            instrument_id=self.config.instrument_id,
            order_side=OrderSide.SELL,
            quantity=self.instrument.make_qty(self.config.trade_size),
            trailing_offset=Decimal(f"{offset:.{self.instrument.price_precision}f}"),
            trailing_offset_type=TrailingOffsetType[self.config.trailing_offset_type],
            trigger_type=TriggerType[self.config.trigger_type],
            reduce_only=True,
            emulation_trigger=TriggerType[self.config.emulation_trigger],
        )

        self.trailing_stop = order
        self.submit_order(order, position_id=position_id)

    def on_data(self, data: Data) -> None:
        """Actions to be performed when the strategy is running and receives data.

        Parameters
        ----------
        data : Data
            The data received.

        """

    def on_event(self, event: Event) -> None:
        """Actions to be performed when the strategy is running and receives an event.

        Parameters
        ----------
        event : Event
            The event received.

        """
        if isinstance(event, OrderFilled):
            if self.entry and event.client_order_id == self.entry.client_order_id:
                if event.order_side == OrderSide.BUY:
                    self.trailing_stop_sell(event.position_id)
                elif event.order_side == OrderSide.SELL:
                    self.trailing_stop_buy(event.position_id)
            if self.trailing_stop and event.client_order_id == self.trailing_stop.client_order_id:
                self.trailing_stop = None

    def on_stop(self) -> None:
        """Actions to be performed when the strategy is stopped."""
        self.cancel_all_orders(self.config.instrument_id)
        self.close_all_positions(self.config.instrument_id)

        # Unsubscribe from data
        self.unsubscribe_bars(self.config.bar_type)
        self.unsubscribe_quote_ticks(self.config.instrument_id)
        self.unsubscribe_trade_ticks(self.config.instrument_id)

    def on_reset(self) -> None:
        """Actions to be performed when the strategy is reset."""
        # Reset indicators here
        self.fast_ema.reset()
        self.slow_ema.reset()
        self.atr.reset()

    def on_save(self) -> dict[str, bytes]:
        """Actions to be performed when the strategy is saved.

        Create and return a state dictionary of values to be saved.

        Returns:
        -------
        dict[str, bytes]
            The strategy state dictionary.

        """
        return {}

    def on_load(self, state: dict[str, bytes]) -> None:
        """Actions to be performed when the strategy is loaded.

        Saved state values will be contained in the give state dictionary.

        Parameters
        ----------
        state : dict[str, bytes]
            The strategy state dictionary.

        """

    def on_dispose(self) -> None:
        """Actions to be performed when the strategy is disposed.

        Cleanup any resources used by the strategy here.

        """
