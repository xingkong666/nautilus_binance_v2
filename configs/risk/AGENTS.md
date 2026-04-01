<!-- Parent: ../AGENTS.md -->
<!-- Generated: 2026-04-01 | Updated: 2026-04-01 -->

# configs/risk/

## Purpose
Global risk parameter definitions for all three risk layers: `PreTradeRisk` (order admission), `RealTimeRisk` (per-bar drawdown monitoring), and the `CircuitBreaker` (halt/reduce/alert actions). Also contains `PostTradeRisk` settings (reconciliation interval, PnL attribution) and a dedicated `small_capital` override block for ≤5000 USDT accounts. Environment-specific files in `configs/env/` may override any value here.

## Key Files

| File | Description |
|------|-------------|
| `global_risk.yaml` | Master risk config: pre-trade limits, real-time thresholds, circuit-breaker triggers, post-trade settings, and `small_capital` override block |

## For AI Agents

### Working In This Directory
- Changes to `global_risk.yaml` affect **all environments** unless overridden in `configs/env/`.
- Before tightening any threshold (e.g., reducing `max_drawdown_pct`), verify against regression tests to ensure existing strategies still pass.
- The `small_capital` block is activated by `configs/env/small_cap.yaml`—it is **not** active by default.
- Circuit-breaker `action` values: `halt_all` (stop all orders), `reduce_only` (allow only position-reducing orders), `alert_only` (log and notify, no order blocking).
- `cooldown_minutes` must be > 0; setting it to 0 could cause rapid re-trigger loops.

### Common Patterns
```yaml
risk:
  pre_trade:
    max_order_size_usd: 50000       # Per-order nominal cap
    max_position_size_usd: 200000   # Total open position cap
    max_leverage: 10
    min_order_interval_ms: 500      # Anti-spam
    max_open_orders: 20

  real_time:
    max_drawdown_pct: 5.0
    daily_loss_limit_usd: 5000
    trailing_drawdown_pct: 3.0

  circuit_breaker:
    triggers:
      - type: daily_loss
        threshold_usd: 5000
        action: halt_all
        cooldown_minutes: 60
```
- Small-capital 2500U sizing rationale (from inline comments): single order ≤ 25% equity = 625U, total position ≤ 80% equity = 2000U, daily loss ≤ 6% equity = 150U.

## Dependencies

### Internal
- `src.risk.pre_trade.PreTradeRiskManager` — reads `risk.pre_trade.*`
- `src.risk.real_time.RealTimeRiskManager` — reads `risk.real_time.*`
- `src.risk.circuit_breaker.CircuitBreaker` — reads `risk.circuit_breaker.*`
- `src.risk.drawdown_control.DrawdownControl` — reads `risk.real_time.trailing_drawdown_pct`
- `src.risk.post_trade.PostTradeRiskManager` — reads `risk.post_trade.*`
- `src.core.config.load_app_config` — merges into `AppConfig.risk`

### External
- `pydantic` — `RiskConfig` model validation

<!-- MANUAL: -->
