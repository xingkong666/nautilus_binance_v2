<!-- Parent: ../AGENTS.md -->
<!-- Generated: 2026-04-01 | Updated: 2026-04-01 -->

# configs/execution/

## Purpose
Execution engine configuration controlling how orders are constructed, priced, and submitted to Binance. Covers order defaults (time-in-force, order type), slippage and cost models used in backtesting, funding-rate analysis toggle, rate limiting parameters, and the optional algorithmic execution layer (TWAP/VWAP/Iceberg). The `submit_orders` flag is the master kill-switch: set to `false` to run the full signal/risk chain without placing real orders.

## Key Files

| File | Description |
|------|-------------|
| `execution.yaml` | Master execution config: order defaults, slippage model, cost model, funding toggle, rate limits, algo execution |

## For AI Agents

### Working In This Directory
- `submit_orders: false` is the safest way to dry-run the full pipeline without real exchange impact.
- `slippage.model` options: `fixed` (constant bps), `volume_based` (size-proportional), `historical` (from recorded data).
- `cost.maker_fee_bps` / `taker_fee_bps` must match the actual Binance fee tier for accurate backtest P&L.
- `algo.default_algo: twap` enables TWAP splitting of large orders; requires `algo.enabled: true`.
- Rate-limit values (`max_orders_per_second`, `max_orders_per_minute`, `burst_size`) must stay within Binance API limits to avoid IP bans.

### Common Patterns
```yaml
execution:
  submit_orders: true
  default_time_in_force: GTC
  default_order_type: MARKET

  slippage:
    model: fixed
    fixed_bps: 2

  cost:
    maker_fee_bps: 2
    taker_fee_bps: 4

  rate_limit:
    max_orders_per_second: 5
    max_orders_per_minute: 100
    burst_size: 10

  algo:
    enabled: false
    default_algo: null   # twap | vwap | iceberg
```
- Override `submit_orders: false` in `configs/env/dev.yaml` during initial testing of a new strategy.
- Funding rate analysis (`funding.enabled: true`) fetches 8-hourly rates and includes them in PnL attribution.

## Dependencies

### Internal
- `src.execution.order_router.OrderRouter` — reads `execution.submit_orders`, `execution.rate_limit.*`
- `src.execution.algo_execution.AlgoExecution` — reads `execution.algo.*`
- `src.risk.post_trade.PostTradeRiskManager` — reads `execution.cost.*` for slippage analysis
- `src.backtest.runner.BacktestRunner` — reads `execution.slippage.*` and `execution.cost.*`
- `src.core.config.load_app_config` — merges into `AppConfig.execution`

### External
- `pydantic` — `ExecutionConfig` model validation
- Binance REST API — subject to the rate limits configured here

<!-- MANUAL: -->
