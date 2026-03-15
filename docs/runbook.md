# 运维手册（Runbook）

日常操作、异常处理、排查流程。

---

## 启动 / 停止

### 正常启动

```bash
# Testnet（开发 / 验证）
uv run python -m src.app.bootstrap \
  --env configs/env/dev.yaml \
  --strategy-config configs/strategies/vegas_tunnel.yaml \
  --symbol BTCUSDT

# 生产
uv run python -m src.app.bootstrap \
  --env configs/env/prod.yaml \
  --strategy-config configs/strategies/vegas_tunnel.yaml \
  --symbol BTCUSDT
```

启动时会依次执行：

1. 配置加载 & 校验
2. 数据库初始化（SQLite）
3. 查询账户模式（若为 Hedge Mode，自动关闭 `reduce_only`）
4. 拉取交易所余额 / 持仓 / open orders
5. 按环境加载快照（`snapshots/dev`、`snapshots/prod` 等）
6. 恢复状态并标记有外部活动的交易对为 ignored
7. TradingNode 启动 & 行情订阅
8. Supervisor 进入 RUNNING 状态

补充说明：

1. 若某交易对在启动时已存在外部持仓或外部挂单，本系统会忽略该交易对，不再继续下单。
2. `ignored` 是运行期保护，不会自动替你撤销外部挂单或平掉外部持仓。

### 优雅停止

发送 `SIGINT`（Ctrl+C）或 `SIGTERM`：

```bash
kill -SIGTERM <pid>
```

停止流程：

1. Supervisor 停止接受新信号
2. 等待在途订单完成（超时 30s 强制取消）
3. 持仓 / open orders 快照写盘
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
  --env dev \
  --symbols BTCUSDT \
  --start 2024-01-01 \
  --end 2024-06-30 \
  --interval 15m \
  --fast-ema 10 \
  --slow-ema 20 \
  --entry-min-atr-ratio 0.0015 \
  --signal-cooldown-bars 3

# 报告输出到 experiments/reports/
```

Vegas 隧道回测示例：

```bash
uv run python scripts/run_backtest.py \
  --env dev \
  --strategy vegas_tunnel \
  --symbols BTCUSDT \
  --start 2024-01-01 \
  --end 2024-06-30 \
  --vegas-fast-ema 12 \
  --vegas-slow-ema 36 \
  --tunnel-ema-1 144 \
  --tunnel-ema-2 169 \
  --vegas-stop-atr-multiplier 1.0
```

Vegas 端到端冒烟快照（2026-03）：

- 结果目录：`experiments/reports/vegas_tunnel_smoke_20240101_20240630`
- Run ID：`e87209cd-9164-4b7e-9802-1a91e525f4bf`
- 关键指标：`total_orders=224`、`total_positions=54`、`PnL=+585.1334 USDT`

### 参数扫描（2024H2 默认窗口）

```bash
uv run python scripts/param_sweep.py \
  --strategy ema \
  --symbols BTCUSDT ETHUSDT \
  --is-start 2024-07-01 \
  --is-end 2024-10-31 \
  --oos-start 2024-11-01 \
  --oos-end 2024-12-31 \
  --workers 8
```

关键输出文件：

- `experiments/sweep/ema_sweep_is.csv`
- `experiments/sweep/ema_sweep_oos.csv`

新增平滑度相关列：

- `Volatility`
- `MaxDrawdown`
- `SmoothScore`（综合排序分数，越高越好）

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
✅ 开仓市价单 Submitted → Accepted → Filled
✅ reduce-only 平仓单 Submitted → Accepted → Filled
✅ 节点优雅停止
✅ 进程退出码为 0（无 `Event loop stopped before Future completed`）
```

如有失败，先检查：

1. `.env` 中 Testnet API Key 是否正确
2. Testnet 账户余额是否足够（最小名义 100 USDT）
3. 网络是否能访问 `stream.binancefuture.com`

补充说明（2026-03-05 更新）：

1. 脚本在平仓成交后会主动触发节点停止，并在主流程统一 `dispose`，用于避免事件循环提前停止报错。
2. 若日志出现 `Residual Position(...)`，通常是测试账户里已有历史仓位，不代表本次冒烟单未平。

补充说明（2026-03-15 更新）：

