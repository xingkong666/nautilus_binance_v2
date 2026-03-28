# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## 项目概述

基于 [NautilusTrader](https://github.com/nautechsystems/nautilus_trader) 构建的机构级 Binance 合约交易系统。策略只产信号，执行 / 风控 / 监控三层完全解耦，通过进程内 `EventBus` 通信。

## 常用命令

### 安装依赖
```bash
uv sync                         # 推荐，使用 uv.lock 锁定版本
pip install -e ".[dev]"         # 备选：pip 安装
pip install -e ".[alerting]"    # 可选：Telegram/Slack 告警支持
```

### 运行
```bash
# 实盘 / Testnet
uv run python -m src.app.bootstrap --env configs/env/dev.yaml    # testnet
uv run python -m src.app.bootstrap --env configs/env/prod.yaml   # 生产

# 回测
uv run python scripts/run_backtest.py \
  --config configs/strategies/ema_cross.yaml \
  --env configs/env/dev.yaml \
  --start 2024-01-01 --end 2024-06-30

# Testnet 冒烟测试（行情接收 → 市价单 → 成交 → 停止）
uv run python scripts/smoke_testnet.py

# 下载历史数据
uv run python scripts/download_data.py
```

### 测试
```bash
uv run pytest                                        # 全量（约 145 个）
uv run pytest tests/unit/ -v                         # 仅单元测试
uv run pytest tests/integration/ -v                 # 仅集成测试
uv run pytest tests/regression/ -v                  # 回归基准测试
uv run pytest tests/unit/test_allocator.py -v       # 运行单个测试文件
uv run pytest --cov=src --cov-report=html            # 生成覆盖率报告
```

### 代码检查
```bash
uv run ruff check src/ tests/      # Lint（行宽 120）
uv run ruff format src/ tests/     # 格式化
uv run pyrefly check              # 类型检查
```

### 基础设施（Docker）
```bash
docker compose up -d              # 启动全部基础设施服务
docker compose down               # 停止并移除容器（数据卷保留）
docker compose ps                 # 查看各服务运行状态
docker compose logs -f postgres   # 查看 PostgreSQL 日志

# 服务端口
# PostgreSQL:   localhost:5432
# Redis:        localhost:6379
# Prometheus:   http://localhost:9090
# Grafana:      http://localhost:3000（默认账密 admin / admin）
# AlertManager: http://localhost:9093
# HealthCheck:  http://localhost:8080/health
```

所需环境变量（`.env` 文件）：
```
POSTGRES_USER=<用户名>
POSTGRES_PASSWORD=<密码>
POSTGRES_DB=<数据库名>
```

## 架构

### 核心设计原则
- **策略只产信号** — 不直接调用交易所接口
- **事件驱动** — 所有跨模块通信通过 `EventBus`（发布/订阅），模块之间不直接调用彼此方法
- **配置优先级**：环境变量 > `configs/env/{env}.yaml` > 各模块 YAML（`configs/risk/` 等）> 代码默认值

### 信号 → 成交数据流
```
Binance WS → NautilusTrader DataEngine → Strategy.on_bar()
    → generate_signal() → SignalEvent（发布到 EventBus）
    → PortfolioAllocator（仓位分配）
    → PreTradeRisk（准入校验）
    → OrderIntent → OrderRouter → AlgoExecution（TWAP / 市价）
    → RateLimiter → BinanceAdapter → Binance API
    → OrderFilled → FillHandler（归因 + PostgreSQL 持久化）
    → PostTradeRisk（滑点 / PnL 分析）
```

### 风控链路
三层独立风控，任意一层均可单独触发熔断：
```
PreTradeRisk    — 下单前检查（仓位大小、杠杆、订单大小）
RealTimeRisk    — 每个 bar/tick 监控（回撤、日亏损）
CircuitBreaker  — 熔断动作：halt_all / reduce_only / alert_only
DrawdownControl — 追踪回撤触发时动态降仓
PostTradeRisk   — 成交后滑点与 PnL 归因分析
```

### 应用组装（`src/app/`）
- `Container` — 依赖注入容器，所有服务单例均在此管理。构建顺序：基础设施 → 执行 → 风控 → 资金分配 → 交易所 → 监控。访问任何属性前需先调用 `container.build()`，结束时调用 `container.teardown()` 释放资源。
- `AppFactory` — 基于已构建的 `Container` 创建回测运行器、策略等对象的工厂。
- `bootstrap.py` — 启动入口封装：`bootstrap(env)` 返回裸 `Container`；`bootstrap_app(env)` 返回含 container + factory 的 `AppContext`；`bootstrap_context(env)` 为上下文管理器，退出时自动 teardown。

### 策略开发（`src/strategy/`）
继承 `BaseStrategy`，实现两个抽象方法：
- `_register_indicators()` — 注册 NautilusTrader 指标
- `generate_signal(bar) -> SignalDirection | None` — 返回 `LONG`、`SHORT`、`FLAT` 或 `None`

基类负责：Bar 订阅、ATR bracket 止损止盈单管理、信号发布到 EventBus（实盘）或直接 `submit_order`（无 EventBus 的回测模式）、策略停止时的仓位清理。

策略配置继承 `BaseStrategyConfig`（Pydantic frozen `StrategyConfig`）。

### 配置加载（`src/core/config.py`）
`load_app_config(env)` 将所有 YAML 文件合并为带类型的 `AppConfig`（Pydantic）。敏感信息（API Key、Telegram Token）通过 `EnvSettings`（pydantic-settings）从 `.env` 读取。配置校验严格——字段非法时启动即失败。

### 状态持久化（`src/state/`）
- `TradePersistence` — **PostgreSQL**（`psycopg`）持久化成交 / 事件；`database_url` 从 `AppConfig` / 环境变量读取
- `SnapshotManager` — 定期将内存状态快照序列化到磁盘
- `ReconciliationManager` — 对比本地状态与交易所实际状态，输出差异
- `RecoveryManager` — 节点重启时从快照 + 对账结果恢复状态

### 实盘守护（`src/live/`）
`Supervisor` 运行状态机：`IDLE → RUNNING → DEGRADED → STOPPED`。`Watchdog` 心跳检测，超时自动重启。`DEGRADED` 状态（熔断触发后），`OrderRouter` 拒绝所有新订单。

### 监控（`src/monitoring/`）
`monitoring.enabled=true` 时生效：
- Prometheus 指标暴露在 `:9090/metrics`（由 docker compose 中 `prometheus` 服务采集）
- Grafana 仪表盘在 `:3000`，数据源为 Prometheus + PostgreSQL
- AlertManager 告警路由在 `:9093`
- 健康检查端点 `:8080/health`
- `AlertManager` 评估告警规则，通过 `Notifier` 分发（Telegram/Slack）
- `EventBus` 全局 handler 自动按事件类型累加 `EVENT_BUS_EVENTS` 计数器

### BinanceAdapter（`src/exchange/binance_adapter.py`）
封装 NautilusTrader `TradingNode`，支持 `LIVE / TESTNET` 环境。API Key 优先读环境变量，YAML 中可配置 fallback。`start()` / `stop()` 均为 async——在调用 `container.teardown()` 前须先 `await adapter.stop()`。

## 约定
- 所有导入使用 `from src.xxx`（hatchling editable install）
- pytest 已配置 `asyncio_mode = "auto"`，async 测试无需 `@pytest.mark.asyncio`
- 结构化日志使用 `structlog`，上下文信息以关键字参数传入：`logger.info("event_name", key=val)`
- 要求 Python 3.13，pyrefly 类型检查强制执行
