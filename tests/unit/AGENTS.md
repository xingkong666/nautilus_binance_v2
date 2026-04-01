<!-- Parent: ../AGENTS.md -->
<!-- Generated: 2026-04-01 | Updated: 2026-04-01 -->

# tests/unit/

## Purpose
Fast, fully-isolated unit tests covering every major module of the trading system. All external I/O (network, database, Binance API, NautilusTrader engine internals) is replaced with `MagicMock`. Tests run in milliseconds and require no infrastructure. This is the primary safety net for logic changes and refactors.

## Key Files

| File | Description |
|------|-------------|
| `test_account_sync.py` | Account state synchronisation logic |
| `test_allocator.py` | `PortfolioAllocator` weight/equal modes, reserve logic |
| `test_backtest_costs.py` | Fee and slippage cost model calculations |
| `test_backtest_runner_cache.py` | `BacktestRunner` catalog caching behaviour |
| `test_base_strategy_sizing.py` | `BaseStrategy` position-sizing helpers |
| `test_binance_adapter.py` | `BinanceAdapter` start/stop, env resolution |
| `test_bootstrap_live.py` | `bootstrap` / `bootstrap_app` live entry-point wiring |
| `test_config_loading.py` | `load_app_config` YAML merge and Pydantic validation |
| `test_downloader.py` | Historical data download utilities |
| `test_drawdown_control.py` | `DrawdownControl` dynamic position reduction |
| `test_ema_cross_filters.py` | EMA cross strategy entry filter logic |
| `test_ema_pullback_atr.py` | EMA pullback + ATR strategy signal generation |
| `test_factory_strategy_loader.py` | `AppFactory` strategy class-path resolution |
| `test_funding.py` | Funding-rate fetch and attribution utilities |
| `test_live_readiness.py` | `ReadinessCheck` collection and pass/fail gates |
| `test_live_warmup.py` | Live warm-up indicator pre-fill logic |
| `test_loaders.py` | Parquet catalog loader helpers |
| `test_micro_scalp_strategy.py` | `MicroScalpStrategy` signal generation |
| `test_nautilus_cache.py` | NautilusTrader cache abstraction layer |
| `test_notifier_telegram.py` | Telegram notifier dispatch and formatting |
| `test_order_router.py` | `OrderRouter` routing, DEGRADED rejection |
| `test_reconciliation.py` | `ReconciliationManager` diff logic |
| `test_regime.py` | Market-regime detector (trend/range classification) |
| `test_signal_processor.py` | `SignalProcessor` dedup and cooldown enforcement |
| `test_supervisor.py` | `Supervisor` state machine transitions |
| `test_turtle_strategy.py` | Turtle breakout strategy signal generation |
| `test_validators.py` | Config and order validation helpers |
| `test_vegas_tunnel_strategy.py` | Vegas Tunnel EMA + Fibonacci TP logic |
| `test_walkforward.py` | Walk-forward split and metric aggregation |
| `test_watchers.py` | Risk `Watcher` classes (drawdown, daily loss) |

## For AI Agents

### Working In This Directory
- Run: `uv run pytest tests/unit/ -v`
- Run a single file: `uv run pytest tests/unit/test_allocator.py -v`
- All imports must be `from src.xxx`—never relative imports.
- No `@pytest.mark.asyncio` needed; `asyncio_mode = "auto"` handles it globally.

### Testing Requirements
- Every test must be fully self-contained—no network calls, no real DB, no filesystem writes.
- Mock Nautilus objects (`Clock`, `Logger`, `Cache`, `Portfolio`) with `unittest.mock.MagicMock`.
- Use `pytest.fixture` for reusable objects; keep fixtures local to the file or in a `conftest.py` at this level if broadly shared.
- Aim for one logical assertion per test; use descriptive test names (`test_<unit>_<scenario>_<expected>`).

### Common Patterns
```python
from unittest.mock import MagicMock, patch
from src.portfolio.allocator import PortfolioAllocator

def test_equal_weight_sums_to_one():
    alloc = PortfolioAllocator(strategies=[...], mode="equal")
    weights = alloc.compute_weights()
    assert sum(weights.values()) == pytest.approx(1.0)
```
- Patch `load_app_config` to avoid filesystem access in config tests.
- Use `pytest.raises(ValueError)` to assert strict Pydantic validation failures.

## Dependencies

### Internal
- `src.portfolio.allocator`, `src.risk.*`, `src.execution.*`, `src.strategy.*`, `src.live.*`, `src.core.*`

### External
- `pytest`, `pytest-asyncio`
- `unittest.mock` (stdlib)
- `nautilus_trader` (test kit providers, model objects)

<!-- MANUAL: -->
