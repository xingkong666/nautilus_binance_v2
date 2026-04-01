<!-- Parent: ../AGENTS.md -->
<!-- Generated: 2026-04-01 | Updated: 2026-04-01 -->

# app

## Purpose

Composition root for the entire trading system. Owns three responsibilities: (1) **Container** — singleton DI container that builds and tears down every service in dependency order; (2) **AppFactory** — stateless object factory that creates strategies, backtest runners, and BinanceAdapters from config; (3) **bootstrap** — entry-point helpers that wire container + factory together and expose clean lifecycle APIs for scripts, tests, and the live process.

## Key Files

| File | Description |
|------|-------------|
| `container.py` | `Container` class — builds all service singletons in 8 ordered phases; exposes typed property accessors; `teardown()` releases resources in reverse order |
| `factory.py` | `AppFactory` class — creates strategy `(cls, config)` tuples, `BacktestRunner`, and `BinanceAdapter`; stateless, always delegates to `Container` for already-built adapters |
| `bootstrap.py` | `bootstrap()`, `bootstrap_app()`, `bootstrap_context()`, `run_live()`, `main()` — entry-point functions, SIGINT/SIGTERM handler, strategy registry, live-state recovery wiring |

## For AI Agents

### Working In This Directory

**Container build order** (do not reorder; later phases depend on earlier ones):
1. Infrastructure — `EventBus`, `TradePersistence`, `SnapshotManager`, `RedisClient`
2. Execution layer — `RateLimiter`, `IgnoredInstrumentRegistry`, `PositionSizer`
3. Risk layer — `PreTradeRiskManager`, `CircuitBreaker`, `DrawdownController`, `RealTimeRiskMonitor`
4. Portfolio — `PortfolioAllocator` (only when `strategies.portfolio` block present in YAML)
5. Exchange — `BinanceAdapter` (only for `prod`/`staging` or when `exchange:` block present)
6. Fill & routing — `FillHandler`, `OrderRouter`, `SignalProcessor`
7. Alerting — `AlertManager`, `BaseWatcher` list
8. Monitoring — `PrometheusServer` (`:9090`), `HealthServer` (`:8080`) — only when `monitoring.enabled=true`

**Critical lifecycle rules:**
- Always call `container.build()` before accessing any property — accessing an unbuilt property raises `RuntimeError`.
- `BinanceAdapter.stop()` is `async`. Call `await adapter.stop()` **before** `container.teardown()` in any async context; `teardown()` only warns if the adapter is still running.
- For context-managed usage, prefer `bootstrap_context()` — it guarantees `teardown()` on exit.
- `Redis` failure is non-fatal: `Container` degrades gracefully and logs `redis_unavailable_degraded_mode`.

**Strategy registry** (`_STRATEGY_REGISTRY` in `bootstrap.py`):
Currently registered: `ema_cross`, `ema_pullback_atr`, `turtle`, `micro_scalp`, `vegas_tunnel`.
To add a new strategy, import the `(StrategyClass, ConfigClass)` pair and add it to `_STRATEGY_REGISTRY`.

**`AppFactory` methods:**
- `create_ema_cross_strategy(symbol, interval, ...)` → `(EMACrossStrategy, EMACrossConfig)`
- `create_ema_pullback_atr_strategy(...)` → `(EMAPullbackATRStrategy, EMAPullbackATRConfig)`
- `create_turtle_strategy(...)` → `(TurtleStrategy, TurtleConfig)`
- `create_micro_scalp_strategy(...)` → `(MicroScalpStrategy, MicroScalpConfig)`
- `create_vegas_tunnel_strategy(...)` → `(VegasTunnelStrategy, VegasTunnelConfig)`
- `create_strategy_from_config(strategy_cfg, symbol, interval)` — dynamic dispatch via strategy name string
- `create_backtest_runner(start, end, symbols, interval, ...)` → `BacktestRunner`
- `create_binance_adapter(symbols, leverages, environment, proxy_url)` → `BinanceAdapter` (reuses container instance if present)

### Testing Requirements

- Use `bootstrap_context(env="dev")` in integration tests for automatic teardown.
- `bootstrap(env=None)` without a `.env` file defaults to `env="dev"` via `EnvSettings`.
- Tests that need a container without live infrastructure: mock `TradePersistence` and `RedisClient` or use `env="dev"` with `submit_orders=false`.
- Async adapter tests: wrap in `asyncio.run()` or use `pytest-asyncio` (configured auto mode).

### Common Patterns

```python
# Minimal script usage
container = bootstrap(env="dev")
factory = AppFactory(container)
runner = factory.create_backtest_runner(start, end)
result = runner.run(EMACrossStrategy, config)
container.teardown()

# Preferred context-manager usage
with bootstrap_context(env="dev") as ctx:
    runner = ctx.factory.create_backtest_runner(start, end)
    result = runner.run(EMACrossStrategy, config)

# Live run (called by bootstrap.py main())
run_live(env="prod", strategy_config="configs/strategies/ema_cross.yaml", symbol="BTCUSDT")
```

## Dependencies

### Internal
- `src.core.config` — `AppConfig`, `EnvSettings`, `load_app_config`, `load_yaml`
- `src.core.events` — `EventBus`
- `src.core.logging` — `setup_logging`
- `src.execution.*` — `OrderRouter`, `SignalProcessor`, `FillHandler`, `RateLimiter`, `PositionSizer`, `IgnoredInstrumentRegistry`
- `src.risk.*` — `PreTradeRiskManager`, `CircuitBreaker`, `DrawdownController`, `RealTimeRiskMonitor`
- `src.portfolio.allocator` — `PortfolioAllocator`
- `src.state.*` — `TradePersistence`, `SnapshotManager`, `ReconciliationEngine`, `RecoveryManager`
- `src.exchange.binance_adapter` — `BinanceAdapter`
- `src.monitoring.*` — `AlertManager`, `PrometheusServer`, `HealthServer`, watchers
- `src.live.*` — `LiveSupervisor`, `ensure_live_readiness`, `preload_strategies_warmup`
- `src.backtest.runner` — `BacktestRunner`, `BacktestConfig`
- `src.strategy.*` — all five concrete strategy classes and their configs

### External
- `nautilus_trader` — `BinanceEnvironment`, `BarType`, `InstrumentId`
- `structlog` — structured logging
- `pydantic` / `pydantic-settings` — config models

<!-- MANUAL: -->
