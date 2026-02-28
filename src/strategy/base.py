"""策略基类.

所有策略继承此基类. 策略只负责产出信号, 不直接下单.
信号通过 EventBus 发送给执行引擎.
"""

from __future__ import annotations

from abc import abstractmethod
from decimal import Decimal
from typing import Optional

from nautilus_trader.common.enums import LogColor
from nautilus_trader.config import StrategyConfig
from nautilus_trader.model.data import Bar, BarType
from nautilus_trader.model.enums import OrderSide, TimeInForce, TriggerType
from nautilus_trader.model.events import PositionChanged, PositionClosed, PositionOpened
from nautilus_trader.model.identifiers import ClientOrderId, InstrumentId
from nautilus_trader.model.instruments import Instrument
from nautilus_trader.model.objects import Price, Quantity
from nautilus_trader.model.position import Position
from nautilus_trader.trading.strategy import Strategy

from nautilus_trader.indicators import AverageTrueRange

from src.core.events import EventBus, EventType, SignalDirection, SignalEvent


class BaseStrategyConfig(StrategyConfig, frozen=True):
    """策略基础配置.

    Attributes:
        instrument_id: 交易对标识。
        bar_type: K 线类型。
        close_positions_on_stop: 策略停止时是否平仓，默认 True。
        trade_size: 每次下单数量（币数），默认 0.01。
        stop_loss_pct: 止损百分比（如 0.02 = 2%）；None 表示不设止损。
        take_profit_pct: 止盈百分比（如 0.04 = 4%）；None 表示不设止盈。
        atr_period: 计算 ATR 所用的周期长度（如果采用 ATR 止盈止损），默认 14。
        atr_sl_multiplier: ATR 止损乘数，例如 2.0 表示止损为 2 * ATR；None 表示不启用基于 ATR 的止损。
        atr_tp_multiplier: ATR 止盈乘数，例如 3.0 表示止盈为 3 * ATR；None 表示不启用基于 ATR 的止盈。
    """

    instrument_id: InstrumentId
    bar_type: BarType
    close_positions_on_stop: bool = True
    trade_size: Decimal = Decimal("0.01")
    stop_loss_pct: Optional[float] = None
    take_profit_pct: Optional[float] = None
    atr_period: int = 14
    atr_sl_multiplier: Optional[float] = None
    atr_tp_multiplier: Optional[float] = None


