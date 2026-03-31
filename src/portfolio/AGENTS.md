# AGENTS.md — src/portfolio/

多策略资金分配。

---

## 文件说明

| 文件 | 职责 |
|---|---|
| `allocator.py` | `PortfolioAllocator` — 将总权益分配给各策略，生成每个策略的资金上限。 |

---

## 分配模式

| 模式 | 说明 |
|---|---|
| `equal` | 每个启用的策略获得 `总资金 / N`。 |
| `weight` | 资金按 `StrategyAllocation.weight` 比例分配。 |
| `risk_parity` | 风险贡献相等；权重与近期波动率成反比。 |

---

## 关键类

**`StrategyAllocation`**（数据类）
- `strategy_id: str`
- `weight: float` — 相对权重（`equal` 模式下忽略）
- `max_allocation_pct: float` — 占总资金的硬上限（%）；`0` 表示无上限
- `enabled: bool`

**`AllocationResult`**（数据类）
- `strategy_id: str`
- `allocated_capital: Decimal` — USDT 金额
- `allocation_pct: float` — 0–1 的分配比例
- `available_capital: Decimal` — 扣除已用保证金后的可用资金

**`PortfolioAllocator`**
- `allocate(total_equity, used_margin_by_strategy)` → `list[AllocationResult]`
- `reserve_pct`（配置，默认 5%）— 保留一部分权益作为缓冲，不分配给任何策略
- `min_allocation`（配置，默认 "100"）— 每个策略的最小 USDT 分配额；低于此值的策略被跳过

---

## 配置（env YAML 中的 `strategies.portfolio` 块）

```yaml
strategies:
  portfolio:
    mode: equal          # equal | weight | risk_parity
    reserve_pct: 5.0
    min_allocation: "100"
    strategies:
      - strategy_id: ema_cross
        weight: 1.0
        max_allocation_pct: 40.0
        enabled: true
```

`PortfolioAllocator` 是可选的 — 若配置中无 `strategies.portfolio`，则 `Container._portfolio_allocator` 为 `None`，系统以单策略模式运行。

---

## 集成说明

`PortfolioAllocator` 由 `SignalProcessor`（或专用的再平衡钩子）在定仓前查询，以获取每个策略的资金上限，结果传入 `PositionSizer`。
