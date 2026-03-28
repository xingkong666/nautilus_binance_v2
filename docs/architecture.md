# 系统架构

## 设计原则

1. **策略只产信号**，不直接调用交易所接口
2. **关注点分离** — 执行 / 风控 / 监控三层完全解耦，可独立替换
3. **事件驱动** — 所有跨模块通信通过 EventBus，不直接调用彼此方法
4. **可观测优先** — 每个关键路径都有 Prometheus 指标 + 结构化日志
5. **崩溃安全** — 快照 + 对账 + 自动恢复，节点重启不丢状态

---

## 整体架构

```
┌─────────────────────────────────────────────────────────┐
│                    Bootstrap / Container                 │
│         （组装所有模块，管理生命周期）                      │
└────────────────────────┬────────────────────────────────┘
                         │
          ┌──────────────┼──────────────┐
          ▼              ▼              ▼
   ┌─────────────┐ ┌──────────┐ ┌───────────────┐
   │  Strategy   │ │   Risk   │ │   Monitoring  │
   │  (信号层)   │ │  (风控层) │ │   (监控层)    │
   └──────┬──────┘ └────┬─────┘ └───────────────┘
          │ Signal       │ Block/Alert
          ▼              ▼
   ┌─────────────────────────┐
   │       EventBus          │
   │   (模块间解耦通信)       │
   └──────────┬──────────────┘
              │
              ▼
   ┌─────────────────────────┐
   │      Execution          │
   │  OrderRouter → Algo     │
   │  RateLimiter → Fill     │
   └──────────┬──────────────┘
              │
              ▼
   ┌─────────────────────────┐
   │    BinanceAdapter       │
   │  (NautilusTrader 封装)  │
   └──────────┬──────────────┘
              │
              ▼
         Binance API
```

---

## 模块职责

### `src/app/` — 应用组装层

| 文件 | 职责 |
|---|---|
| `container.py` | 依赖注入容器，组装所有模块实例，管理生命周期 |
| `factory.py` | 创建 BinanceAdapter、策略等组件的工厂函数 |
| `bootstrap.py` | 标准 live 入口：读配置 → 恢复状态 → 标记忽略交易对 → 启动 TradingNode |

### `src/core/` — 基础设施

| 文件 | 职责 |
|---|---|
| `config.py` | Pydantic Settings，从 YAML + 环境变量加载配置 |
| `events.py` | EventBus（发布/订阅），所有跨模块事件定义 |
| `logging.py` | structlog 封装，JSON 结构化日志 |
| `time_sync.py` | 本地时钟与交易所时钟同步 |
| `constants.py` | 全局常量（交易所名、venue 名等） |

### `src/strategy/` — 策略层

策略**只做决策**，通过 `SignalEvent` 发布信号，不持有任何执行状态。

| 文件 | 职责 |
|---|---|
| `base.py` | 策略基类，定义 `on_bar / on_signal` 接口 |
| `signal.py` | SignalEvent 数据类（方向、强度、元数据） |
| `ema_cross.py` | EMA 双均线策略（fast/slow period 可配置） |
| `vegas_tunnel.py` | Vegas 隧道策略（EMA12/36 + EMA144/169 + Fib 分批止盈） |

### `src/execution/` — 执行层

接收 SignalEvent，完成从信号到成交的全过程。

```
SignalEvent
    → OrderIntent（意图，含仓位大小）
    → IgnoredInstrumentRegistry（外部活动交易对过滤）
    → PreTradeRisk 校验
    → OrderRouter（路由到 BinanceAdapter）
    → AlgoExecution（TWAP / 市价 等算法）
    → RateLimiter（频率保护）
    → FillHandler（成交记录 + 归因）
```

### `src/risk/` — 风控层

三层风控，互相独立，均可单独触发熔断：

```
PreTradeRisk    → 下单前检查（仓位 / 杠杆 / 订单大小）
RealTimeRisk    → 持仓中监控（回撤 / 日亏损）
CircuitBreaker  → 熔断（halt_all / reduce_only / alert_only）
DrawdownControl → 追踪回撤，动态降仓
PostTradeRisk   → 成交后分析（滑点 / PnL 归因）
```

