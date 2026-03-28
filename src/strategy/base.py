"""策略基类.

所有策略继承此基类. 策略只负责产出信号, 不直接下单.
信号通过 EventBus 发送给执行引擎.
"""

from __future__ import annotations

import re
from abc import abstractmethod
from collections.abc import Iterable
from datetime import UTC, datetime, timedelta
from decimal import ROUND_FLOOR, Decimal

from nautilus_trader.common.enums import LogColor
from nautilus_trader.config import StrategyConfig
from nautilus_trader.indicators import AverageTrueRange
from nautilus_trader.model.data import Bar, BarType
from nautilus_trader.model.enums import OrderSide, TimeInForce, TriggerType
from nautilus_trader.model.events import PositionChanged, PositionClosed, PositionOpened
from nautilus_trader.model.identifiers import ClientOrderId, InstrumentId
from nautilus_trader.model.instruments import Instrument
from nautilus_trader.model.objects import Quantity
from nautilus_trader.model.position import Position
from nautilus_trader.trading.strategy import Strategy

from src.core.events import EventBus, SignalDirection, SignalEvent


class BaseStrategyConfig(StrategyConfig, frozen=True):
    """策略基础配置.

    Attributes:
        instrument_id: 交易对标识。
        bar_type: K 线类型。
        close_positions_on_stop: 策略停止时是否平仓，默认 True。
        trade_size: 每次下单数量（币数），默认 0.01（固定数量模式）。
        margin_pct_per_trade: 每笔使用账户权益多少百分比作为保证金，再乘 sizing_leverage 得到目标名义敞口。
        gross_exposure_pct_per_trade: 每笔目标名义敞口占账户权益百分比，可大于 100。
        capital_pct_per_trade: 每笔使用账户总权益的百分比（0-100），设置后优先于 trade_size。
        sizing_leverage: sizing 计算使用的杠杆倍数；margin_pct_per_trade 模式下生效。
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
    margin_pct_per_trade: float | None = None
    gross_exposure_pct_per_trade: float | None = None
    capital_pct_per_trade: float | None = None
    sizing_leverage: float = 1.0
    stop_loss_pct: float | None = None
    take_profit_pct: float | None = None
    atr_period: int = 14
    atr_sl_multiplier: float | None = None
    atr_tp_multiplier: float | None = None
    live_warmup_bars: int = 0
    live_warmup_margin_bars: int = 5


class BaseStrategy(Strategy):  # type: ignore[misc]
    """策略基类.

    子类实现 generate_signal() 返回信号方向.
    基类负责:
    - 指标注册（包含自动注册 ATR 如果启用了基于 ATR 的 TP/SL）
    - Bar 订阅
    - 信号发布到 EventBus
    - 止损/止盈挂单管理（支持百分比模式与 ATR 乘数模式）
    """

    def __init__(self, config: BaseStrategyConfig, event_bus: EventBus | None = None) -> None:
        """Initialize the base strategy.

        Args:
            config: Configuration values for the component.
            event_bus: Event bus used for cross-module communication.
        """
        super().__init__(config)
        self.instrument: Instrument | None = None
        self._event_bus = event_bus

        # bracket 订单跟踪：position_id -> (sl_order_id, tp_order_id)
        self._sl_orders: dict[str, ClientOrderId] = {}
        self._tp_orders: dict[str, ClientOrderId] = {}
        self._indicators_registered = False
        self._warmup_history_requested = False
        self._warmup_history_preloaded = False

        self._atr_indicator: AverageTrueRange | None = None
        if config.atr_sl_multiplier is not None or config.atr_tp_multiplier is not None:
            self._atr_indicator = AverageTrueRange(config.atr_period)

    def on_start(self) -> None:
        """策略启动."""
        self.instrument = self.cache.instrument(self.config.instrument_id)
        if self.instrument is None:
            self.log.error(f"Could not find instrument for {self.config.instrument_id}")
            self.stop()
            return

        self._ensure_indicators_registered()
        self._request_warmup_history()
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

    def _history_warmup_bars(self) -> int:
        """返回策略默认需要的历史预热 bars 数量。."""
        return 0

    def _on_historical_bar(self, bar: Bar) -> None:
        """消费历史 bar，用于构建最小运行态而不发出交易信号。."""

    def _resolved_warmup_bars(self) -> int:
        explicit = max(0, int(getattr(self.config, "live_warmup_bars", 0)))
        return explicit if explicit > 0 else max(0, self._history_warmup_bars())

    def _request_warmup_history(self) -> None:
        if self._warmup_history_preloaded:
            return
        if self._event_bus is None:
            return

        warmup_bars = self._resolved_warmup_bars()
        if warmup_bars <= 0:
            return

        bar_span = self._bar_type_interval()
        if bar_span is None:
            self.log.warning(f"Skipping warmup history: unsupported bar_type={self.config.bar_type}")
            return

        margin_bars = max(0, int(getattr(self.config, "live_warmup_margin_bars", 5)))
        request_limit = warmup_bars + margin_bars
        start = datetime.now(UTC) - (bar_span * request_limit)
        self.request_bars(
            self.config.bar_type,
            start=start,
            limit=request_limit,
        )
        self._warmup_history_requested = True
        self.log.info(
            f"Requested warmup history bars={request_limit} bar_type={self.config.bar_type}",
            color=LogColor.BLUE,
        )

    def _ensure_indicators_registered(self) -> None:
        if self._indicators_registered:
            return

        if self._atr_indicator is not None:
            self.register_indicator_for_bars(self.config.bar_type, self._atr_indicator)

        self._register_indicators()
        self._indicators_registered = True

    def preload_history(self, bars: Iterable[Bar]) -> int:
        """预加载历史 bars，用于 live 启动前预热指标和最小运行态。."""
        self._ensure_indicators_registered()

        count = 0
        for bar in bars:
            self.handle_historical_bar(bar)
            self._on_historical_bar(bar)
            count += 1

        if count > 0:
            self._warmup_history_preloaded = True
            self._warmup_history_requested = True
            self.log.info(
                "Warmup history preloaded: "
                f"strategy={self.__class__.__name__} "
                f"instrument_id={self.config.instrument_id} "
                f"bar_type={self.config.bar_type} "
                f"loaded_bars={count}",
                color=LogColor.BLUE,
            )

        return count

    def on_historical_data(self, data) -> None:
        """消费历史数据，用于预热指标和策略最小状态。."""
        for bar in self._iter_historical_bars(data):
            self._on_historical_bar(bar)

    def _iter_historical_bars(self, data) -> list[Bar]:
        if isinstance(data, Bar):
            return [data]
        if isinstance(data, Iterable) and not isinstance(data, (str, bytes, bytearray)):
            bars: list[Bar] = []
            for item in data:
                if isinstance(item, Bar):
                    bars.append(item)
            return bars
        return []

    def _bar_type_interval(self) -> timedelta | None:
        spec = str(self.config.bar_type.spec)
        match = re.match(r"(?P<count>\d+)-(?P<unit>SECOND|MINUTE|HOUR|DAY)", spec)
        if match is None:
            return None

        count = int(match.group("count"))
        unit = match.group("unit")
        if unit == "SECOND":
            return timedelta(seconds=count)
        if unit == "MINUTE":
            return timedelta(minutes=count)
        if unit == "HOUR":
            return timedelta(hours=count)
        if unit == "DAY":
            return timedelta(days=count)
        return None

    def on_bar(self, bar: Bar) -> None:
        """接收 Bar，生成信号.

        Args:
            bar: Incoming bar data for the strategy callback.
        """
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
            bar: 用于解析数量和记录价格上下文的当前 Bar。
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
            self._submit_market_order(direction, bar)

    def _submit_market_order(self, direction: SignalDirection, bar: Bar) -> None:
        """回测本地模式：先平仓再开市价单.

        LONG  → 平空 + 开多；SHORT → 平多 + 开空；FLAT → 平所有仓位。

        Args:
            direction: 信号方向。
            bar: 用于解析下单数量和记录价格上下文的当前 Bar。

        """
        if self.instrument is None:
            return

        instrument_id = self.config.instrument_id
        qty = self._resolve_order_quantity(bar)
        if qty is None:
            self.log.warning("Order skipped: resolved quantity is 0")
            return

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

    def _resolve_order_quantity(self, bar: Bar) -> Quantity | None:
        """解析下单数量（优先保证金/名义敞口 sizing，其次固定数量）.

        Args:
            bar: Incoming bar data for the strategy callback.
        """
        if self.instrument is None:
            return None

        margin_pct = self.config.margin_pct_per_trade
        if margin_pct is not None and margin_pct > 0:
            qty = self._resolve_qty_from_margin_pct(
                margin_pct=margin_pct,
                sizing_leverage=float(self.config.sizing_leverage),
                close_price=float(bar.close),
            )
            if qty is not None and qty.as_decimal() > 0:
                return qty

            self.log.warning(
                "margin_pct_per_trade sizing failed, fallback to lower-priority sizing "
                f"(margin_pct_per_trade={margin_pct}, sizing_leverage={float(self.config.sizing_leverage)})"
            )

        gross_exposure_pct = self.config.gross_exposure_pct_per_trade
        if gross_exposure_pct is not None and gross_exposure_pct > 0:
            qty = self._resolve_qty_from_notional_pct(gross_exposure_pct, float(bar.close))
            if qty is not None and qty.as_decimal() > 0:
                return qty

            self.log.warning(
                "gross_exposure_pct_per_trade sizing failed, fallback to lower-priority sizing "
                f"(gross_exposure_pct_per_trade={gross_exposure_pct})"
            )

        capital_pct = self.config.capital_pct_per_trade
        if capital_pct is not None and capital_pct > 0:
            qty = self._resolve_qty_from_notional_pct(capital_pct, float(bar.close))
            if qty is not None and qty.as_decimal() > 0:
                return qty

            self.log.warning(
                "capital_pct_per_trade sizing failed, fallback to fixed trade_size "
                f"(capital_pct_per_trade={capital_pct})"
            )

        qty = self.instrument.make_qty(self.config.trade_size)
        if qty.as_decimal() <= 0:
            return None
        return qty

    def _resolve_order_quantity_decimal(
        self,
        bar: Bar,
        fallback_trade_size: bool = True,
    ) -> Decimal | None:
        """解析下单数量并返回 Decimal，自动按最小步进对齐。.

        Args:
            bar: 当前 Bar。
            fallback_trade_size: 当主路径解析失败时是否回退到 trade_size。

        """
        qty = self._resolve_order_quantity(bar)
        if qty is not None and qty.as_decimal() > 0:
            value = self._split_quantity_by_ratios_strict_step(qty.as_decimal(), [Decimal("1")])[0]
            return value if value > 0 else None

        if not fallback_trade_size:
            return None

        value = self._split_quantity_by_ratios_strict_step(
            total_qty=Decimal(str(self.config.trade_size)),
            ratios=[Decimal("1")],
        )[0]
        return value if value > 0 else None

    def _resolve_equity(self) -> Decimal | None:
        """读取账户总权益."""
        if self.instrument is None:
            return None

        venue = getattr(self.config.instrument_id, "venue", None)
        if venue is None:
            return None

        try:
            account = self.portfolio.account(venue=venue)
        except Exception:  # noqa: BLE001
            return None
        if account is None:
            return None

        quote_ccy = getattr(self.instrument, "quote_currency", None)
        balance = account.balance_total(quote_ccy)
        if balance is None:
            balance = account.balance_total()
        if balance is None:
            return None

        equity = balance.as_decimal()
        if equity <= 0:
            return None
        return equity

    def _resolve_qty_from_notional_pct(self, notional_pct: float, close_price: float) -> Quantity | None:
        """按账户权益百分比换算目标名义敞口.

        Args:
            notional_pct: Percentage value for notional.
            close_price: Close price.
        """
        if self.instrument is None:
            return None
        if close_price <= 0:
            return None

        equity = self._resolve_equity()
        if equity is None:
            return None

        notional = equity * Decimal(str(notional_pct / 100.0))
        if notional <= 0:
            return None

        qty_decimal = notional / Decimal(str(close_price))
        try:
            qty = self.instrument.make_qty(qty_decimal)
        except ValueError:
            return None
        if qty.as_decimal() <= 0:
            return None
        return qty

    def _resolve_qty_from_capital_pct(self, capital_pct: float, close_price: float) -> Quantity | None:
        """兼容旧命名：按账户权益百分比换算目标名义敞口.

        Args:
            capital_pct: Percentage value for capital.
            close_price: Close price.
        """
        return self._resolve_qty_from_notional_pct(capital_pct, close_price)

    def _resolve_qty_from_margin_pct(
        self,
        margin_pct: float,
        sizing_leverage: float,
        close_price: float,
    ) -> Quantity | None:
        """按保证金百分比 * sizing_leverage 计算目标名义敞口.

        Args:
            margin_pct: Percentage value for margin.
            sizing_leverage: Sizing leverage.
            close_price: Close price.
        """
        if close_price <= 0:
            return None

        equity = self._resolve_equity()
        if equity is None:
            return None

        leverage = max(0.0, sizing_leverage)
        if leverage <= 0:
            return None

        margin = equity * Decimal(str(margin_pct / 100.0))
        if margin <= 0:
            return None

        notional = margin * Decimal(str(leverage))
        if notional <= 0:
            return None

        return self._resolve_qty_from_notional_pct(
            notional_pct=float((notional / equity) * Decimal("100")),
            close_price=close_price,
        )

    def _quantity_step(self) -> Decimal:
        """返回当前 instrument 的最小下单步进。."""
        if self.instrument is not None and hasattr(self.instrument, "size_increment"):
            step = Decimal(str(self.instrument.size_increment))
            if step > 0:
                return step
        return Decimal("0.00000001")

    def _normalize_ratios(self, ratios: list[Decimal]) -> list[Decimal]:
        if not ratios:
            return []

        normalized = [r if r > 0 else Decimal("0") for r in ratios]
        ratio_sum = sum(normalized, start=Decimal("0"))
        if ratio_sum <= 0:
            normalized = [Decimal("1")] * len(ratios)
            ratio_sum = Decimal(str(len(ratios)))

        return [r / ratio_sum for r in normalized]

    def _split_quantity_by_ratios_preserve_total(
        self,
        total_qty: Decimal,
        ratios: list[Decimal],
        step: Decimal | None = None,
    ) -> list[Decimal]:
        """按比例切分数量，优先保证总量守恒。.

        Args:
            total_qty: 总数量。
            ratios: 比例列表（允许任意非负值，内部自动归一化）。
            step: 最小数量步进；None 时自动读取 instrument 步进。

        Returns:
            与 ratios 同长度的切分数量，和等于 total_qty。

        """
        norm = self._normalize_ratios(ratios)
        if not norm:
            return []

        if step is None:
            step = self._quantity_step()

        if step <= 0:
            chunks = [total_qty * n for n in norm]
            chunks[-1] = total_qty - sum(chunks[:-1], start=Decimal("0"))
            return chunks

        total_steps = int((total_qty / step).to_integral_value(rounding=ROUND_FLOOR))
        if total_steps <= 0:
            zeros = [Decimal("0")] * len(ratios)
            zeros[-1] = total_qty
            return zeros

        raw_targets = [Decimal(total_steps) * n for n in norm]
        step_counts = [int(x.to_integral_value(rounding=ROUND_FLOOR)) for x in raw_targets]
        remains = total_steps - sum(step_counts)

        if remains > 0:
            frac_order = sorted(
                range(len(ratios)),
                key=lambda i: (raw_targets[i] - Decimal(step_counts[i]), -i),
                reverse=True,
            )
            for i in range(remains):
                step_counts[frac_order[i % len(ratios)]] += 1

        chunks = [step * Decimal(c) for c in step_counts]
        chunks[-1] = total_qty - sum(chunks[:-1], start=Decimal("0"))
        return chunks

    def _split_quantity_by_ratios_strict_step(
        self,
        total_qty: Decimal,
        ratios: list[Decimal],
        step: Decimal | None = None,
    ) -> list[Decimal]:
        """按比例切分数量，严格满足每一段都是 step 的整数倍。.

        返回值总和 <= total_qty；若 total_qty 不是 step 整数倍，尾差会被舍弃。

        Args:
            total_qty: Total qty.
            ratios: Ratios.
            step: Step.
        """
        norm = self._normalize_ratios(ratios)
        if not norm:
            return []

        if step is None:
            step = self._quantity_step()

        if step <= 0:
            chunks = [total_qty * n for n in norm]
            chunks[-1] = total_qty - sum(chunks[:-1], start=Decimal("0"))
            return chunks

        total_steps = int((total_qty / step).to_integral_value(rounding=ROUND_FLOOR))
        if total_steps <= 0:
            return [Decimal("0")] * len(ratios)

        raw_targets = [Decimal(total_steps) * n for n in norm]
        step_counts = [int(x.to_integral_value(rounding=ROUND_FLOOR)) for x in raw_targets]
        remains = total_steps - sum(step_counts)

        if remains > 0:
            frac_order = sorted(
                range(len(ratios)),
                key=lambda i: (raw_targets[i] - Decimal(step_counts[i]), -i),
                reverse=True,
            )
            for i in range(remains):
                step_counts[frac_order[i % len(ratios)]] += 1

        return [step * Decimal(c) for c in step_counts]

    def _split_quantity_by_ratios(
        self,
        total_qty: Decimal,
        ratios: list[Decimal],
        step: Decimal | None = None,
    ) -> list[Decimal]:
        """兼容旧调用：等价于 preserve_total 模式。.

        Args:
            total_qty: Total qty.
            ratios: Ratios.
            step: Step.
        """
        return self._split_quantity_by_ratios_preserve_total(
            total_qty=total_qty,
            ratios=ratios,
            step=step,
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
        if (
            cfg.stop_loss_pct is None
            and cfg.take_profit_pct is None
            and cfg.atr_sl_multiplier is None
            and cfg.atr_tp_multiplier is None
        ):
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
        """Run on save.

        Returns:
            dict[str, bytes]: Dictionary representation of the result.
        """
        return {}

    def on_load(self, state: dict[str, bytes]) -> None:
        """Run on load.

        Args:
            state: State.
        """
        pass

    def on_dispose(self) -> None:
        """Run on dispose."""
        pass
