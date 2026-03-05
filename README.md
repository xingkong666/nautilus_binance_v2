# nautilus_binance_v2

基于 [NautilusTrader](https://github.com/nautechsystems/nautilus_trader) 构建的机构级 Binance 合约交易系统。

策略只产信号，执行 / 风控 / 监控完全解耦。

---

## 特性

- **多策略支持** — PortfolioAllocator 统一管理，equal / weight / risk_parity 三种分配模式
- **完整风控链路** — 事前（PreTradeRisk）→ 实时（DrawdownController + CircuitBreaker）→ 事后（PostTradeRisk）
- **状态持久化** — 快照 + 对账 + 崩溃恢复，节点重启自动续跑
- **可观测性** — Prometheus 指标 + Telegram/Slack 告警 + HTTP HealthCheck
- **Testnet 验证** — 全链路冒烟脚本，上实盘前在 Testnet 跑通

---

## 快速上手

### 1. 安装依赖

```bash
# 推荐 uv（项目已锁定 uv.lock）
uv sync

# 或 pip
pip install -e ".[dev]"

# 若需要 Telegram/Slack 告警
pip install -e ".[alerting]"
```

### 2. 配置环境变量

复制示例文件并填写 API Key：

```bash
cp .env.example .env
```

`.env` 关键字段：

```dotenv
# Binance Testnet
BINANCE_FUTURES_TESTNET_API_KEY=your_key
BINANCE_FUTURES_TESTNET_API_SECRET=your_secret

# Binance 实盘（谨慎填写）
BINANCE_FUTURES_API_KEY=your_key
BINANCE_FUTURES_API_SECRET=your_secret

# Telegram 告警（可选）
TELEGRAM_BOT_TOKEN=your_token
TELEGRAM_CHAT_ID=your_chat_id
```

### 3. Testnet 冒烟测试

```bash
uv run python scripts/smoke_testnet.py
```

验证：行情接收 → 开仓成交 → reduce-only 平仓成交 → 节点停止 → 进程正常退出（无事件循环报错）。

### 4. 回测

```bash
uv run python scripts/run_backtest.py \
  --strategy ema_cross \
  --symbols BTCUSDT \
  --start 2024-01-01 \
  --end 2024-06-30 \
  --interval 15m \
  --fast-ema 10 \
  --slow-ema 20
```

海龟策略示例：

```bash
uv run python scripts/run_backtest.py \
  --strategy turtle \
  --symbols BTCUSDT \
  --start 2024-01-01 \
  --end 2024-06-30 \
  --interval 15m \
  --entry-period 20 \
  --exit-period 10 \
  --turtle-atr-period 20 \
  --stop-atr-multiplier 2.0 \
  --unit-add-atr-step 0.5 \
  --max-units 4
```

Vegas 隧道策略示例：

```bash
uv run python scripts/run_backtest.py \
  --strategy vegas_tunnel \
  --symbols BTCUSDT \
  --start 2024-01-01 \
  --end 2024-06-30 \
  --vegas-fast-ema 12 \
  --vegas-slow-ema 36 \
  --tunnel-ema-1 144 \
  --tunnel-ema-2 169 \
  --vegas-stop-atr-multiplier 1.0 \
  --vegas-fib-1 1.0 \
  --vegas-fib-2 1.618 \
  --vegas-fib-3 2.618
```

### 5. 实盘

```bash
# Testnet（模拟盘，真实策略）
uv run python scripts/run_live_testnet_strategy.py \
  --strategy-config configs/strategies/vegas_tunnel.yaml \
  --symbol BTCUSDT

# Testnet
uv run python -m src.app.bootstrap --env configs/env/dev.yaml

# 生产
uv run python -m src.app.bootstrap --env configs/env/prod.yaml
```

说明：`bootstrap` 只负责容器初始化；要在模拟盘真正跑策略，请使用 `run_live_testnet_strategy.py`。

---

## 项目结构

```
nautilus_binance_v2/
├── configs/                # 配置文件（按关注点分离）
│   ├── env/                #   dev / stage / prod 环境差异
│   ├── accounts/           #   交易所账户
│   ├── strategies/         #   策略参数
│   ├── risk/               #   全局风控
│   ├── execution/          #   执行层
│   └── monitoring/         #   告警规则
├── data/                   # 数据目录（gitignore）
│   ├── raw/                #   原始 K 线
│   ├── processed/          #   NautilusTrader catalog
│   ├── features/           #   特征缓存
│   └── versioned/          #   版本化数据集
├── docs/                   # 文档
│   ├── architecture.md     #   系统架构
│   ├── risk.md             #   风控体系
│   ├── monitoring.md       #   监控告警
│   └── runbook.md          #   运维手册
├── experiments/            # 实验产物（报告 / 模型 / artifacts）
├── scripts/                # 入口脚本
│   ├── smoke_testnet.py    #   Testnet 冒烟
│   ├── run_live_testnet_strategy.py  # Testnet 模拟盘策略运行
│   ├── run_backtest.py     #   批量回测
│   └── download_data.py    #   数据下载
├── src/                    # 核心代码
│   ├── app/                #   应用组装（Container / Factory / Bootstrap）
│   ├── core/               #   基础设施（Config / Events / Logging）
│   ├── data/               #   数据加载与特征
│   ├── strategy/           #   策略基类 + EMA / RSI / Turtle / VegasTunnel
│   ├── execution/          #   执行层（OrderRouter / AlgoExecution）
│   ├── risk/               #   风控全链路
│   ├── portfolio/          #   多策略分配
│   ├── state/              #   持久化 / 快照 / 对账
│   ├── live/               #   实盘守护（Supervisor / Watchdog）
│   ├── monitoring/         #   Prometheus / HealthServer / 告警
│   ├── backtest/           #   回测引擎封装
│   └── exchange/           #   BinanceAdapter
└── tests/
    ├── unit/               #   91 个单元测试
    ├── integration/        #   36 个集成测试
    └── regression/         #   18 个回归基准测试
```

---

## 测试

```bash
# 全量跑（145 tests）
uv run pytest

# 只跑单元测试
uv run pytest tests/unit/ -v

# 带覆盖率
uv run pytest --cov=src --cov-report=html
```

---

## 监控（可选）

启动 Prometheus + Grafana：

```bash
docker compose up -d
```

- Prometheus: http://localhost:9090
- Grafana:    http://localhost:3000  （默认账密 admin / admin）
- HealthCheck: http://localhost:8080/health

详见 [docs/monitoring.md](docs/monitoring.md)。

---

## 依赖版本

| 包 | 版本要求 |
|---|---|
| Python | >= 3.13 |
| nautilus_trader | >= 1.223.0 |
| pydantic | >= 2.12.5 |
| prometheus-client | >= 0.24.1 |

---

## 文档索引

| 文档 | 内容 |
|---|---|
| [architecture.md](docs/architecture.md) | 系统架构与模块职责 |
| [risk.md](docs/risk.md) | 风控体系详解 |
| [monitoring.md](docs/monitoring.md) | 监控 & 告警配置 |
| [runbook.md](docs/runbook.md) | 运维操作手册 |
| [vegas_tunnel_rollout_2026_03.md](docs/vegas_tunnel_rollout_2026_03.md) | Vegas 策略与执行安全增强变更记录 |
| [turtle_backtest_4h_sensitivity.md](docs/turtle_backtest_4h_sensitivity.md) | Turtle 4h 参数敏感性与样本外验证 |
