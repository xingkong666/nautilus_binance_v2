# AGENTS.md — src/live/

实盘运行编排：监管器、看门狗、健康探针、预热、就绪检查。

---

## 文件说明

| 文件 | 职责 |
|---|---|
| `supervisor.py` | `LiveSupervisor` — 主协调器；启动/停止所有实盘子服务；响应熔断器和致命错误。 |
| `watchdog.py` | `Watchdog` — 心跳监控；超时时重启适配器。 |
| `health.py` | `LiveHealthProbe` — 定期活性检查（交易所连通性、Redis、PnL 边界）。 |
| `readiness.py` | `ensure_live_readiness()` — `run_live()` 进入前的预检查。 |
| `warmup.py` | `preload_strategies_warmup()` — 拉取历史 K 线，在实盘 K 线到来前预热策略指标。 |
| `account_sync.py` | `AccountSync` — 定期将本地持仓状态与交易所对账。 |

---

## LiveSupervisor 状态机

```
IDLE → STARTING → RUNNING → DEGRADED → STOPPING → STOPPED
```

- `start()` — 在后台线程中启动 `AccountSync`、`Watchdog`、`LiveHealthProbe`；转为 `RUNNING` 状态。
- `stop(timeout)` — 通知所有子服务停止；等待最长 `timeout` 秒。
- 收到 `EventType.CIRCUIT_BREAKER` 时：转为 `DEGRADED` 状态，不再下新订单。
- 遇到不可恢复的错误：转为 `STOPPING` 状态，调用 `container.teardown()`。

---

## ensure_live_readiness（readiness.py）

在任何服务启动前由 `run_live()` 调用。验证：

1. 策略配置文件存在且可正常解析。
2. 至少指定了一个交易对（来自参数或配置）。
3. 环境变量 `CONFIRM_LIVE=YES` 已设置（生产环境门禁）。
4. 环境变量 `SUBMIT_ORDERS` 与配置一致。

返回 `(strategy_config_path, live_symbols)` 或抛出含详细说明的异常。

---

## preload_strategies_warmup（warmup.py）

从 Binance HTTP API 拉取近期历史 K 线，直接喂给策略指标，使其在第一根实盘 K 线到来前已"预热"（收敛）。

- 预热 K 线数量由 `BaseStrategyConfig.live_warmup_bars` 控制。
- 额外追加 `live_warmup_margin_bars` 根 K 线作为安全缓冲。
- 在 `adapter.run()` 之前同步执行。

---

## AccountSync（account_sync.py）

- 每隔 `sync_interval_seconds`（默认 60 秒）轮询交易所账户状态。
- 将实盘持仓与 `SnapshotManager` 状态比对。
- 仅记录偏差日志，**不**自动修正 — 发布 `RiskAlertEvent` 供运维人员处理。

---

## Watchdog（watchdog.py）

- 期望适配器的行情数据流每隔 `heartbeat_interval_seconds` 调用一次心跳。
- 超时时：发布 `RiskAlertEvent(level="ERROR", rule_name="watchdog_timeout")`。
- 由 `LiveSupervisor` 决定是重启还是停机。
