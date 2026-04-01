<!-- Parent: ../AGENTS.md -->
<!-- Generated: 2026-04-01 | Updated: 2026-04-01 -->

# tests/integration/

## Purpose
Integration tests that verify the correct wiring of the application's major components end-to-end without hitting Binance's live API. They exercise the full `Container` build lifecycle, the `EventBus` signal-to-fill pipeline, multi-strategy co-existence, and the risk integration chain. A running PostgreSQL instance is required for persistence-related tests.

## Key Files

| File | Description |
|------|-------------|
| `conftest.py` | Shared fixtures: `_TestEnvSettings` stub, patched `load_app_config`, container teardown helpers |
| `test_container_build.py` | Verifies `Container.build()` instantiates all services and `teardown()` completes without errors |
| `test_event_pipeline.py` | End-to-end: `SignalEvent` → `PreTradeRiskManager` → `OrderIntentEvent` → `FillHandler` → `TradePersistence` |
| `test_multi_strategy_integration.py` | Multiple strategies share a single `EventBus`; signals don't cross-contaminate |
| `test_risk_integration.py` | Risk chain integration: pre-trade block, circuit-breaker trigger, post-trade attribution |

## For AI Agents

### Working In This Directory
- Run: `uv run pytest tests/integration/ -v`
- Requires PostgreSQL at `postgresql://admin:Longmao!666@127.0.0.1:5432/nautilus_trader` (start with `docker compose up -d postgres`).
- Tests use `_TestEnvSettings` stubs instead of real `.env` files—no live API keys needed.
- No `@pytest.mark.asyncio` needed.

### Testing Requirements
- Each test must call `container.teardown()` (or use a fixture that does) to release DB connections and async resources.
- Use `pytest.fixture(scope="function")` for containers to avoid state leakage between tests.
- Do not assert on exact timing or order counts if they depend on Binance market data.
- If adding a new integration test, patch `BinanceAdapter` so no real WebSocket connection is opened.

### Common Patterns
```python
@pytest.fixture
async def container(monkeypatch):
    monkeypatch.setattr(config_module, "EnvSettings", _TestEnvSettings)
    cfg = load_app_config("dev")
    c = Container(cfg)
    await c.build()
    yield c
    await c.teardown()

async def test_container_builds(container):
    assert container.event_bus is not None
    assert container.order_router is not None
```
- `EventBus` publish/subscribe patterns: `bus.publish(EventType.SIGNAL, event)` then assert handler called.
- Use `asyncio.wait_for(...)` with a short timeout to guard against hangs in pipeline tests.

## Dependencies

### Internal
- `src.app.container.Container`
- `src.core.events.EventBus`, `EventType`, `SignalEvent`, `OrderIntentEvent`
- `src.risk.pre_trade.PreTradeRiskManager`
- `src.execution.fill_handler.FillHandler`
- `src.state.persistence.TradePersistence`
- `src.core.config.load_app_config`

### External
- `pytest`, `pytest-asyncio`
- PostgreSQL (via `docker compose up -d postgres`)
- `psycopg` (connection pool used by `TradePersistence`)

<!-- MANUAL: -->