1. Testnet 与 prod 快照目录已按环境隔离，避免测试环境状态污染生产恢复。
2. 若测试账户本身存在外部持仓或外部挂单，对应交易对会在 live 启动时被自动忽略。

## Testnet 模拟盘（真实策略）

按策略 YAML 在 Testnet 直接运行：

```bash
uv run python scripts/run_live_testnet_strategy.py \
  --strategy-config configs/strategies/vegas_tunnel.yaml \
  --symbol BTCUSDT
```

常用参数：

1. `--strategy-config`：可切换为 `configs/strategies/turtle.yaml` / `micro_scalp.yaml` 等。
2. `--timeout-seconds 3600`：运行 1 小时后自动停机（不传则持续运行，Ctrl+C 停止）。

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

Funding Rate 下载：

```bash
uv run python scripts/download_funding_rates.py \
  --symbol BTCUSDT \
  --start 2024-01-01 \
  --end 2025-12-31

uv run python scripts/download_funding_rates.py \
  --symbol ETHUSDT \
  --start 2024-01-01 \
  --end 2025-12-31
```

输出位置：

- 原始 CSV：`data/raw/funding/`
- 标准化 parquet：`data/features/`

组合 walk-forward：

```bash
uv run python scripts/run_portfolio_walkforward.py \
  --config configs/strategies/vegas_ema_combo_multi_grid.yaml
```

当前最佳场景不是整个网格，而是：

- `experiments/walkforward/vegas_ema_combo_multi_grid/score_0/`

如需复现已验证但未胜出的 regime 实验，可使用：

```bash
uv run python scripts/run_portfolio_walkforward.py \
  --config configs/strategies/vegas_ema_combo_multi_best_strategy_regime.yaml

uv run python scripts/run_portfolio_walkforward.py \
  --config configs/strategies/vegas_ema_combo_multi_best_strategy_regime_relaxed_gate.yaml
```

### 代理与网络环境

若你所在环境需要代理访问 Binance，建议显式配置：

```bash
export HTTPS_PROXY=http://127.0.0.1:7890
export HTTP_PROXY=http://127.0.0.1:7890
export NO_PROXY=127.0.0.1,localhost
```

注意事项：

1. 下载器对“本地已有 CSV”走快路径，不会创建网络客户端。  
2. 若设置了 `ALL_PROXY=socks5://...` 或 `socks5h://...`，需安装 SOCKS 依赖（`httpx[socks]`），否则会在创建 HTTP client 时抛错。  
3. 代理仅影响需要联网的下载流程，不影响本地回测与本地 CSV 读取。

---

## 常见异常处理

### 节点异常退出后重启

```bash
# 直接重启，Container 会自动从快照恢复
uv run python -m src.app.bootstrap \
  --env configs/env/prod.yaml \
  --strategy-config configs/strategies/vegas_tunnel.yaml \
  --symbol BTCUSDT
```

启动日志中查找：

```
[Recovery] Loaded snapshot from disk: ...
[Reconciliation] Local vs Exchange diff: ...
[Ignored] instrument marked ignored due to external activity: ...
```

当前恢复行为：

1. 若本地无快照，会直接用交易所真值冷启动。
2. 若本地快照与交易所不一致，会以交易所真值覆盖本地仓位。
3. 若发现外部持仓或外部挂单，对应交易对会被加入 ignored 列表，本系统后续不会再对其下单。

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
3. 确认该交易对是否属于人工/其他系统管理
4. 若属于外部管理，保持 ignored 状态，不要让本系统继续参与
5. 若不属于外部管理，先清理外部仓位 / 挂单，再重启节点重新对账

### 外部下单 / 外部挂单

症状：

- 启动日志出现 `instrument_ignored_external_activity`
- 或信号被拒绝，日志出现 `signal_rejected_ignored_instrument`

处理流程：

1. 在 Binance 确认该交易对是否存在人工或其他系统的持仓 / 挂单。
2. 若确认是外部交易，保持当前 ignored 状态，不要强行恢复本系统下单。
3. 若要恢复本系统控制，先撤掉外部挂单、平掉外部持仓。
4. 重启节点，让系统重新做启动恢复与交易对过滤。

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
# 更新 pyproject.toml 中版本约束到 1.224.0
uv lock --upgrade-package nautilus-trader
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
