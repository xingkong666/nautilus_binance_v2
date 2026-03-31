# AGENTS.md — src/app/

应用组装层：依赖注入、对象工厂、启动引导入口。

---

## 文件说明

| 文件 | 职责 |
|---|---|
| `bootstrap.py` | 统一启动入口。`bootstrap()` → 裸 `Container`；`bootstrap_app()` → `AppContext`；`bootstrap_context()` → 上下文管理器形式；`run_live()` 组装完整实盘管道。 |
| `container.py` | `Container` — 所有服务单例的唯一持有者。`build()` 按依赖顺序初始化；`teardown()` 逆序清理。 |
| `factory.py` | `AppFactory` — 创建策略、`BacktestRunner`、`BinanceAdapter`。无状态，依赖 `Container` 获取配置和单例。 |

---

## Container 构建顺序

1. `EventBus`（若 monitoring 启用则挂载 Prometheus 钩子）
2. `TradePersistence`、`SnapshotManager`
3. `RedisClient`（失败时降级运行，不致命）
4. `RateLimiter`、`IgnoredInstrumentRegistry`、`PositionSizer`
5. `PreTradeRiskManager`、`CircuitBreaker`、`DrawdownController`、`RealTimeRiskMonitor`
6. `PortfolioAllocator`（仅当配置中存在 `strategies.portfolio` 块时）
7. `BinanceAdapter`（仅 `prod`/`staging` 或配置中存在 `exchange:` 块时）
8. `FillHandler`、`OrderRouter`、`SignalProcessor`
9. `AlertManager`、各 Watcher
10. `PrometheusServer`、`HealthServer`（仅当 `monitoring.enabled=True`）

---

## 策略注册表（`bootstrap.py` 中的 `_STRATEGY_REGISTRY`）

将 YAML 中的策略 `name` 映射到 `(策略类, 配置类)`：

| 键名 | 策略类 | 配置类 |
|---|---|---|
| `ema_cross` | `EMACrossStrategy` | `EMACrossConfig` |
| `ema_pullback_atr` | `EMAPullbackATRStrategy` | `EMAPullbackATRConfig` |
| `turtle` | `TurtleStrategy` | `TurtleConfig` |
| `micro_scalp` | `MicroScalpStrategy` | `MicroScalpConfig` |
| `vegas_tunnel` | `VegasTunnelStrategy` | `VegasTunnelConfig` |

新增策略时：在 `_STRATEGY_REGISTRY` 中注册，**并**在 `AppFactory` 中添加对应的 `create_<name>_strategy()` 方法。

---

## 关键模式

- **禁止在 `Container.build()` 之外实例化服务** — 所有单例均由容器持有。
- `AppFactory` 是**无状态**的，读取 `container.config` 并委托构建。
- `bootstrap_context()` 是脚本和测试推荐的形式，可保证 `teardown()` 被调用。
- `run_live()` 是实盘模式的入口：依次调用 `ensure_live_readiness()`、构建策略、连接适配器、引导状态，最后调用阻塞式 `adapter.run()`。
- `_bootstrap_live_state()` 在节点启动前从交易所拉取真实持仓/挂单，并将已有持仓加入 `IgnoredInstrumentRegistry`。

---

## 常见陷阱

- 在异步上下文中，应先 `await adapter.stop()` 再调用 `container.teardown()`；顺序颠倒会记录警告。
- `bootstrap()` 是惰性的 — **不会**启动 Prometheus 或 HealthServer；只有 `Container.build()` 在 `monitoring.enabled=True` 时才会启动。
- 容器中的 `BinanceAdapter` 构建时**未启动**；需先调用 `adapter.prepare_runtime_config()` 和 `adapter.build_node()`，再调用 `adapter.run()`。
