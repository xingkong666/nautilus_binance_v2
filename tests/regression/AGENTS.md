<!-- Parent: ../AGENTS.md -->
<!-- Generated: 2026-04-01 | Updated: 2026-04-01 -->

# tests/regression/

## Purpose
Baseline-locked regression tests that run each production strategy against deterministic synthetic data inside an in-memory `BacktestEngine` and compare key metrics (order count, position count, fill count, PnL) to hardcoded `BASELINE` dictionaries. Any code change that alters strategy behaviour will cause a regression failure, serving as an early warning before the change reaches production.

## Key Files

| File | Description |
|------|-------------|
| `conftest.py` | Shared fixtures: in-memory `BacktestEngine` builder, `make_sine_bars()` / `make_trend_bars()` synthetic data generators, `BAR_TYPE` constant, `run_ema_cross()` helper |
| `test_ema_cross_baseline.py` | EMA cross strategy (fast=5, slow=20) on 200-bar sine-wave data; baseline: 12 orders, 6 positions |
| `test_rsi_baseline.py` | RSI mean-reversion strategy baseline lock |
| `test_turtle_baseline.py` | Turtle breakout strategy baseline lock |
| `test_vegas_tunnel_baseline.py` | Vegas Tunnel (EMA 144/169 tunnel) strategy baseline lock |

## For AI Agents

### Working In This Directory
- Run: `uv run pytest tests/regression/ -v`
- These tests are **read-only contracts**—do not tighten or loosen thresholds without cause.
- To regenerate a baseline after a confirmed intentional change: run the test with a temporary `print(actual_metrics)` statement, confirm the values make sense, then update the `BASELINE` dict and write a clear commit message explaining why.

### Testing Requirements
- **Never tighten baselines** (e.g., raising expected order count) without re-running on fresh synthetic data and explicit team review.
- Synthetic data is generated deterministically (fixed seed / deterministic math); do not introduce randomness.
- Tests must not touch the real `data/` catalog or any network resource.
- The `conftest.py` `BacktestEngine` uses `AccountType.MARGIN`, `OmsType.HEDGING`, `Venue("BINANCE")`, and USDT starting balance—keep these consistent across all baseline tests.

### Common Patterns
```python
BASELINE = {
    "orders": 12,
    "positions": 6,
    "fills": 12,
}

def test_ema_cross_order_count(backtest_engine):
    bars = make_sine_bars(BAR_TYPE, n=200)
    result = run_ema_cross(backtest_engine, bars, fast=5, slow=20)
    assert result.order_count == BASELINE["orders"]
    assert result.position_count == BASELINE["positions"]
```
- Use `conftest.py` helpers (`make_sine_bars`, `make_trend_bars`) rather than loading from disk.
- Assert on integer counts (orders, fills, positions) rather than floating-point PnL for maximum stability.

## Dependencies

### Internal
- `src.strategy.ema_cross.EMACrossStrategy`, `EMACrossConfig`
- `src.strategy.rsi_strategy.RSIStrategy`, `RSIStrategyConfig`
- `src.strategy.turtle_strategy.TurtleStrategy`
- `src.strategy.vegas_tunnel.VegasTunnelStrategy`

### External
- `pytest`, `pytest-asyncio`
- `nautilus_trader.backtest.engine.BacktestEngine`
- `nautilus_trader.test_kit.providers.TestInstrumentProvider`
- `nautilus_trader.model.*` (Bar, BarType, Price, Quantity, Money)

<!-- MANUAL: -->
