# 风控体系

风控分为三层，互相独立，任意一层均可单独触发熔断。配置文件：`configs/risk/global_risk.yaml`。

---

## 整体架构

```
下单请求
    │
    ▼
┌─────────────────────┐
│   PreTradeRisk      │  ← 事前：准入校验
│  （每笔订单执行前）   │
└──────────┬──────────┘
           │ 通过
           ▼
┌─────────────────────┐
│   执行层             │
│  （下单 → 成交）     │
└──────────┬──────────┘
           │ 并行监控
           ▼
┌─────────────────────┐
│   RealTimeRisk      │  ← 实时：持续巡检
│  （每个 bar/tick）   │
└──────────┬──────────┘
           │ 条件触发
           ▼
┌─────────────────────┐
│   CircuitBreaker    │  ← 熔断：紧急制动
└──────────┬──────────┘
           │ 成交后
           ▼
┌─────────────────────┐
│   PostTradeRisk     │  ← 事后：归因分析
└─────────────────────┘
```

---

## 事前风控（PreTradeRisk）

每笔订单下单前同步校验，不通过则直接拒绝，不发出任何网络请求。

| 参数 | 默认值 | 说明 |
|---|---|---|
| `max_order_size_usd` | 50,000 | 单笔订单最大名义价值 |
| `max_position_size_usd` | 200,000 | 单个标的最大持仓名义价值 |
| `max_leverage` | 10 | 最大允许杠杆倍数 |
| `min_order_interval_ms` | 500 | 同一标的最小下单间隔（防刷单） |
| `max_open_orders` | 20 | 最大在途订单数 |

风控模式（`configs/env/*.yaml` 中的 `risk.mode`）：

- `soft` — 违规只告警，不阻断（适合开发 / 回测验证）
- `hard` — 违规直接拒绝订单（生产必须设为 hard）

---

## 实时风控（RealTimeRisk + DrawdownControl）

后台定时巡检（每个 bar 触发一次），监控整体账户健康状态。

| 参数 | 默认值 | 说明 |
|---|---|---|
| `max_drawdown_pct` | 5.0% | 从峰值回撤超过此值触发熔断 |
| `daily_loss_limit_usd` | 5,000 | 当日亏损超过此值触发熔断 |
| `trailing_drawdown_pct` | 3.0% | 追踪回撤（动态峰值更新） |

**DrawdownControl** 在回撤接近阈值时（默认 80%）先触发降仓，而不是等到触发熔断时才减仓。

---

## 熔断机制（CircuitBreaker）

三种触发类型，每种可独立配置 action 和冷却时间：

### 触发类型

| 类型 | 默认阈值 | 说明 |
|---|---|---|
| `daily_loss` | 5,000 USDT | 当日已实现亏损 |
| `drawdown` | 5.0% | 账户净值回撤 |
| `rapid_loss` | 10 分钟内 3 次亏损 | 连续快速亏损（防止策略失控） |

### 熔断 Action

| Action | 行为 |
|---|---|
| `halt_all` | 拒绝所有新订单，Supervisor 进入 DEGRADED 状态 |
| `reduce_only` | 只允许平仓方向的订单，禁止开仓 |
| `alert_only` | 仅发出告警，不影响交易（用于预警） |

### 冷却期

熔断触发后进入冷却期，期间即使条件消失也不自动恢复：

- `daily_loss` 触发：冷却 60 分钟
- `drawdown` 触发：冷却 120 分钟
- `rapid_loss` 触发：冷却 30 分钟

冷却期结束后需**手动确认**才能恢复交易（防止自动重启放大损失）。

---

## 事后风控（PostTradeRisk）

每笔成交后异步执行，不阻塞执行链路。

- **滑点追踪** — 实际成交价 vs 信号时价格，统计滑点分布
- **PnL 归因** — 按策略、标的、时段拆分盈亏来源
- **对账** — 每 15 分钟对比本地状态与交易所实际持仓（可配置）

对账发现不一致时发出 `CRITICAL` 级别告警，详见 [monitoring.md](monitoring.md)。

---

## 仓位计算（PositionSizer）

`PositionSizer` 根据信号强度和账户状态计算最终下单量：

```python
# 简化逻辑
raw_size = signal.strength * base_size
risk_adjusted = min(raw_size, max_position_size / current_price)
final_size = PreTradeRisk.validate(risk_adjusted)
```

多策略场景下，`PortfolioAllocator` 先按分配权重缩放各策略的 `base_size`，再走 PositionSizer。

---

## 常见问题

**Q: 熔断触发后如何恢复？**

1. 等待冷却期结束
2. 检查触发原因（查日志 / Telegram 告警）
3. 手动调用 `Supervisor.reset()` 或重启节点

**Q: 回测时风控会生效吗？**

会，但默认 `mode: soft`（只告警不阻断）。回测完成后建议查看风控日志，确认策略在风控约束下的实际行为。

**Q: 如何临时关闭某个熔断触发器？**

在 `configs/risk/global_risk.yaml` 中将对应 trigger 的 `action` 改为 `alert_only`，然后重载配置（或重启节点）。不建议直接关闭，优先用 `alert_only` 观察。
