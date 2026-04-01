<!-- Parent: ../AGENTS.md -->
<!-- Generated: 2026-04-01 | Updated: 2026-04-01 -->

# portfolio

## Purpose
Multi-strategy capital allocation layer. `PortfolioAllocator` sits between the `EventBus` (receiving `SignalEvent`s) and the execution pipeline. It determines how much capital each strategy may deploy given total account equity, current margin usage, and the configured allocation mode. It does **not** route orders directly — it emits sized `OrderIntent`s consumed downstream by `PreTradeRisk` and `OrderRouter`.

## Key Files

| File | Description |
|------|-------------|
| `allocator.py` | `PortfolioAllocator` — core allocator. Holds a list of `StrategyAllocation` configs and computes `AllocationResult` per strategy. Three modes: `equal` (uniform split), `weight` (proportional to `StrategyAllocation.weight`), `risk_parity` (equal risk contribution weighted by inverse volatility). Enforces `max_allocation_pct` cap per strategy. Produces re-balance `OrderIntent` lists via `rebalance()` |

## For AI Agents

### Working In This Directory

- **Allocation modes**: `equal`, `weight`, `risk_parity`. Mode is set at construction time from `AppConfig`; switching modes at runtime requires rebuilding the allocator.
- `StrategyAllocation.enabled = False` excludes a strategy from all allocation computations — useful for pausing a strategy without removing its config.
- `max_allocation_pct = 0.0` means **no cap** (unlimited within the mode's computed share).
- `AllocationResult.available_capital` = allocated capital minus currently used margin for that strategy. Use this value — not `allocated_capital` — when sizing new orders.
- `rebalance()` returns a list of `OrderIntent`s (using `reduce_only=True` for size-down intents). These must still pass through the normal `PreTradeRisk → OrderRouter` pipeline.
- `risk_parity` mode requires volatility estimates passed in as `vol_map: dict[str, float]`. If a strategy's volatility is missing or zero, it defaults to equal weight for that strategy.
- All capital values are `Decimal` in USDT; weights are `float` — convert carefully when mixing types.
- Import path: `from src.portfolio.allocator import PortfolioAllocator, StrategyAllocation, AllocationResult`.

### Testing Requirements

- Test `equal` mode: verify each enabled strategy receives `total_capital / N`, disabled strategies receive `Decimal(0)`.
- Test `weight` mode: verify allocations are proportional to weights and sum to `total_capital`.
- Test `risk_parity` mode: supply known `vol_map`; verify inverse-vol weighting.
- Test `max_allocation_pct` cap: verify no strategy exceeds its cap even when weights would produce a larger share.
- Test `rebalance()`: mock current positions; verify returned `OrderIntent`s have correct `reduce_only` flags and quantities.

### Common Patterns

```python
from decimal import Decimal
from src.portfolio.allocator import PortfolioAllocator, StrategyAllocation

allocations = [
    StrategyAllocation(strategy_id="ema_cross", weight=1.0, enabled=True),
    StrategyAllocation(strategy_id="breakout",  weight=2.0, max_allocation_pct=40.0),
]
allocator = PortfolioAllocator(allocations=allocations, mode="weight")

results = allocator.allocate(total_capital=Decimal("100000"), margin_used={})
for r in results:
    print(r.strategy_id, r.allocated_capital, r.available_capital)
```

## Dependencies

### Internal
- `src.execution.order_intent` — `OrderIntent` (produced by `rebalance()`)
- `src.core.events` — `EventBus`, `SignalEvent` (allocator subscribes to signals)

### External
- `structlog` — structured logging
- `decimal` — all capital arithmetic (`ROUND_DOWN` for lot-size compliance)

<!-- MANUAL: -->
