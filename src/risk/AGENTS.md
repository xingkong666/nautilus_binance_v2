<!-- Parent: ../AGENTS.md -->
<!-- Generated: 2026-04-01 | Updated: 2026-04-01 -->

# risk

## Purpose
Three-layer independent risk management system. Any single layer can halt trading without coordination from the others. Layers are fully decoupled from strategy and execution via the `EventBus` — they never call exchange APIs directly.

```
PreTradeRisk    — gate checks before any order leaves the system
RealTimeRisk    — continuous per-bar/tick monitoring (drawdown, daily loss)
CircuitBreaker  — escalating action: alert_only → reduce_only → halt_all
DrawdownControl — dynamic position-size reducer triggered by equity drawdown
PostTradeRisk   — slippage and PnL attribution after fills
```

## Key Files

| File | Description |
|------|-------------|
| `pre_trade.py` | `PreTradeRiskManager` — synchronous gate called by `SignalProcessor` before `OrderRouter`. Returns `PreTradeCheckResult(passed, reason)`. Checks: single-order size limit, total position size, max leverage, minimum order interval, max open order count |
| `real_time.py` | `RealTimeRiskMonitor` — called on every bar/tick update. Monitors max drawdown (`max_drawdown_pct`), daily loss limit (`daily_loss_limit_usd`), and trailing drawdown. Publishes `RiskAlertEvent` and triggers `CircuitBreaker` when thresholds are breached. Optionally persists metrics to Redis key `nautilus:risk:metrics` |
| `circuit_breaker.py` | `CircuitBreaker` — evaluates `CircuitBreakerTrigger` rules; sets `CircuitBreakerState` (stored in Redis key `nautilus:cb:state`). **Actions**: `halt_all` (block all new orders), `reduce_only` (close-only mode), `alert_only` (log + notify, no order block). **Trigger types**: `daily_loss`, `drawdown`, `rapid_loss`. Supports `cooldown_minutes` auto-reset |
| `drawdown_control.py` | `DrawdownController` — tracks peak equity and current equity; computes drawdown percentage; returns a `reduce_factor` (0–1) applied to position sizing when `warning_pct` or `critical_pct` thresholds are exceeded |
| `position_sizer.py` | Kelly / fixed-fraction / volatility-scaled position sizing helpers consumed by `PortfolioAllocator` and `PreTradeRiskManager` |
| `post_trade.py` | `PostTradeRiskAnalyzer` — invoked by `FillHandler` after each fill; computes realized slippage vs. arrival price and PnL attribution; publishes results to `EventBus` |

## For AI Agents

### Working In This Directory

- **All three active layers (`PreTradeRisk`, `RealTimeRisk`, `CircuitBreaker`) are independently sufficient to halt trading** — do not assume they share state.
- `CircuitBreakerState` is stored in Redis (`nautilus:cb:state`) so that state survives process restarts and is visible across any future worker processes.
- `RealTimeRiskMonitor` also uses Redis (`nautilus:risk:metrics`) as a fallback metrics store; it degrades gracefully to in-memory when Redis is unavailable.
- `PreTradeRiskManager.check(intent)` must be called **before** `OrderRouter.route(intent)`. The `SignalProcessor` orchestrates this — do not call `OrderRouter` directly from strategy code.
- `DrawdownController` does **not** publish events — it returns a scalar factor. The caller (`PortfolioAllocator` or `PreTradeRiskManager`) is responsible for applying it to quantity.
- Cooldown auto-reset in `CircuitBreaker`: once `cooldown_until_ns` is passed, `is_triggered` resets to `False` on the next `check()` call.
- All monetary thresholds use `Decimal`; percentages use `float`. Never mix types in comparisons.
- Use `from src.core.constants import CB_HALT_ALL` (and `CB_REDUCE_ONLY`, `CB_ALERT_ONLY`) for action string constants — do not hardcode strings.

### Testing Requirements

- `PreTradeRiskManager`: test each check rule in isolation (size, leverage, interval, open-order count). Use `PreTradeCheckResult.passed == False` assertions with expected `reason` strings.
- `CircuitBreaker`: test all three actions; test cooldown expiry (mock `time.time_ns()`); test Redis persistence path and in-memory fallback.
- `RealTimeRiskMonitor`: mock equity updates to cross `max_drawdown_pct` and `daily_loss_limit_usd`; assert `RiskAlertEvent` is published and `CircuitBreaker.trigger()` is called.
- `DrawdownController`: feed equity sequence with known peak; assert `get_reduce_factor()` returns `reduce_factor` at `critical_pct` and `1.0` below `warning_pct`.

### Common Patterns

```python
# Pre-trade gate (called inside SignalProcessor)
result = pre_trade_risk.check(intent)
if not result.passed:
    logger.warning("pre_trade_rejected", reason=result.reason)
    return

# Real-time equity update (called in strategy.on_bar())
real_time_monitor.update(equity=current_equity, unrealized_pnl=upnl)

# Circuit breaker manual trigger
circuit_breaker.trigger(
    trigger_type="daily_loss",
    current_value=-6500.0,
    threshold=-5000.0,
)

# Drawdown-adjusted quantity
factor = drawdown_controller.get_reduce_factor()
adjusted_qty = base_qty * Decimal(str(factor))
```

## Dependencies

### Internal
- `src.core.events` — `EventBus`, `RiskAlertEvent`, `OrderIntentEvent`
- `src.core.constants` — `CB_HALT_ALL`, `CB_REDUCE_ONLY`, `CB_ALERT_ONLY`
- `src.cache.redis_client` — `RedisClient` (optional; used by `CircuitBreaker` and `RealTimeRiskMonitor`)

### External
- `structlog` — structured logging
- `decimal` — all monetary calculations
- `time` — nanosecond timestamps via `time.time_ns()`

<!-- MANUAL: -->
