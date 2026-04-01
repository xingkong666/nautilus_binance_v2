<!-- Parent: ../AGENTS.md -->
<!-- Generated: 2026-04-01 | Updated: 2026-04-01 -->

# configs/

## Purpose
Centralised YAML configuration tree for the entire trading system. All files are loaded and merged by `src.core.config.load_app_config(env)` into a fully typed `AppConfig` (Pydantic). The merge priority is: **environment variables** > `configs/env/{env}.yaml` > module-level YAMLs (`risk/`, `execution/`, `monitoring/`, etc.) > code defaults. Strict Pydantic validation means any unknown or mistyped field causes an immediate startup failure.

## Key Files

| File | Description |
|------|-------------|
| `instruments.yaml` | Tradable instrument list with Nautilus provider names and market-cap rank ordering |
| `accounts/binance_futures.yaml` | Binance MARGIN/HEDGING account parameters and starting balance |
| `redis/redis.conf` | Redis server configuration used by `docker compose` |

## Subdirectories

| Directory | Description |
|-----------|-------------|
| `env/` | Per-environment overrides (dev / stage / prod / small_cap / phase1 / phase2) |
| `strategies/` | Per-strategy YAML configs loaded by `AppFactory` |
| `risk/` | Global risk parameters (pre-trade, real-time, circuit-breaker, post-trade) |
| `execution/` | Execution engine settings (order type, slippage, fees, rate limits, algo) |
| `monitoring/` | Alerting rules and Prometheus scrape configuration |

## For AI Agents

### Working In This Directory
- **Never hardcode secrets** (API keys, passwords) in YAML—use `.env` + `EnvSettings` (pydantic-settings).
- Configuration is loaded once at startup; changes require a process restart.
- To add a new config section: define the Pydantic model in `src/core/config.py`, add the YAML key, and provide a default value to keep existing envs working.
- Use `uv run python -c "from src.core.config import load_app_config; print(load_app_config('dev'))"` to validate changes.

### Common Patterns
- Override any module YAML value in `configs/env/{env}.yaml` using the same dotted key path.
- Small-capital mode (`small_cap.yaml`) overrides risk thresholds via the `risk.override` section in `global_risk.yaml`.
- `instruments.yaml` drives the live instrument subscription list; `market_cap_rank` controls selection order when `max_instruments` is set.

## Dependencies

### Internal
- `src.core.config.load_app_config` — primary consumer
- `src.app.container.Container` — reads `AppConfig` for every service
- `src.app.bootstrap` — entry point that calls `load_app_config`

### External
- `pydantic` / `pydantic-settings` — config validation and `.env` parsing
- `PyYAML` — YAML file parsing

<!-- MANUAL: -->
