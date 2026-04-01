# AGENTS.md — src/

所有生产代码的源码根目录。每个子包是一个独立的有界上下文，职责单一。

---

## 包结构一览

| 包 | 职责 | 关键文件 |
|---|---|---|
| `app/` | 依赖注入连接、启动引导、工厂 | `bootstrap.py`、`container.py`、`factory.py` |
| `core/` | 共享基础类型（不依赖其他 src 包） | `config.py`、`events.py`、`enums.py`、`constants.py` |
| `strategy/` | 仅负责产出信号 — 绝不提交订单 | `base.py`、`ema_cross.py`、`turtle.py`、`vegas_tunnel.py` |
| `execution/` | 信号→意图→订单管道 | `signal_processor.py`、`order_router.py`、`algo.py` |
| `risk/` | 事前 / 实时 / 事后风控、熔断器 | `pre_trade.py`、`real_time.py`、`circuit_breaker.py` |
| `portfolio/` | 多策略资金分配 | `allocator.py` |
| `state/` | 快照、持久化、对账、崩溃恢复 | `snapshot.py`、`persistence.py`、`reconciliation.py` |
| `live/` | 实盘编排与健康探针 | `supervisor.py`、`watchdog.py`、`readiness.py` |
| `monitoring/` | Prometheus 指标、HTTP 健康接口、告警 | `metrics.py`、`alerting.py`、`health_server.py` |
| `backtest/` | 回测引擎封装、滚动优化、市场状态检测 | `runner.py`、`walkforward.py`、`regime.py` |
| `data/` | 数据加载、特征仓库、校验器 | `loaders.py`、`feature_store.py`、`validators.py` |
| `exchange/` | Binance 合约适配器（NautilusTrader 胶水层） | `binance_adapter.py` |
| `cache/` | Redis 客户端封装 | `redis_client.py` |

---

## 依赖方向（严格单向，禁止循环）

```
core  ←  strategy  ←  execution  ←  app
              ↓              ↓
            risk          portfolio
              ↓              ↓
            state         monitoring
              ↓
            live
              ↓
           exchange
              ↓
            cache
```

`core` **零内部依赖**。其他所有包均可导入 `core`。
`exchange` 和 `cache` 是叶子节点 — `src/` 内部只有 `app/` 和 `live/` 可以导入它们。

**⚠ 已知偏差**：`risk/real_time.py`、`risk/circuit_breaker.py`、`execution/rate_limiter.py` 直接导入 `cache.RedisClient`，绕过了 Container 注入。当前无循环风险（`RedisClient` 仅依赖 `core`），但违反了 "只有 `app/` 和 `live/` 可导入 `cache`" 的约束。待决定：更新规则以反映现实，或重构为通过 Container 注入。

---

## 不变量

- `from __future__ import annotations` 是每个模块的**第一行**。
- 禁止相对导入，始终使用 `from src.<包>.<模块> import ...`。
- 禁止同层包之间产生循环引用。
- 财务数值始终使用 `decimal.Decimal`，禁止 `float`。
- 结构化日志：模块级 `logger = structlog.get_logger()`。