class BaseStrategy(Strategy):
    """策略基类.

    子类实现 generate_signal() 返回信号方向.
    基类负责:
    - 指标注册（包含自动注册 ATR 如果启用了基于 ATR 的 TP/SL）
    - Bar 订阅
    - 信号发布到 EventBus
    - 止损/止盈挂单管理（支持百分比模式与 ATR 乘数模式）
    """

    def __init__(self, config: BaseStrategyConfig, event_bus: EventBus | None = None) -> None:
        super().__init__(config)
        self.instrument: Instrument | None = None
        self._event_bus = event_bus

        # bracket 订单跟踪：position_id -> (sl_order_id, tp_order_id)
        self._sl_orders: dict[str, ClientOrderId] = {}
        self._tp_orders: dict[str, ClientOrderId] = {}

        self._atr_indicator: Optional[AverageTrueRange] = None
        if config.atr_sl_multiplier is not None or config.atr_tp_multiplier is not None:
             self._atr_indicator = AverageTrueRange(config.atr_period)

    def on_start(self) -> None:
        """策略启动."""
        self.instrument = self.cache.instrument(self.config.instrument_id)
        if self.instrument is None:
            self.log.error(f"Could not find instrument for {self.config.instrument_id}")
            self.stop()
            return

        if self._atr_indicator is not None:
            self.register_indicator_for_bars(self.config.bar_type, self._atr_indicator)

        self._register_indicators()
        self.subscribe_bars(self.config.bar_type)

    @abstractmethod
    def _register_indicators(self) -> None:
        """注册指标 (子类实现)."""

    @abstractmethod
    def generate_signal(self, bar: Bar) -> SignalDirection | None:
        """生成交易信号 (子类实现).

        Args:
            bar: 当前 Bar。

        Returns:
            信号方向，如果不产生信号返回 None。
        """

    def on_bar(self, bar: Bar) -> None:
        """接收 Bar，生成信号."""
        self.log.info(repr(bar), LogColor.CYAN)

        if not self.indicators_initialized():
            self.log.info(
                f"Waiting for indicators to warm up [{self.cache.bar_count(self.config.bar_type)}]",
                color=LogColor.BLUE,
            )
            return

        if bar.is_single_price():
            return

        direction = self.generate_signal(bar)
        if direction is not None:
            self._publish_signal(direction, bar)

    def _publish_signal(self, direction: SignalDirection, bar: Bar) -> None:
        """发布信号事件，或在无 EventBus 时直接提交市价单（回测本地模式）.

        实盘：信号发布到 EventBus，由执行引擎消费并下单。
        回测：无 EventBus 时，策略直接调用 submit_order 完成下单闭环。

        Args:
            direction: 信号方向。
            bar: 当前 Bar。
        """
        self.log.info(
            f"Signal: {direction.value} @ {bar.close}",
            color=LogColor.GREEN,
        )

        if self._event_bus:
            signal = SignalEvent(
                source=self.__class__.__name__,
                instrument_id=str(self.config.instrument_id),
                direction=direction,
                strength=1.0,
                metadata={
                    "bar_close": str(bar.close),
                    "bar_type": str(self.config.bar_type),
                },
            )
            self._event_bus.publish(signal)
        else:
            self._submit_market_order(direction)

    def _submit_market_order(self, direction: SignalDirection) -> None:
        """回测本地模式：先平仓再开市价单.

        LONG  → 平空 + 开多；SHORT → 平多 + 开空；FLAT → 平所有仓位。

        Args:
            direction: 信号方向。
        """
        if self.instrument is None:
            return

        instrument_id = self.config.instrument_id
        qty = self.instrument.make_qty(self.config.trade_size)

        # 平当前所有仓位（会触发 on_position_closed，自动取消 bracket 单）
        self.close_all_positions(instrument_id)

        if direction == SignalDirection.FLAT:
            return

        side = OrderSide.BUY if direction == SignalDirection.LONG else OrderSide.SELL
        order = self.order_factory.market(
            instrument_id=instrument_id,
            order_side=side,
            quantity=qty,
            time_in_force=TimeInForce.GTC,
        )
        self.submit_order(order)
        self.log.info(
            f"Order submitted: {side.name} {qty} {instrument_id}",
            color=LogColor.BLUE,
        )

    # ------------------------------------------------------------------
    # Bracket 止损/���盈管理
    # ------------------------------------------------------------------

    def on_position_opened(self, event: PositionOpened) -> None:
        """仓位开启时挂止损/止盈单.

        Args:
            event: PositionOpened 事件。
        """
        position = self.cache.position(event.position_id)
        if position is None:
            return
        self._place_bracket_orders(position)

    def on_position_changed(self, event: PositionChanged) -> None:
        """仓位变化时（加仓/减仓）重新挂 bracket 单.

        先取消旧的，再按新仓位均价重新挂。

        Args:
            event: PositionChanged 事件。
        """
        pos_id_str = str(event.position_id)
        self._cancel_bracket_orders(pos_id_str)

        position = self.cache.position(event.position_id)
        if position is None or position.is_closed:
            return
        self._place_bracket_orders(position)

    def on_position_closed(self, event: PositionClosed) -> None:
        """仓位关闭时取消残留 bracket 单.

        Args:
            event: PositionClosed 事件。
        """
        self._cancel_bracket_orders(str(event.position_id))

    def _place_bracket_orders(self, position: Position) -> None:
        """根据当前仓位挂止损/止盈单.

        支持 固定百分比 pct 或 当下 ATR 乘数模式计算目标价。优先评估 ATR（如果开启），未开启 ATR 时再评估 pct。

        Args:
            position: 当前持仓对象。
        """
        cfg = self.config
        
        # 如果未设置任何止损止盈参数，则退出
        if (cfg.stop_loss_pct is None and cfg.take_profit_pct is None and 
            cfg.atr_sl_multiplier is None and cfg.atr_tp_multiplier is None):
            return
            
        if self.instrument is None:
            return

        instrument_id = cfg.instrument_id
        pos_id_str = str(position.id)

        avg_px = float(position.avg_px_open)
        is_long = position.is_long
        qty = position.quantity
        
        current_atr = None
        if self._atr_indicator is not None and self._atr_indicator.initialized:
            current_atr = self._atr_indicator.value

        # 平仓方向与持仓方向相反
        close_side = OrderSide.SELL if is_long else OrderSide.BUY

        # --- 计算止损价 ---
        sl_price = None
        if cfg.atr_sl_multiplier is not None and current_atr is not None:
             sl_distance = current_atr * cfg.atr_sl_multiplier
             sl_price = avg_px - sl_distance if is_long else avg_px + sl_distance
        elif cfg.stop_loss_pct is not None:
             sl_price = avg_px * (1.0 - cfg.stop_loss_pct) if is_long else avg_px * (1.0 + cfg.stop_loss_pct)

        # 发送止损单（StopMarket）
        if sl_price is not None:
            sl_price_obj = self.instrument.make_price(sl_price)
            sl_order = self.order_factory.stop_market(
                instrument_id=instrument_id,
                order_side=close_side,
                quantity=qty,
                trigger_price=sl_price_obj,
                trigger_type=TriggerType.DEFAULT,
                time_in_force=TimeInForce.GTC,
                reduce_only=True,
            )
            self._sl_orders[pos_id_str] = sl_order.client_order_id
            self.submit_order(sl_order)
            self.log.info(
                f"SL placed: {close_side.name} {qty} @ stop {sl_price_obj} (pos={pos_id_str})",
                color=LogColor.YELLOW,
            )

        # --- 计算止盈价 ---
        tp_price = None
        if cfg.atr_tp_multiplier is not None and current_atr is not None:
             tp_distance = current_atr * cfg.atr_tp_multiplier
             tp_price = avg_px + tp_distance if is_long else avg_px - tp_distance
        elif cfg.take_profit_pct is not None:
             tp_price = avg_px * (1.0 + cfg.take_profit_pct) if is_long else avg_px * (1.0 - cfg.take_profit_pct)

        # 发送止盈单（Limit）
        if tp_price is not None:
            tp_price_obj = self.instrument.make_price(tp_price)
            tp_order = self.order_factory.limit(
                instrument_id=instrument_id,
                order_side=close_side,
                quantity=qty,
                price=tp_price_obj,
                time_in_force=TimeInForce.GTC,
                reduce_only=True,
                post_only=False,
            )
            self._tp_orders[pos_id_str] = tp_order.client_order_id
            self.submit_order(tp_order)
            self.log.info(
                f"TP placed: {close_side.name} {qty} @ limit {tp_price_obj} (pos={pos_id_str})",
                color=LogColor.YELLOW,
            )

    def _cancel_bracket_orders(self, pos_id_str: str) -> None:
        """取消指定仓位的 bracket 单（如果还在挂单状态）.

        Args:
            pos_id_str: 仓位 ID 字符串。
        """
        instrument_id = self.config.instrument_id

        for order_map in (self._sl_orders, self._tp_orders):
            coid = order_map.pop(pos_id_str, None)
            if coid is None:
                continue
            order = self.cache.order(coid)
            if order is not None and order.is_open:
                self.cancel_order(order)
                self.log.info(
                    f"Bracket order cancelled: {coid} (pos={pos_id_str})",
                    color=LogColor.YELLOW,
                )

    # ------------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------------

    def on_stop(self) -> None:
        """策略停止：取消所有挂单，按配置决定是否平仓."""
        instrument_id = self.config.instrument_id
        self.cancel_all_orders(instrument_id)
        self._sl_orders.clear()
        self._tp_orders.clear()
        if self.config.close_positions_on_stop:
            self.close_all_positions(instrument_id)
        self.unsubscribe_bars(self.config.bar_type)

    def on_reset(self) -> None:
        """策略重置（子类覆盖以重置指标）."""
        self._sl_orders.clear()
        self._tp_orders.clear()

    def on_save(self) -> dict[str, bytes]:
        return {}

    def on_load(self, state: dict[str, bytes]) -> None:
        pass

    def on_dispose(self) -> None:
        pass
