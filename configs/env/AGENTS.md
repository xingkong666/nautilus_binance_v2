<!-- Parent: ../AGENTS.md -->
<!-- Generated: 2026-04-01 | Updated: 2026-04-01 -->

# configs/env/

## Purpose
Environment-specific override files that sit at the top of the configuration priority stack (below only explicit environment variables). Each file sets values for `env`, `logging`, `exchange.environment`, `risk.mode`, `monitoring.enabled`, and other fields that differ between deployment contexts. `load_app_config(env)` selects the file matching the `env` argument (e.g., `"dev"` → `dev.yaml`).

## Key Files

| File | Description |
|------|-------------|
| `dev.yaml` | Testnet, DEBUG logging, `risk.mode: soft` (alert-only), monitoring disabled, `submit_orders: true` |
| `stage.yaml` | Staging environment; mirrors prod topology but against Testnet API |
| `prod.yaml` | Production: WARNING logging, `exchange.environment: LIVE`, monitoring enabled, strict risk |
| `small_cap.yaml` | 2500 USDT capital overlay: tightened position limits, reduced order sizes, 8% drawdown threshold |
| `phase1_small_cap.yaml` | Phase 1 growth plan with small-capital risk parameters and conservative strategy selection |
| `phase2_growth.yaml` | Phase 2: relaxed regime gate, increased capital allocation, drawdown threshold at 13% |

## For AI Agents

### Working In This Directory
- **`prod.yaml` is production-critical**—every change requires review and must be tested against `stage.yaml` first.
- To select an env at runtime: `uv run python -m src.app.bootstrap --env configs/env/dev.yaml`.
- Values in these files override the same keys in all module YAMLs (`risk/`, `execution/`, etc.).
- Sensitive values (`binance_api_key`, `telegram_bot_token`) must **never** appear here—they are read from `.env` via `EnvSettings`.
- When adding a new phase config, copy the nearest existing file and document the diff in a comment block at the top.

### Common Patterns
```yaml
# configs/env/dev.yaml key fields
env: dev
exchange:
  environment: TESTNET   # TESTNET | LIVE
risk:
  enabled: true
  mode: soft             # soft=alert-only | hard=blocking
monitoring:
  enabled: false
  prometheus_port: 9100
execution:
  submit_orders: true
```
- `risk.mode: soft` in dev means risk violations log warnings but do not block orders—safe for testing.
- `risk.mode: hard` in prod means violations raise exceptions and halt order submission.
- Small-cap envs activate `small_capital` section of `global_risk.yaml` via `risk.override`.

## Dependencies

### Internal
- `src.core.config.load_app_config` — selects and merges this file
- `src.app.bootstrap` — passes `env` path as CLI argument

### External
- `pydantic` / `pydantic-settings` — `EnvSettings` reads `.env` for secrets
- `PyYAML` — file parsing

<!-- MANUAL: -->
