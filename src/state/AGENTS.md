<!-- Parent: ../AGENTS.md -->
<!-- Generated: 2026-04-01 | Updated: 2026-04-01 -->

# state

## Purpose
Durable state management for the trading system. Handles four concerns: persisting trade records to PostgreSQL, periodic memory snapshots to disk, reconciliation of local state against live exchange positions, and recovery from snapshots + reconciliation results on node restart.

## Key Files

| File | Description |
|------|-------------|
| `persistence.py` | `TradePersistence` — PostgreSQL backend via `psycopg` (sync). Connects with `autocommit=False`. Auto-creates `trades` and `events` tables on first connect (`_init_tables()`). `database_url` sourced from `AppConfig` / `DATABASE_URL` env var. Key methods: `save_trade()`, `save_event()`, `get_trades()` |
| `snapshot.py` | `SnapshotManager` — serializes in-memory state (positions, equity, strategy states) to disk at a configurable interval. Snapshots are JSON files written atomically to the configured snapshot directory |
| `reconciliation.py` | `ReconciliationEngine` — compares `local_positions` against `exchange_positions`; returns `ReconciliationResult(matched, local_positions, exchange_positions, mismatches)`. Publishes `RiskAlertEvent` for each mismatch. Called periodically by the live supervisor or on startup |
| `recovery.py` | `RecoveryManager` — orchestrates restart recovery: loads the latest snapshot, runs reconciliation, applies exchange-side corrections to bring local state in sync before resuming live trading |

## For AI Agents

### Working In This Directory

- **`TradePersistence` opens a persistent connection** at `__init__` time — do not instantiate it in hot paths or tests without a real/mock PostgreSQL URL. Use `psycopg.connect` with `autocommit=False`; commits are explicit.
- `database_url` format: `postgresql://user:pass@host:5432/dbname`. In tests, use `pytest-postgresql` or a `docker compose` test database; never use the production URL.
- `ReconciliationEngine.reconcile(local_positions, exchange_positions)` is **pure** (no side effects except `EventBus` publish on mismatch) — safe to call in dry-run mode.
- `SnapshotManager` writes atomically (write to `.tmp`, then `os.replace`) — do not interrupt mid-write. The snapshot directory must exist before the manager is constructed.
- `RecoveryManager.recover()` must complete before `Container.build()` allows `OrderRouter` to accept new orders. The live `Supervisor` enforces this sequencing.
- All timestamps stored as nanoseconds (`timestamp_ns: BIGINT`) to match NautilusTrader's native precision.
- Imports: `from src.state.persistence import TradePersistence`, `from src.state.reconciliation import ReconciliationEngine`, etc.

### Testing Requirements

- `TradePersistence`: use a real PostgreSQL test DB (via Docker or `pytest-postgresql`). Test `save_trade()` round-trip and `get_trades()` filter by `instrument_id` and time range. Test table auto-creation idempotency.
- `ReconciliationEngine`: supply crafted `local_positions` / `exchange_positions` dicts with known mismatches; assert `ReconciliationResult.mismatches` contains the correct entries and `RiskAlertEvent` is published for each.
- `SnapshotManager`: test atomic write (verify `.tmp` file is removed after commit); test load-from-snapshot restores exact state dict.
- `RecoveryManager`: integration test using a saved snapshot + a reconciliation result with one mismatch; assert the recovered state reflects the exchange-side value.

### Common Patterns

```python
# Persistence — save a trade fill
from src.state.persistence import TradePersistence

persistence = TradePersistence(database_url=config.database_url)
persistence.save_trade(
    instrument_id="BTCUSDT-PERP.BINANCE",
    side="BUY",
    quantity="0.01",
    price="65000.00",
    order_id="abc123",
    strategy_id="ema_cross",
    fees="0.65",
)

# Reconciliation — compare local vs exchange
from src.state.reconciliation import ReconciliationEngine

engine = ReconciliationEngine(event_bus=bus)
result = engine.reconcile(
    local_positions=[{"instrument_id": "BTCUSDT-PERP.BINANCE", "quantity": "0.01"}],
    exchange_positions=[{"instrument_id": "BTCUSDT-PERP.BINANCE", "quantity": "0.02"}],
)
if not result.matched:
    for mismatch in result.mismatches:
        logger.warning("reconciliation_mismatch", **mismatch)
```

## Dependencies

### Internal
- `src.core.events` — `EventBus`, `RiskAlertEvent`
- `src.core.config` — `AppConfig` (supplies `database_url`)

### External
- `psycopg` — PostgreSQL driver (sync, v3 API)
- `structlog` — structured logging
- `json` — snapshot serialization
- `decimal` — position quantity precision

<!-- MANUAL: -->
