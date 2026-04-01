<!-- Parent: ../AGENTS.md -->
<!-- Generated: 2026-04-01 | Updated: 2026-04-01 -->

# core

## Purpose

Foundational layer shared by every other module in the system. Defines the event model and in-process pub/sub bus (`events.py`), the full typed configuration hierarchy (`config.py`), project-wide path constants and event-name strings (`constants.py`), trading enums and Binance-to-Nautilus interval mappings (`enums.py`), structured logging setup (`logging.py`), NautilusTrader cache configuration builder (`nautilus_cache.py`), and exchange clock sync utilities (`time_sync.py`). **No other `src/` module is imported here** — `core` is the base of the dependency graph.

## Key Files

| File | Description |
|------|-------------|
| `events.py` | `EventType` enum (17 types), `SignalDirection` enum, frozen `Event` / `SignalEvent` / `OrderIntentEvent` / `RiskAlertEvent` dataclasses, `EventBus` pub/sub with per-type and global (`subscribe_all`) handlers |
| `config.py` | `AppConfig` and all sub-config Pydantic models (`RiskConfig`, `ExecutionConfig`, `MonitoringConfig`, `LiveConfig`, `AccountConfig`, `DataConfig`, `RedisConfig`, `NautilusCacheConfig`); `EnvSettings` (pydantic-settings, reads `.env`); `load_app_config()` multi-file merge; `load_yaml()` and `deep_merge()` helpers |
| `constants.py` | `BASE_DIR`, `CONFIGS_DIR`, `DATA_DIR` path constants; event-name string literals; risk mode and circuit-breaker action constants |
| `enums.py` | `TraderType`, `Interval` (12 bar intervals), `INTERVAL_TO_MS`, `INTERVAL_TO_NAUTILUS` mapping dicts, `DEFAULT_INSTRUMENTS` list |
| `logging.py` | `setup_logging(level)` — configures `structlog` with JSON or console renderer based on environment |
| `nautilus_cache.py` | `build_nautilus_cache_settings(config, mode)` — produces NautilusTrader `CacheConfig` + `instance_id` tuple for live or backtest modes |
| `time_sync.py` | Exchange clock synchronisation utilities for latency-sensitive live operation |

## For AI Agents

### Working In This Directory

**EventBus usage contract:**
- `bus.subscribe(EventType.X, handler)` — typed subscription; handler receives the concrete `Event` subclass.
- `bus.subscribe_all(handler)` — global handler (used by Prometheus metrics counter in `Container`).
- `bus.publish(event)` — synchronous, in-process fan-out; exceptions in individual handlers are caught and logged, not re-raised.
- `bus.clear()` — called by `Container.teardown()`; removes all handlers.
- The bus is **not thread-safe** by design. All publishing happens on the main trading thread.

**Event authoring rules:**
- All event dataclasses must be `frozen=True`.
- Subclasses must set `event_type` in `__post_init__` via `object.__setattr__(self, "event_type", EventType.X)` (required because the base dataclass is frozen).
- `timestamp_ns` defaults to `time.time_ns()` — do not pass a manual value unless writing deterministic tests.

**Config loading priority** (highest → lowest):
1. Environment variables / `.env` file (via `EnvSettings`)
2. `configs/env/{env}.yaml`
3. Module YAMLs (`configs/risk/global_risk.yaml`, `configs/execution/execution.yaml`, `configs/accounts/binance_futures.yaml`, `configs/monitoring/alerts.yaml`)
4. Pydantic field defaults

`deep_merge()` performs recursive dict merge; scalar values in the higher-priority source always win.

**Adding a new config field:**
1. Add the field with a default to the appropriate Pydantic sub-model.
2. If it should be overridable from `.env`, add a corresponding field to `EnvSettings` and wire it in the relevant `_env_*_overrides()` helper in `config.py`.
3. Never add `dict[str, Any]` fields to sub-models unless they are genuinely freeform (like `pre_trade` risk rules).

**Interval mapping:** use `INTERVAL_TO_NAUTILUS[interval]` to get the NautilusTrader bar-spec string. For non-`MINUTE_1` intervals the `AppFactory` appends `@1-MINUTE-EXTERNAL` to create a synthetic bar type.

### Testing Requirements

- `EventBus` is stateless between tests — instantiate a fresh one per test; do not share instances.
- `load_app_config(env="dev")` can be called in tests; ensure `configs/env/dev.yaml` exists or pass a temp path.
- Config models are Pydantic — test invalid inputs with `pytest.raises(ValidationError)`.

### Common Patterns

```python
# EventBus publish/subscribe
from src.core.events import EventBus, EventType, SignalEvent, SignalDirection

bus = EventBus()
bus.subscribe(EventType.SIGNAL, lambda e: print(e))
bus.publish(SignalEvent(instrument_id="BTCUSDT-PERP.BINANCE", direction=SignalDirection.LONG))

# Config loading
from src.core.config import load_app_config
config = load_app_config(env="dev")
db_url = config.data.database_url

# Interval mapping
from src.core.enums import Interval, INTERVAL_TO_NAUTILUS
nautilus_str = INTERVAL_TO_NAUTILUS[Interval.HOUR_4]  # "4-HOUR"

# Path constants
from src.core.constants import BASE_DIR, CONFIGS_DIR
yaml_path = CONFIGS_DIR / "env" / "dev.yaml"
```

## Dependencies

### Internal
None — `core` has no imports from other `src/` modules.

### External
- `pydantic` — config model validation
- `pydantic-settings` — `EnvSettings` env-var / `.env` loading
- `structlog` — logger access
- `yaml` (PyYAML) — YAML file loading
- `nautilus_trader` — `CacheConfig`, `MessageBusConfig` (in `nautilus_cache.py`)

<!-- MANUAL: -->
