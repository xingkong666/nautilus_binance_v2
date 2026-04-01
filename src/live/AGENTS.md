<!-- Parent: ../AGENTS.md -->
<!-- Generated: 2026-04-01 | Updated: 2026-04-01 -->

# Live

## Purpose
实盘进程管理层，协调所有实盘子服务的完整生命周期。`LiveSupervisor` 作为主控状态机，编排 `AccountSync`、`Watchdog`、`LiveHealthProbe` 等子服务；`Watchdog` 负责心跳监控和自动恢复；`readiness.py` 提供启动前预检逻辑；`warmup.py` 在策略接收实时行情前预注入历史 Bar。系统通过 `EventBus` 订阅熔断/重连事件，任意子服务异常可将状态机推进到 `DEGRADED`（`OrderRouter` 此时拒绝所有新订单）。

## Key Files

| File | Description |
|------|-------------|
| `supervisor.py` | `LiveSupervisor` — 实盘主控状态机，管理所有子服务生命周期；状态：`IDLE→STARTING→RUNNING→DEGRADED→STOPPING→STOPPED` |
| `watchdog.py` | `Watchdog` — 后台线程，监控各子服务心跳（`Watchable` 协议）、系统资源和 EventBus 事件流；支持自动恢复动作 |
| `account_sync.py` | `AccountSync` — 定期通过 Binance REST API 同步账户余额和持仓；软失败设计，同步结果写入 Redis 缓存（TTL = interval_sec + 5）并发布 RECONCILIATION 事件 |
| `health.py` | `LiveHealthProbe` — 实现 `Watchable` 协议，后台定期采集系统状态并向 EventBus 发布 HEALTH_CHECK 事件；可被 Watchdog 监控 |
| `readiness.py` | `ReadinessCheck` — 启动前预检（API Key、策略配置路径解析、稳定币资产校验等），任一项失败则阻止启动 |
| `warmup.py` | 历史 Bar 预热工具，通过 Binance REST API 批量拉取历史 K 线注入策略，避免指标冷启动 |
| `__init__.py` | 模块公开导出：`LiveSupervisor`、`SupervisorState`、`Watchdog` |

## For AI Agents

### Working In This Directory
- `SupervisorState` 枚举定义在 `supervisor.py`，状态流转：`IDLE → STARTING → RUNNING → DEGRADED → STOPPING → STOPPED`
- `DEGRADED` 状态由熔断器触发（订阅 `EventType` 熔断事件），进入后 `OrderRouter` 将拒绝所有新订单
- `Watchdog` 采用 `Watchable` 协议（`is_running` + `last_heartbeat_ns` 属性），任何子服务只需实现该协议即可被监控
- `AccountSync` 软失败设计：单次 REST 调用失败只记录 structlog 日志，不抛出异常，不中断主进程
- `warmup.py` 直接调用 Binance Futures HTTP API（`https://fapi.binance.com` 或 testnet），不经过 NautilusTrader 数据管道
- 新增子服务需在 `supervisor.py` 的 `_start_services()` 中注册，并在 `watchdog.py` 的 `register()` 中挂载心跳监控

### Testing Requirements
- 单元测试路径：`tests/unit/test_supervisor.py`、`tests/unit/test_watchdog.py`
- 使用 `unittest.mock` mock `Container` 依赖；`AccountSync` 需 mock Binance HTTP 客户端
- `readiness.py` 中的路径解析函数（`resolve_strategy_config_path`）可直接单元测试，无需 mock
- 集成测试中 `LiveSupervisor` 需要完整的 `Container`，建议用 testnet 配置

### Common Patterns
```python
# 启动实盘督导进程
from src.live.supervisor import LiveSupervisor

supervisor = LiveSupervisor(container)
supervisor.start()
supervisor.join()  # 阻塞直到 stop() 或致命错误

# 注册子服务到 Watchdog
from src.live.watchdog import Watchdog

watchdog = Watchdog(container, check_interval_sec=10)
watchdog.register("account_sync", my_account_sync)
watchdog.start()

# 历史预热（在 BinanceAdapter.start() 后调用）
from src.live.warmup import warmup_strategy

await warmup_strategy(strategy, bar_type, environment=BinanceEnvironment.TESTNET, lookback=200)
```

## Dependencies

### Internal
- `src.app.container` — 依赖注入容器，提供所有服务单例
- `src.core.events` — `EventBus`、`EventType`、`Event`（订阅熔断/健康/对账事件）
- `src.core.config` — `AppConfig`、`EnvSettings`、`load_yaml`
- `src.core.constants` — `CONFIGS_DIR`
- `src.exchange.binance_adapter` — Binance 客户端（`account_sync.py` 用于 REST 调用）
- `src.cache.redis_client` — 账户余额/持仓快照写入（`account_sync.py`）
- `src.strategy.base` — `BaseStrategy`（`warmup.py` 用于注入历史 Bar）

### External
- `nautilus_trader.adapters.binance` — `BinanceEnvironment`、HTTP URLs
- `nautilus_trader.model.data` — `Bar`、`BarType`
- `httpx` — `warmup.py` 拉取历史 K 线
- `structlog` — 结构化日志
- `threading` — 所有后台服务均运行在独立线程

<!-- MANUAL: -->
