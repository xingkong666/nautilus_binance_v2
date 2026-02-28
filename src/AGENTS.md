# SRC 目录知识库

## 概览
`src/` 包含生产运行核心逻辑。请保持领域边界清晰，并遵循 EventBus 驱动的交互方式。

## 目录结构
```text
src/
├── app/         # container/factory/bootstrap 生命周期
├── core/        # config/events/logging/constants
├── strategy/    # 仅产出信号的策略层
├── execution/   # 信号到订单的执行流水线
├── risk/        # pre/realtime/breaker/post 风控
├── monitoring/  # 指标、watchers、notifiers
├── state/       # 持久化/快照/恢复
├── live/        # supervisor/watchdog/账户同步
├── backtest/    # 回测 runner/report
├── exchange/    # Binance 适配层
└── portfolio/   # 多策略资金分配
```

## 快速定位
| 任务 | 位置 | 说明 |
|---|---|---|
| 依赖装配关系 | `src/app/container.py` | 组装中心与生命周期顺序 |
| 安全启动入口 | `src/app/bootstrap.py` | 优先使用 `bootstrap_context(...)` |
| 配置合并语义 | `src/core/config.py` | 优先级与类型化配置模型 |
| 事件契约 | `src/core/events.py` | 事件类型与载荷结构 |
| 信号发布模式 | `src/strategy/base.py` | 策略只发事件，不直接下单 |
| 执行路径 | `src/execution/` | intent → routing → algo → fill |
| 风控与告警 | `src/risk/` | 风控事件与 breaker 联动 |
| 告警 watcher 管道 | `src/monitoring/watchers.py` | EventBus 订阅与 alert manager |
| 状态持久化 | `src/state/persistence.py`, `src/state/snapshot.py` | PostgreSQL 表与快照序列化 |

## 约定
- 跨模块通信统一走 `EventBus`。
- 延续 `structlog` key/value 结构化日志风格。
- 策略层保持无交易所 API 副作用。
- 事件载荷与配置单元使用明确的数据模型（dataclass/model）。

## 反模式
- 不要绕过 `src/core/events.py` 做跨模块直接调用。
- 不要把订单执行逻辑放进 `src/strategy/`。
- 不要在运行时代码里用临时拼装替代 container/bootstrap。

## 备注
- `src/monitoring/notifier/` 在 Slack/Telegram 后端行为不同，但共享同一 notifier 基类契约。
- `src/app/bootstrap.py` 负责进程信号关闭处理，清理流程要保持确定性。
