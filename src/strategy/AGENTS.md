<!-- Parent: ../AGENTS.md -->
<!-- Generated: 2026-04-01 | Updated: 2026-04-01 -->

# strategy

## Purpose

Contains the strategy base class and all concrete trading strategies. **Strategies are pure signal generators** — their only output is a `SignalDirection` returned from `generate_signal()`. The base class translates that into a `SignalEvent` on the `EventBus` (live mode) or a direct `submit_order` call (backtest-local mode, no EventBus). No strategy may call exchange APIs, access `OrderRouter`, or interact with risk/execution layers directly.

## Key Files

| File | Description |
|------|-------------|
| `base.py` | `BaseStrategyConfig` (Pydantic frozen `StrategyConfig`) and `BaseStrategy` (abstract NautilusTrader `Strategy` subclass); handles indicator registration, bar subscription, ATR bracket SL/TP order management, three sizing modes, live warmup history requests, and position lifecycle callbacks |
| `signal.py` | `TradeSignal` frozen dataclass — richer signal format with `suggested_size`, `stop_loss`, `take_profit`, `metadata`; `is_entry` / `is_exit` properties |
| `ema_cross.py` | `EMACrossStrategy` / `EMACrossConfig` — classic dual-EMA crossover; golden cross → LONG, death cross → SHORT |
| `ema_pullback_atr.py` | `EMAPullbackATRStrategy` / `EMAPullbackATRConfig` — EMA trend + ATR-measured pullback entry filter + optional ADX trend-strength gate |
| `micro_scalp.py` | `MicroScalpStrategy` / `MicroScalpConfig` — high-frequency EMA/RSI/ADX confluence scalper with limit-order entry, maker offset ticks, limit TTL, and chase logic |
| `rsi_strategy.py` | `RSIStrategy` / `RSIConfig` — mean-reversion strategy driven by RSI oversold/overbought levels |
| `turtle.py` | `TurtleStrategy` / `TurtleConfig` — Donchian-channel breakout trend-following with multi-unit pyramiding (up to `max_units`) and ATR-based N-unit stop |
| `vegas_tunnel.py` | `VegasTunnelStrategy` / `VegasTunnelConfig` — four-EMA tunnel system (fast/slow signal EMAs + two tunnel EMAs at periods 144/169) with Fibonacci TP split across three targets |

## For AI Agents

### Working In This Directory

**The two abstract methods every strategy must implement:**

```python
def _register_indicators(self) -> None:
    """Register NautilusTrader indicators with register_indicator_for_bars()."""

def generate_signal(self, bar: Bar) -> SignalDirection | None:
    """Return LONG, SHORT, FLAT, or None (no signal this bar)."""
```

**Base class responsibilities (do NOT re-implement in subclasses):**
- `on_start()` — resolves instrument, registers indicators, requests warmup history, subscribes to bars.
- `on_bar()` — guards on `indicators_initialized()` and `bar.is_single_price()`, then calls `generate_signal()`.
- `_publish_signal()` — publishes `SignalEvent` to EventBus (live) or calls `_submit_market_order()` (backtest).
- `on_position_opened/changed/closed()` — ATR/pct bracket SL/TP order lifecycle management.
- `on_stop()` — cancels all orders, optionally flattens positions (`close_positions_on_stop`).

**Sizing modes** (evaluated in priority order; first non-zero result wins):
1. `margin_pct_per_trade` — margin as % of equity × `sizing_leverage` → notional → qty
2. `gross_exposure_pct_per_trade` — notional as % of equity → qty
3. `capital_pct_per_trade` — alias for gross exposure (legacy name)
4. `trade_size` (Decimal) — fixed coin quantity fallback

**ATR bracket orders** (`atr_sl_multiplier` / `atr_tp_multiplier` on config):
- Base class auto-registers an `AverageTrueRange(atr_period)` indicator when either multiplier is set.
- SL is a `stop_market` order (`reduce_only=True`); TP is a `limit` order (`reduce_only=True`).
- Both are re-placed on every `on_position_changed` event (re-centres on new avg entry price).

**Live warmup** (`live_warmup_bars`, `live_warmup_margin_bars` on config):
- `_resolved_warmup_bars()` returns `live_warmup_bars` if > 0, else `_history_warmup_bars()` (subclass override).
- `preload_history(bars)` can be called externally (e.g., by `preload_strategies_warmup`) to feed historical bars without emitting signals.

**Adding a new strategy:**
1. Create `src/strategy/my_strategy.py` with `MyStrategyConfig(BaseStrategyConfig, frozen=True)` and `MyStrategy(BaseStrategy)`.
2. Implement `_register_indicators()` and `generate_signal(bar) -> SignalDirection | None`.
3. Register in `bootstrap.py` `_STRATEGY_REGISTRY` and add a factory method to `AppFactory`.
4. Add YAML config to `configs/strategies/my_strategy.yaml`.

### Testing Requirements

- Instantiate strategies with a mock `EventBus` or `event_bus=None` (backtest mode).
- Use `NautilusTrader` test harness (`BacktestEngine` or `MockActor`) to feed bars and assert signals.
- Test sizing modes by mocking `strategy.portfolio.account()` to return a known equity balance.
- ATR bracket tests: assert `_sl_orders` and `_tp_orders` dicts are populated after `on_position_opened`.
- Test `on_stop()` asserts `close_all_positions` is called when `close_positions_on_stop=True`.

### Common Patterns

```python
# Minimal concrete strategy
class MyStrategy(BaseStrategy):
    def _register_indicators(self) -> None:
        self._ema = ExponentialMovingAverage(20)
        self.register_indicator_for_bars(self.config.bar_type, self._ema)

    def generate_signal(self, bar: Bar) -> SignalDirection | None:
        if bar.close > self._ema.value:
            return SignalDirection.LONG
        if bar.close < self._ema.value:
            return SignalDirection.SHORT
        return None

# Config with ATR brackets and margin sizing
config = MyStrategyConfig(
    instrument_id=InstrumentId.from_str("BTCUSDT-PERP.BINANCE"),
    bar_type=BarType.from_str("BTCUSDT-PERP.BINANCE-1-MINUTE-LAST-EXTERNAL"),
    margin_pct_per_trade=2.0,
    sizing_leverage=10.0,
    atr_sl_multiplier=1.5,
    atr_tp_multiplier=3.0,
    atr_period=14,
)
```

## Dependencies

### Internal
- `src.core.events` — `EventBus`, `SignalDirection`, `SignalEvent`

### External
- `nautilus_trader.trading.strategy` — `Strategy` base class
- `nautilus_trader.config` — `StrategyConfig`
- `nautilus_trader.indicators` — `AverageTrueRange`, EMA, RSI, ADX, Donchian, etc.
- `nautilus_trader.model.*` — `Bar`, `BarType`, `InstrumentId`, `OrderSide`, `Quantity`, `Position`
- `pydantic` — frozen config model validation

<!-- MANUAL: -->