### `src/portfolio/` — 多策略分配

`PortfolioAllocator` 管理多个策略实例的权重与资金分配：

- `equal` — 平均分配
- `weight` — 按配置权重分配
- `risk_parity` — 按波动率倒数分配（风险平价）

支持运行时动态启停单个策略，不影响其他策略。

### `src/state/` — 状态持久化

| 文件 | 职责 |
|---|---|
| `persistence.py` | SQLite 持久化（订单 / 成交 / 仓位） |
| `snapshot.py` | 内存状态快照，按环境隔离序列化到磁盘 |
| `reconciliation.py` | 本地状态 vs 交易所实际状态对账，输出差异报告 |
| `recovery.py` | 节点重启时从快照 / 交易所真值恢复状态 |

### `src/live/` — 实盘守护

| 文件 | 职责 |
|---|---|
| `supervisor.py` | 实盘守护核心，状态机（IDLE→RUNNING→DEGRADED→STOPPED） |
| `watchdog.py` | 心跳检测，超时自动重启 |
| `account_sync.py` | 定期同步账户余额 / 持仓 / open orders，并识别外部活动交易对 |
| `health.py` | 系统健康状态聚合 |

### `src/monitoring/` — 可观测性

| 文件 | 职责 |
|---|---|
| `metrics.py` | Prometheus Counter / Gauge / Histogram 定义 |
| `prometheus_server.py` | 暴露 `/metrics` HTTP 端点（默认 :9100） |
| `health_server.py` | 暴露 `/health` HTTP 端点（默认 :8080） |
| `alerting.py` | 告警规则引擎，触发条件 → 通知 |
| `notifier/` | Telegram / Slack 通知实现 |
| `watchers.py` | 已落地 RiskAlert / Drawdown / FillLatency 三类 watcher |

### `src/exchange/` — 交易所适配

`BinanceAdapter` 封装 NautilusTrader `TradingNode`：

- 支持 `LIVE / TESTNET / DEMO` 三种环境
- API Key 从环境变量读取，支持 fallback
- 异步 `start() / stop()`，干净的生命周期管理
- 可查询账户真实持仓、余额与当前 open orders
- 启动前自动探测 Binance 账户模式；若为 Hedge Mode，则关闭 `reduce_only`
- 启动时自动执行对账（`reconciliation=True`）

---

## 数据流

### 行情 → 信号

```
Binance WS (tick/bar)
    → NautilusTrader DataEngine
    → Strategy.on_bar()
    → 指标计算（EMA 等）
    → 触发条件满足 → emit SignalEvent
```

### 信号 → 成交

```
SignalEvent
    → IgnoredInstrumentRegistry（若该交易对存在外部持仓/外部挂单则直接丢弃）
    → PortfolioAllocator（仓位分配）
    → PreTradeRisk（准入校验）
    → OrderIntent（意图构建）
    → OrderRouter（路由）
    → AlgoExecution（执行算法）
    → BinanceAdapter → Binance API
    → OrderFilled → FillHandler（归因 + 持久化）
    → PostTradeRisk（事后分析）
```

### 风控触发 → 熔断

```
RealTimeRisk 监控 (每个 bar / tick)
    → 条件触发（日亏 / 回撤）
    → CircuitBreaker.trigger()
    → emit CircuitBreakerEvent
    → Supervisor 接收 → 状态 DEGRADED
    → OrderRouter 拒绝新订单
    → Alerting → Telegram 推送
```

### 启动恢复 → 外部活动隔离

```
bootstrap
    → 查询账户模式（Hedge / One-way）
    → 拉取交易所余额 / 持仓 / open orders
    → RecoveryManager 以交易所真值恢复快照
    → IgnoredInstrumentRegistry 标记外部活动交易对
    → TradingNode 启动
    → SignalProcessor 对被忽略交易对拒绝下单
```

---

## 配置层次

```
环境变量（最高优先级）
    └── configs/env/{dev,stage,prod}.yaml（环境差异）
            └── configs/{risk,strategies,...}.yaml（模块配置）
                    └── 代码默认值（最低优先级）
```

所有配置通过 `src/core/config.py` 的 Pydantic Settings 加载，类型安全，启动时校验失败则拒绝启动。
