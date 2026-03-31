# AGENTS.md — src/risk/

完整风控链：事前 → 实时 → 熔断器 → 事后。

风控违规**发布 `RiskAlertEvent` — 绝不抛出异常**。

---

## 文件说明

| 文件 | 职责 |
|---|---|
| `pre_trade.py` | `PreTradeRiskManager` — 提交前的订单级检查。 |
| `real_time.py` | `RealTimeRiskMonitor` — 实盘运行期间的持续持仓/PnL 监控。 |
| `drawdown_control.py` | `DrawdownController` — 追踪回撤水位线；触发 WARNING / CRITICAL 告警。 |
| `circuit_breaker.py` | `CircuitBreaker` — 连续亏损、日亏损达限或手动触发时熔断。 |
| `position_sizer.py` | `PositionSizer` — 根据权益和定仓配置计算下单数量。 |
| `post_trade.py` | `PostTradeRiskAnalyzer` — 会话结束后的指标统计：夏普比率、最大回撤、胜率。 |

---

## PreTradeRiskManager 检查项

通过 `configs/risk/global_risk.yaml` → `risk.pre_trade.*` 配置：

| 检查项 | 配置键 | 默认值 |
|---|---|---|
| 最大订单名义价值（USD） | `max_order_size_usd` | 50 000 |
| 最大总持仓规模（USD） | `max_position_size_usd` | 200 000 |
| 最大杠杆 | `max_leverage` | 10 |
| 最小下单间隔（毫秒） | `min_order_interval_ms` | 500 |
| 最大挂单数 | `max_open_orders` | 20 |

`check(intent, current_price)` 返回 `PreTradeCheckResult(passed, reason)`。失败时发布 `RiskAlertEvent(level="WARNING", rule_name=<check>)`。

---

## DrawdownController

- `warning_pct`（默认 3%）：发布 `RiskAlertEvent(level="WARNING")`。
- `critical_pct`（默认 5%）：发布 `RiskAlertEvent(level="CRITICAL")` — `LiveSupervisor` 响应后暂停新订单。
- 基于权益的追踪高水位线；通过 `reset_watermark()` 显式重置。

---

## CircuitBreaker

以下情况触发熔断（状态 → `TRIPPED`）：
- 连续亏损订单数 ≥ `max_consecutive_losses`（默认 5）
- 当日已实现亏损 ≥ `daily_loss_limit_usd`
- 手动调用 `trip()`

熔断后：
- 发布 `EventType.CIRCUIT_BREAKER` 事件。
- `SignalProcessor` 在路由前检查 `is_tripped()` — 所有新信号被阻断。
- 通过 `reset()` 重置（手动或新交易日时）。

Redis 可用时用于跨进程状态同步；否则回退到进程内状态。

---

## PositionSizer

`compute_quantity(equity, price, sizing_config)` → `Decimal` 数量。

定仓模式（来自 `BaseStrategyConfig`）：
1. `capital_pct_per_trade` — `(权益 × pct / 100) / 价格`
2. `gross_exposure_pct_per_trade` — 名义价值占权益比
3. `margin_pct_per_trade` — `(权益 × pct / 100 × 杠杆) / 价格`
4. `trade_size` — 固定数量兜底

---

## 新增风控规则

1. 将检查项添加到 `PreTradeRiskManager.check()` 或在 `src/monitoring/watchers.py` 中创建新的 Watcher。
2. 违规时：`self._event_bus.publish(RiskAlertEvent(...))` — 禁止抛出异常。
3. 在 `configs/risk/global_risk.yaml` 中添加新配置键和默认值。
4. 在 `tests/unit/test_pre_trade_risk.py` 或相关测试文件中添加单元测试。
