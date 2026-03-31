# AGENTS.md — src/core/

共享基础类型。**不依赖任何其他 `src.*` 包。** 其余所有包均可从此处导入。

---

## 文件说明

| 文件 | 职责 |
|---|---|
| `config.py` | `AppConfig` 及所有子配置（Pydantic v2）。`load_app_config()` 合并多层 YAML + 环境变量。 |
| `events.py` | `EventBus`、`Event`、`SignalEvent`、`OrderIntentEvent`、`RiskAlertEvent`、`EventType`、`SignalDirection`。 |
| `enums.py` | `Interval` 枚举 + `INTERVAL_TO_NAUTILUS` 到 NautilusTrader K 线类型字符串的映射。 |
| `constants.py` | `BASE_DIR`、`CONFIGS_DIR` 及其他路径常量。 |
| `logging.py` | `setup_logging()` — 配置 structlog，支持 JSON 或控制台渲染器。 |
| `nautilus_cache.py` | `build_nautilus_cache_settings()` — 构建实盘/回测模式的 `NautilusCacheConfig`。 |
| `time_sync.py` | 轻量级时间工具函数（UTC 辅助函数）。 |

---

## 配置分层（`load_app_config`）

优先级从高到低：

1. 环境变量（通过 `EnvSettings` 读取 `.env` 文件）
2. `configs/env/<env>.yaml`（按环境的覆盖配置）
3. `configs/risk/global_risk.yaml`、`configs/execution/execution.yaml`、`configs/accounts/binance_futures.yaml`、`configs/monitoring/alerts.yaml`
4. Pydantic 模型默认值

每一层均使用 `deep_merge(base, override)` — 嵌套 dict 递归合并，而非整体替换。

---

## EventBus

```python
bus = EventBus()
bus.subscribe(EventType.SIGNAL, handler)   # 按类型订阅
bus.subscribe_all(handler)                  # 订阅所有事件（用于监控）
bus.publish(event)
bus.clear()                                 # 测试 teardown 时调用
```

- 处理器在 `publish` 时同步调用，按订阅顺序执行。
- 处理器内的异常会被记录日志，**不会向上传播** — EventBus 不会因为坏处理器而崩溃。
- `subscribe_all` 由 Prometheus 计数器钩子和审计日志使用。

---

## 事件参考

| 类 | `event_type` | 关键字段 |
|---|---|---|
| `SignalEvent` | `SIGNAL` | `instrument_id`、`direction`（`LONG`/`SHORT`/`FLAT`）、`strength` |
| `OrderIntentEvent` | `ORDER_INTENT` | `instrument_id`、`side`、`quantity`、`order_type`、`price`、`stop_loss`、`take_profit` |
| `RiskAlertEvent` | `RISK_ALERT` | `level`（`WARNING`/`ERROR`/`CRITICAL`）、`rule_name`、`message` |

所有事件均为**冻结数据类** — 创建后不可修改。

---

## Interval → BarType 映射

| `Interval` | Nautilus K 线类型字符串 |
|---|---|
| `MINUTE_1` | `1-MINUTE` |
| `MINUTE_5` | `5-MINUTE` |
| `MINUTE_15` | `15-MINUTE` |
| `HOUR_1` | `1-HOUR` |
| `HOUR_4` | `4-HOUR` |
| `DAY_1` | `1-DAY` |

`MINUTE_1` K 线：`BarType.from_str(f"{instrument_id}-1-MINUTE-LAST-EXTERNAL")`
其他周期：从 1 分钟外部 K 线采样：`...-LAST-INTERNAL@1-MINUTE-EXTERNAL`
