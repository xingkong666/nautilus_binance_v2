# src

## Purpose

Core application source code for the nautilus_binance_v2 institutional Binance futures trading system. Contains 13 Python modules implementing the complete trading pipeline: signal generation, execution, risk management, state persistence, monitoring, and infrastructure wiring.

## Key Files

| File | Description |
|------|-------------|
| `__init__.py` | Package marker; no public exports |

## Subdirectories

| Module | Role |
|--------|------|
| `app/` | Dependency container, object factory, bootstrap entry points |
| `core/` | Foundational primitives: EventBus, AppConfig, enums, constants, logging |
| `strategy/` | BaseStrategy + five concrete strategies; signal generation only |
| `execution/` | OrderRouter, SignalProcessor, FillHandler, RateLimiter, PositionSizer |
| `risk/` | PreTradeRisk, RealTimeRisk, CircuitBreaker, DrawdownControl, PostTradeRisk |
| `portfolio/` | PortfolioAllocator for multi-strategy capital allocation |
| `state/` | TradePersistence (PostgreSQL), SnapshotManager, ReconciliationEngine, RecoveryManager |
| `live/` | LiveSupervisor, Watchdog, readiness checks, warmup helpers |
| `monitoring/` | Prometheus metrics, AlertManager, HealthServer, Grafana watchers |
| `backtest/` | BacktestRunner and BacktestConfig wrapping NautilusTrader engine |
| `data/` | Historical data download, catalog management |
| `exchange/` | BinanceAdapter wrapping NautilusTrader TradingNode |
| `cache/` | RedisClient wrapper for optional distributed state |

## For AI Agents

### Architectural Rules — Read Before Editing

1. **Strategies only emit signals.** A strategy must never call exchange APIs or submit orders directly. The sole output is a `SignalEvent` published to `EventBus` (live) or a direct `submit_order` call in backtest-local mode (no EventBus). Do not add order submission logic to any file under `strategy/`.

2. **Cross-module communication is EventBus-only.** Modules must not import each other's classes to call methods directly. Publish an `Event` subclass; the subscriber handles it. The event taxonomy lives in `src/core/events.py`.

3. **All imports use `from src.xxx`.** The project is installed as an editable hatchling package. Never use relative imports (`from .foo`) or path-hacked imports.

4. **Module dependency layering** (lower layers must not import from higher layers):
   ```
   core/          ← no internal deps
   cache/         ← core
   state/         ← core, cache
   risk/          ← core
   portfolio/     ← core, risk
   execution/     ← core, risk, portfolio, state
   strategy/      ← core
   exchange/      ← core
   monitoring/    ← core
   live/          ← core, execution, risk, state, exchange
   backtest/      ← core, strategy, exchange
   app/           ← all modules (composition root)
   data/          ← core (standalone scripts)
   ```

5. **Config is Pydantic-validated at startup.** All configuration flows through `AppConfig` (assembled by `load_app_config()`). Invalid fields raise at import time — do not add `dict`-based config bypasses.

6. **Container owns all singleton lifecycles.** Service instances live in `Container`. Never construct service objects outside `Container.build()` except in tests.

### Testing Requirements

- Run the full suite: `uv run pytest tests/`
- Unit tests only: `uv run pytest tests/unit/ -v`
- Integration tests: `uv run pytest tests/integration/ -v`
- Async tests require no decorator — `asyncio_mode = "auto"` is set in `pyproject.toml`
- Generate coverage: `uv run pytest --cov=src --cov-report=html`

### Common Patterns

- Structured logging: `logger.info("event_name", key=val)` via `structlog`
- Signal publishing: `event_bus.publish(SignalEvent(...))`
- Subscribing: `event_bus.subscribe(EventType.SIGNAL, handler_fn)`
- Config access: always via `container.config.<section>.<field>`

## Dependencies

### Internal
All modules depend on `src/core/` for `EventBus`, `AppConfig`, `EventType`, and `SignalDirection`.

### External
- `nautilus_trader` — trading engine, data types, backtest engine
- `pydantic` / `pydantic-settings` — config validation and env var loading
- `structlog` — structured logging
- `psycopg` — PostgreSQL persistence
- `redis` — optional distributed cache
- `prometheus_client` — metrics exposition

<!-- MANUAL: -->
