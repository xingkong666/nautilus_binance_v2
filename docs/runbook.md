# 运维手册（Runbook）

日常操作、异常处理、排查流程。

---

## 启动 / 停止

### 正常启动

```bash
# Testnet（开发 / 验证）
uv run python -m src.app.bootstrap --env configs/env/dev.yaml

# 生产
uv run python -m src.app.bootstrap --env configs/env/prod.yaml
```

启动时会依次执行：

1. 配置加载 & 校验
2. 数据库初始化（SQLite）
3. 快照加载（如存在）
4. 对账（本地状态 vs 交易所）
5. TradingNode 启动 & 行情订阅
6. Supervisor 进入 RUNNING 状态

### 优雅停止

发送 `SIGINT`（Ctrl+C）或 `SIGTERM`：

```bash
kill -SIGTERM <pid>
```

停止流程：

1. Supervisor 停止接受新信号
2. 等待在途订单完成（超时 30s 强制取消）
3. 持仓快照写盘
4. TradingNode 优雅 dispose
5. 监控服务停止

### 强制停止（紧急）

```bash
kill -SIGKILL <pid>
```

⚠️ 强制停止不会触发优雅流程，下次启动时会从快照恢复，并执行对账。

---

## 回测

```bash
uv run python scripts/run_backtest.py \
  --config configs/strategies/ema_cross.yaml \
  --env configs/env/dev.yaml \
  --start 2024-01-01 \
  --end 2024-06-30

# 报告输出到 experiments/reports/
```

---

## Testnet 冒烟测试

每次上实盘前，先在 Testnet 跑一遍冒烟：

```bash
uv run python scripts/smoke_testnet.py
```

预期输出（全部出现才算通过）：

```
✅ TradingNode 启动成功
✅ 行情 tick 接收（WebSocket 连接正常）
✅ 市价单 Submitted → Accepted → Filled
✅ PositionOpened
✅ 节点优雅停止
```

如有失败，先检查：

1. `.env` 中 Testnet API Key 是否正确
2. Testnet 账户余额是否足够（最小名义 100 USDT）
3. 网络是否能访问 `stream.binancefuture.com`

---

## 数据下载

```bash
# 下载 BTCUSDT 1分钟数据（最近 30 天）
uv run python scripts/download_data.py \
  --symbol BTCUSDT \
  --interval 1m \
  --days 30

# 数据存到 data/raw/，处理后的 catalog 在 data/processed/
```

---

## 常见异常处理

### 节点异常退出后重启

```bash
# 直接重启，Container 会自动从快照恢复
uv run python -m src.app.bootstrap --env configs/env/prod.yaml
```

启动日志中查找：

```
[Recovery] Loaded snapshot from disk: ...
[Reconciliation] Local vs Exchange diff: ...
```

如果对账发现不一致，会发出 `CRITICAL` 告警并打印差异明细，**人工确认**后才会继续。

### 熔断触发

Telegram 收到告警：`🚨 熔断触发: daily_loss threshold exceeded`

处理流程：

1. 查看当日 PnL（Grafana 或日志）
2. 确认是策略失控还是市场异常
3. 等待冷却期结束（或手动重置）
4. 检查策略参数，必要时调整后重启

手动重置熔断（冷却期结束后）：

```bash
# 重启节点即可（Supervisor 启动时状态为 IDLE）
uv run python -m src.app.bootstrap --env configs/env/prod.yaml
```

### 对账不一致

Telegram 收到告警：`🔴 对账不一致: position mismatch on BTCUSDT-PERP`

处理流程：

1. 查看日志中的差异明细
2. 登录 Binance 确认实际持仓
3. 以交易所为准，手动修正本地状态（或平掉多余仓位）
4. 重启节点重新对账

### API Key 失效

症状：启动时报 `AuthenticationError` 或成交时报 `Invalid API-key`

处理：

1. 登录 Binance 检查 API Key 状态
2. 如需重新生成，更新 `.env` 中对应字段
3. 确认 IP 白名单包含当前服务器 IP
4. 重启节点

### WebSocket 断连

NautilusTrader 内置自动重连，通常无需干预。

如果持续断连（日志中频繁出现 `WebSocket reconnecting`）：

1. 检查网络稳定性
2. 检查 Binance 服务状态：https://www.binancezh.io/en/status
3. 考虑切换备用数据源（如有配置）

### 内存持续增长

检查是否有事件积压：

```bash
# 查看进程内存
ps aux | grep bootstrap
```

如果内存超过预期（如 >2GB），先优雅重启，同时记录现象，后续分析 DataEngine 缓存配置。

---

## 日志查看

### 日志格式

生产环境输出 JSON 结构化日志（`format: json`），开发环境输出人类可读格式。

```bash
# 实时查看（开发环境）
uv run python -m src.app.bootstrap --env configs/env/dev.yaml 2>&1 | tee run.log

# JSON 日志过滤（生产）
tail -f run.log | jq 'select(.level == "ERROR")'
```

### 关键日志关键字

| 关键字 | 含义 |
|---|---|
| `CircuitBreaker triggered` | 熔断触发 |
| `Reconciliation mismatch` | 对账不一致 |
| `OrderFilled` | 订单成交 |
| `PositionOpened/Closed` | 仓位变化 |
| `Supervisor state:` | 守护进程状态变化 |
| `PreTradeRisk rejected` | 风控拒绝订单 |

---

## 健康检查

```bash
# 节点运行时检查健康状态
curl http://localhost:8080/health | jq .
```

返回 `"status": "healthy"` 为正常，`"degraded"` 表示部分功能受限，`"unhealthy"` 需要立即处理。

---

## 手动平仓（紧急）

当需要紧急平掉所有仓位时，推荐操作：

1. **优先**：登录 Binance 网页端手动操作（最可靠）
2. **备选**：停止节点 → 修改策略配置关闭开仓信号 → 重启节点（会在 `close_positions_on_stop` 触发时平仓）

> ⚠️ 不要在系统异常时依赖自动平仓逻辑，直接操作交易所界面更安全。

---

## 版本升级

### 升级 nautilus_trader

```bash
# 更新 pyproject.toml 中版本约束
# 然后
uv sync

# 跑全量测试确认兼容性
uv run pytest

# 在 Testnet 跑冒烟测试
uv run python scripts/smoke_testnet.py
```

### 升级注意事项

- NautilusTrader API 在 minor 版本间可能有 breaking change，升级后必须跑冒烟
- 如果 `BinanceEnvironment` / `TradingNodeConfig` 等核心类签名变了，先更新 `src/exchange/binance_adapter.py`
- 回归测试（`tests/regression/`）的基准值是硬编码的，升级后如果基准漂移需要重新锁定
