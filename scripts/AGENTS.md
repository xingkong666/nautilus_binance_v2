<!-- Parent: ../AGENTS.md -->
<!-- Generated: 2026-04-01 | Updated: 2026-04-01 -->

# scripts/

## Purpose
Operational and research scripts for data acquisition, backtesting, parameter optimisation, live deployment, and system health checks. These are standalone entry points invoked directly via `uv run python scripts/<name>.py` (or `bash` for shell scripts). They use `from src.xxx` imports and rely on the same `AppConfig` / `bootstrap` infrastructure as the main application.

## Key Files

| File | Description |
|------|-------------|
| `download_data.py` | Downloads Binance Futures OHLCV K-lines for specified symbols, intervals, and date ranges; optionally writes to a `ParquetDataCatalog` |
| `download_funding_rates.py` | Fetches 8-hourly funding rate history from Binance and persists for PnL attribution |
| `run_backtest.py` | Single-strategy backtest runner: loads catalog data, runs `BacktestEngine`, prints performance report; supports `--symbols`, `--start`, `--end`, `--balance`, `--leverage`, `--save` |
| `param_sweep.py` | Multi-process grid search over strategy parameters; writes results to CSV; supports in-sample/out-of-sample splits using EMA cross and RSI strategies |
| `run_portfolio_walkforward.py` | Walk-forward backtest across multiple time windows; aggregates per-window metrics |
| `smoke_testnet.py` | End-to-end Testnet smoke test: connects `TradingNode` to Testnet, subscribes to BTCUSDT quote ticks, fires a market order, waits for fill, then halts |
| `check_live_readiness.py` | Pre-flight checklist runner: collects `ReadinessCheck` items (API connectivity, risk config, data freshness) and exits non-zero if any check fails |
| `run_live_testnet_strategy.py` | Launches a full live strategy run against Testnet using `bootstrap_app`; mirrors `bootstrap.py` prod flow |
| `run_live_prod.sh` | Shell wrapper for production launch; sets `PYTHONPATH`, activates venv, calls `bootstrap.py` with `configs/env/prod.yaml` |

## For AI Agents

### Working In This Directory
- Always run from the **project root**: `uv run python scripts/run_backtest.py ...` (not from inside `scripts/`).
- `download_data.py` and `download_funding_rates.py` require Binance API access (testnet keys suffice for recent data).
- `smoke_testnet.py` requires valid Testnet API keys in `.env` (`BINANCE_TESTNET_API_KEY`, `BINANCE_TESTNET_API_SECRET`) and a running network connection.
- `check_live_readiness.py` exits with code `1` if any check fails—suitable for CI pre-flight gates.
- `param_sweep.py` uses `multiprocessing`; default worker count is `os.cpu_count()`—reduce with `--workers N` on memory-constrained machines.

### Testing Requirements
- Scripts are not directly unit-tested; they are covered indirectly by `tests/unit/test_downloader.py`, `test_backtest_runner_cache.py`, `test_walkforward.py`, and `test_live_readiness.py`.
- For smoke-testing a script change, run with `--help` first, then against Testnet with a short date range.
- `run_live_prod.sh` must **never** be run in CI—it targets the live exchange.

### Common Patterns
```bash
# Download 3 months of BTC 1m bars
uv run python scripts/download_data.py \
  --symbols BTCUSDT --interval 1m \
  --start 2024-01-01 --end 2024-03-31

# Backtest EMA cross with custom capital
uv run python scripts/run_backtest.py \
  --config configs/strategies/ema_cross.yaml \
  --env configs/env/dev.yaml \
  --start 2024-01-01 --end 2024-06-30 \
  --balance 10000 --leverage 5 --save

# Parameter sweep (parallel)
uv run python scripts/param_sweep.py --workers 4

# Pre-flight check before going live
uv run python scripts/check_live_readiness.py --env configs/env/prod.yaml
```

## Dependencies

### Internal
- `src.app.bootstrap` — `bootstrap_app`, `bootstrap_context`
- `src.app.factory.AppFactory` — strategy instantiation
- `src.backtest.runner.BacktestRunner`, `BacktestConfig`
- `src.core.config.load_app_config`
- `src.live.readiness.ReadinessCheck`, `collect_live_readiness_checks`

### External
- `nautilus_trader` — `BacktestEngine`, `ParquetDataCatalog`, `TradingNode`
- `pandas` — sweep result CSV output
- Binance REST API — data download and readiness connectivity checks
- `argparse` (stdlib) — CLI argument parsing for all Python scripts

<!-- MANUAL: -->
