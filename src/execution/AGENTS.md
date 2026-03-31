# AGENTS.md — src/execution/

信号转订单管道。将策略信号桥接到交易所适配器。

管道：`SignalEvent` → `SignalProcessor` → `PreTradeRisk` → `OrderRouter` → `Strategy.submit_order()`

---

## 文件说明

| 文件 | 职责 |
|---|---|
| `signal_processor.py` | `SignalProcessor` — 订阅 `EventType.SIGNAL`，转换为 `OrderIntent`，执行频率限制和忽略交易对检查，然后调用 `OrderRouter.route()`。 |
| `order_router.py` | `OrderRouter` — 将 `OrderIntent` 转换为 Nautilus 订单对象并调用 `strategy.submit_order()`。`submit_orders=False` 时空跑。 |
| `order_intent.py` | `OrderIntent` 数据类 — 提交前的订单内部表示。 |
| `algo.py` | `AlgoExecution` — 大单的 TWAP/VWAP/冰山拆单执行。 |
| `fill_handler.py` | `FillHandler` — 订阅 `ORDER_FILLED` 事件，持久化成交记录，更新持仓状态。 |
| `ignored_instruments.py` | `IgnoredInstrumentRegistry` — 运行时跳过交易对的集合；启动时根据外部持仓/挂单填充。 |
| `rate_limiter.py` | `RateLimiter` — 基于令牌桶的每交易对频率限制；多进程部署时由 Redis 支持。 |
| `cost_model.py` | 手续费和滑点成本模型（供回测使用）。 |
| `slippage.py` | 市场冲击/滑点估算器。 |

---

## SignalProcessor 处理流程

```
on_signal(SignalEvent)
  → _to_intent()           # 转换为 OrderIntent
  → _is_ignored()          # IgnoredInstrumentRegistry 检查
  → _check_rate_limit()    # RateLimiter 检查
  → pre_trade_risk.check() # PreTradeRiskManager 检查
  → order_router.route()   # 提交或空跑
```

任何一步失败都会发布 `RiskAlertEvent` 并直接返回，不继续后续步骤。

---

## OrderRouter

- `bind_strategy(strategy)` — 在任何 `route()` 调用前必须先调用；为给定 `instrument_id` 注册策略。
- `route(intent)` — 按 `instrument_id` 找到目标策略，调用 `strategy.submit_order()`。
- `submit_orders=False` 时仍会验证并发布 `ORDER_SUBMITTED` 事件 — 适用于空跑/模拟交易。
- 支持多个策略绑定；路由器按 `intent.instrument_id` 分发。

---

## IgnoredInstrumentRegistry

启动时因外部持仓或挂单而加入此处的交易对，在 `SignalProcessor` 中会被静默跳过，防止系统与外部手动交易相冲突。

```python
registry.ignore(
    instrument_id="BTCUSDT-PERP.BINANCE",
    reason="existing_exchange_position_on_startup",
    source="bootstrap",
)
registry.is_ignored("BTCUSDT-PERP.BINANCE")  # True
```

---

## RateLimiter

- 通过 `configs/execution/execution.yaml` → `execution.rate_limit` 配置。
- Redis 不可用时回退到进程内令牌桶。
- 关键字段：`max_orders_per_second`、`max_orders_per_minute`、`burst_size`。

---

## 关键不变量

- `OrderIntent` 构造后不可变。
- `route()` 返回 `bool` — `True` 表示订单已（或将）提交。
- 禁止从 `src.strategy.*` 导入 — 信号单向流入执行层。
