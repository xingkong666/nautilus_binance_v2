<!-- Parent: ../AGENTS.md -->
<!-- Generated: 2026-04-01 | Updated: 2026-04-01 -->

# tests/

## Purpose
Houses all automated tests for the nautilus_binance_v2 trading system, organized into three tiers: unit tests (fast, fully mocked), integration tests (wiring and event pipeline verification), and regression tests (baseline-locked backtest metrics). Tests use pytest with `asyncio_mode = "auto"` so no `@pytest.mark.asyncio` decorator is needed. All source imports use `from src.xxx`.

## Key Files

| File | Description |
|------|-------------|
| `__init__.py` | Package marker enabling `from tests.*` imports in fixtures |

## Subdirectories

| Directory | Description |
|-----------|-------------|
| `unit/` | ~29 isolated unit tests; all external dependencies mocked with `MagicMock` |
| `integration/` | 4 tests verifying container assembly, event pipeline, multi-strategy wiring, and risk integration |
| `regression/` | 4 baseline-locked backtest tests + shared `conftest.py` with in-memory engine fixtures |

## For AI Agents

### Working In This Directory
- Run the full suite with `uv run pytest` from the project root (≈145 tests).
- Run a single tier: `uv run pytest tests/unit/ -v`, `tests/integration/ -v`, or `tests/regression/ -v`.
- Generate coverage: `uv run pytest --cov=src --cov-report=html`.
- All tests import via `from src.xxx`—never use relative imports.
- `asyncio_mode = "auto"` is set in `pyproject.toml`; do not add `@pytest.mark.asyncio`.

### Testing Requirements
- Unit tests must not touch the network, filesystem, or real Nautilus objects—use `MagicMock`.
- Integration tests may require a running PostgreSQL instance (configured via `PG_URL` in each file).
- Regression tests must not tighten baselines without re-running on fresh data and updating the hardcoded `BASELINE` dict with a clear commit message.

### Common Patterns
- Fixtures live in tier-local `conftest.py` files; the regression conftest is the richest (in-memory `BacktestEngine`, synthetic bar builders).
- Tests that need `AppConfig` mock `load_app_config` or pass a `_TestEnvSettings`-style stub.
- Use `pytest.raises` for expected exceptions; use `assert ... == approx(...)` for floating-point metrics.

## Dependencies

### Internal
- `src.*` — all production modules under test
- `tests/regression/conftest.py` — shared backtest fixtures for regression tier

### External
- `pytest`, `pytest-asyncio` — test runner and async support
- `nautilus_trader` — `BacktestEngine`, `TestInstrumentProvider`, `MagicMock`-able objects
- `psycopg` — required by integration tests that exercise `TradePersistence`

<!-- MANUAL: -->
