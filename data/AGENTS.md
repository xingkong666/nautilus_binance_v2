<!-- Parent: ../AGENTS.md -->
<!-- Generated: 2026-04-01 | Updated: 2026-04-01 -->

# data/

## Purpose
Persistent data store for the trading system. Holds raw downloaded market data, the NautilusTrader ParquetDataCatalog used by the backtest engine, production state snapshots written by `SnapshotManager`, a SQLite trades database, processed funding-rate Parquet files, a feature engineering store, and versioned datasets. This directory is **not** checked into git (large binaries); it is populated by `scripts/download_data.py` and by the live system at runtime.

## Key Files

| File | Description |
|------|-------------|
| `processed/trades.db` | SQLite database written by `TradePersistence`; contains trade records and events for the live system (fallback / local copy alongside PostgreSQL) |
| `processed/catalog/latest.json` | Pointer to the active NautilusTrader `ParquetDataCatalog` used by `BacktestRunner` |
| `processed/snapshots/latest.json` | Pointer to the most recent `SnapshotManager` snapshot (used by `RecoveryManager` on restart) |
| `processed/snapshots/snapshot_*.json` | Timestamped state snapshots (nanosecond epoch in filename); loaded by `RecoveryManager` during node recovery |
| `processed/funding_rates_BTCUSDT.parquet` | Processed funding rate time series for BTCUSDT (Parquet format) |
| `processed/funding_rates_ETHUSDT.parquet` | Processed funding rate time series for ETHUSDT (Parquet format) |

## Subdirectories

| Directory | Purpose |
|-----------|---------|
| `raw/` | Raw data as downloaded; never modified after ingestion |
| `raw/futures/` | Historical OHLCV CSV files per instrument (e.g. `BTCUSDT.csv`, `ETHUSDT.csv`) downloaded by `scripts/download_data.py` |
| `raw/funding/` | Raw funding rate CSVs per instrument before processing |
| `processed/` | Transformed data ready for backtesting and live use |
| `processed/catalog/` | NautilusTrader `ParquetDataCatalog` root — consumed directly by `BacktestRunner` via `src/backtest/runner.py` |
| `processed/catalog/data/bar/` | 1-minute OHLCV bar Parquet files for BTCUSDT-PERP, ETHUSDT-PERP, BNBUSDT-PERP, SOLUSDT-PERP |
| `processed/catalog/data/crypto_perpetual/` | Instrument definition Parquet files (tick size, lot size, etc.) for each perpetual contract |
| `processed/snapshots/prod/` | Production state snapshots written by `SnapshotManager` during live trading |
| `features/` | Feature-engineered datasets produced by `FeatureStore` (`src/data/`); used by ML-augmented strategies |
| `versioned/` | Immutable versioned dataset snapshots for reproducible experiment replay |

## For AI Agents

### Working In This Directory
- **Do not modify `raw/`** — raw data is write-once. Re-download via `scripts/download_data.py` if corruption is suspected.
- **Catalog path in config**: `configs/env/*.yaml` specifies `catalog_path`; it must point to `data/processed/catalog/`. Update the config, not the directory name.
- **Snapshot files are binary-ish JSON** — `RecoveryManager` loads the file named in `latest.json`. To roll back to an earlier snapshot, update `latest.json` to point to a previous `snapshot_*.json` file.
- **`trades.db` is a secondary store** — PostgreSQL (`TradePersistence`) is primary. `trades.db` is a local SQLite copy; do not treat it as the source of truth.
- **Adding new instruments**: run `scripts/download_data.py` with the new symbol, then re-import into the catalog. The catalog auto-discovers bar files by instrument ID subdirectory naming.
- **`features/` and `versioned/` may be empty** in fresh checkouts — they are populated by experiment scripts and `FeatureStore` writes at runtime.
- **Log files** (`live.stdout.log`, `live.stderr.log`) may appear here at runtime when the systemd service is active (written by `deploy/systemd/nautilus-live.service`).

### Common Patterns
- **Download and ingest fresh data**:
  ```bash
  uv run python scripts/download_data.py   # writes to data/raw/ and data/processed/catalog/
  ```
- **Verify catalog contents** (from Python):
  ```python
  from nautilus_trader.persistence.catalog import ParquetDataCatalog
  catalog = ParquetDataCatalog("data/processed/catalog")
  print(catalog.instruments())
  ```
- **Check latest snapshot**:
  ```bash
  cat data/processed/snapshots/latest.json
  ```
- **Inspect funding rates**:
  ```python
  import pandas as pd
  df = pd.read_parquet("data/processed/funding_rates_BTCUSDT.parquet")
  ```

## Dependencies

### Internal
- `../src/data/loaders.py` — writes raw CSVs to `raw/` and imports bars into `processed/catalog/`
- `../src/backtest/runner.py` — reads `processed/catalog/` via `ParquetDataCatalog`
- `../src/state/snapshot.py` — writes/reads `processed/snapshots/`
- `../src/state/persistence.py` — writes `processed/trades.db`
- `../src/data/` (`FeatureStore`) — writes `features/`
- `../scripts/download_data.py` — primary data ingestion entry point
- `../configs/env/*.yaml` — `catalog_path` and `snapshot_dir` point into this directory

### External
- Binance public REST API — source of raw OHLCV and funding rate data
- NautilusTrader `ParquetDataCatalog` — Parquet read/write format for `processed/catalog/`
- `pandas` / `pyarrow` — used by loaders and feature store for Parquet I/O

<!-- MANUAL: -->
