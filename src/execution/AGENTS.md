<!-- Parent: ../AGENTS.md -->
<!-- Generated: 2026-04-01 | Updated: 2026-04-01 -->

# execution

## Purpose
Translates strategy signals into exchange orders. This layer owns the complete pipeline from `SignalEvent` reception through quantity calculation, pre-trade risk gating, order routing, algorithmic execution (TWAP/iceberg), rate limiting, and post-fill bookkeeping. It is intentionally stateless with respect to strategy logic — all signal intent arrives via the `EventBus`.

## Key Files

| File | Description |
|------|-------------|
| `signal_processor.py` | Subscribes to `EventType.SIGNAL` on the `EventBus`; converts each `SignalEvent` into an `OrderIntent` (via quantity lookup and direction mapping); checks rate limits and ignored-instruments before handing off to `OrderRouter` |
| `order_intent.py` | Frozen `@dataclass` `OrderIntent` — the canonical DTO between strategy, risk, and execution. Fields: `instrument_id`, `side` (BUY/SELL), `quantity`, `order_type`, `price`, `stop_loss`, `take_profit`, `time_in_force`, `reduce_only`, `strategy_id`, `metadata`. Factory: `OrderIntent.from_signal()` |
| `order_router.py` | Receives a vetted `OrderIntent`; rounds quantity to exchange lot-size; calls `strategy.submit_order()` (skipped when `submit_orders=False`); publishes `OrderIntentEvent` and `RiskAlertEvent` on failure. Supports multi-strategy binding via `bind_strategy()` / `bind_strategies()` |
| `fill_handler.py` | Handles `OrderFilled` events: persists trade records via `TradePersistence`, updates position state, triggers post-trade risk, publishes fill events back onto the `EventBus` |
| `rate_limiter.py` | Sliding-window rate limiter. Uses Redis ZSET + Lua atomic script when `RedisClient` is available; falls back to local `deque` for single-process isolation. Redis keys: `nautilus:rl:orders:second`, `nautilus:rl:orders:minute` |
| `algo.py` | Abstract `ExecAlgorithm` base class with `split(intent) -> list[OrderIntent]`. Concrete: `TWAPAlgorithm` (uniform slice over time window), `IcebergAlgorithm` (stub). Plug into `SignalProcessor` or `OrderRouter` before submission |
| `cost_model.py` | Fee and cost estimation utilities for pre-trade P&L projection |
| `slippage.py` | Slippage estimation models used by post-trade attribution |
| `ignored_instruments.py` | `IgnoredInstrumentRegistry` — runtime blocklist of instrument IDs that `SignalProcessor` refuses to route |

## For AI Agents

### Working In This Directory

- **Pipeline order is strict**: `SignalEvent → SignalProcessor → OrderIntent → PreTradeRisk.check() → OrderRouter → strategy.submit_order()`. Never bypass steps.
- `OrderIntent` is **frozen** — create a new instance rather than mutating fields.
- `OrderRouter` has a `submit_orders` flag (default `True`). Set `False` in tests/paper-trading to run the full validation path without hitting the exchange.
- `RateLimiter` is optional in `SignalProcessor.__init__`. When present, `allow()` must return `True` before `OrderRouter` is called.
- `AlgoExecution` (`TWAPAlgorithm`) produces a list of child `OrderIntent`s — each child must pass through `OrderRouter` independently.
- All logging uses `structlog` with keyword-argument context: `logger.info("order_routed", instrument_id=..., qty=...)`.
- Imports must use `from src.execution.xxx` (hatchling editable install; no relative imports).

### Testing Requirements

- Unit-test `SignalProcessor` by injecting a mock `EventBus` and asserting `OrderRouter.route()` is called with correct `OrderIntent`.
- Use `submit_orders=False` on `OrderRouter` in all unit/integration tests to avoid live order submission.
- `RateLimiter` tests must cover both the Redis path and the local-deque fallback.
- `TWAPAlgorithm.split()` tests: verify child quantities sum to parent quantity and each child carries correct `strategy_id`.
- `FillHandler` tests: mock `TradePersistence` and assert `on_fill()` persists and publishes the fill event.

### Common Patterns

```python
# Constructing an OrderIntent from a signal
from src.execution.order_intent import OrderIntent
from src.core.events import SignalDirection

intent = OrderIntent.from_signal(
    instrument_id="BTCUSDT-PERP.BINANCE",
    direction=SignalDirection.LONG,
    quantity=Decimal("0.01"),
    strategy_id="ema_cross",
)

# Binding a strategy to OrderRouter
router = OrderRouter(event_bus=bus, submit_orders=False)
router.bind_strategy(strategy)

# TWAP split
algo = TWAPAlgorithm(slices=5, interval_seconds=60)
child_intents = algo.split(intent)
```

## Dependencies

### Internal
- `src.core.events` — `EventBus`, `EventType`, `SignalEvent`, `OrderIntentEvent`, `RiskAlertEvent`, `SignalDirection`
- `src.risk.pre_trade` — `PreTradeRiskManager` (injected into `SignalProcessor`)
- `src.state.persistence` — `TradePersistence` (injected into `FillHandler`)
- `src.cache.redis_client` — `RedisClient` (optional, for `RateLimiter`)

### External
- `nautilus_trader` — `OrderSide`, `TimeInForce`, `InstrumentId`, `Instrument`, `Strategy`
- `structlog` — structured logging
- `decimal` — all price/quantity arithmetic uses `Decimal`

<!-- MANUAL: -->
