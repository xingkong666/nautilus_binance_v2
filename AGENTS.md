# 项目知识库

**Generated:** 2026-02-28 16:00 Asia/Shanghai
**Branch:** `master` (no commits yet)
**Commit:** `N/A`

## 概览
基于 NautilusTrader 的机构级 Binance 合约交易系统。策略层只产出信号；执行、风控、监控、持久化通过 EventBus 解耦。

## 目录结构
```text
nautilus_binance_v2/
├── src/         # 按领域划分的运行时模块
├── tests/       # 单元/集成/回归测试
├── configs/     # 环境与模块 YAML 配置
├── scripts/     # 运维与入口脚本
├── docs/        # 架构/运行手册/风控/监控文档
└── docker-compose.yml
```

## 快速定位
| 任务 | 位置 | 说明 |
|---|---|---|
| 应用生命周期 | `src/app/` | Container 构建/释放、bootstrap 上下文 |
| 配置系统 | `src/core/config.py`, `configs/` | 环境变量 > 环境 YAML > 模块 YAML > 默认值 |
| 事件与契约 | `src/core/events.py` | 跨模块事件模型 |
| 策略逻辑 | `src/strategy/` | 仅生成信号 |
| 执行流程 | `src/execution/` | intent/router/algo/limit/fill |
| 风控链路 | `src/risk/` | pre-trade/realtime/breaker/post-trade |
| 监控与告警 | `src/monitoring/` | watchers 与 notifier 后端 |
| 状态持久化 | `src/state/` | PostgreSQL + 快照/恢复 |
| 测试结构 | `tests/` | unit/integration/regression 分层 |

## 约定
- 使用 `src...` 绝对导入。
- Python `>=3.13`；`mypy` 为 strict；`ruff` 行宽 `120`。
- `pytest` 使用 `asyncio_mode = "auto"`。
- 使用结构化日志（`structlog`），保持 key/value 上下文字段。

## 反模式（本项目）
- 不要绕过 EventBus 进行跨模块调用。
- 不要在策略层直接调用交易所 API。
- 不要把密钥写入 YAML；应从环境变量读取。
- 不要修改配置优先级语义。

## 项目特有风格
- 三层风控可独立触发。
- 在脚本与测试中优先使用 `bootstrap_context(...)`，确保安全 teardown。
- 监控 watcher 统一通过 AlertManager/notifier 管道下发告警。

## 常用命令
```bash
uv sync
uv run python -m src.app.bootstrap --env configs/env/dev.yaml
uv run python scripts/run_backtest.py --config configs/strategies/ema_cross.yaml --env configs/env/dev.yaml --start 2024-01-01 --end 2024-06-30
uv run python scripts/smoke_testnet.py
uv run pytest
uv run ruff check src/ tests/
uv run mypy src/
docker compose up -d
```

## 备注
- 当前仓库尚无 git 提交。
- 修改核心逻辑时忽略 `.venv`、缓存目录与 `data/`。
- 子目录说明见 `src/`、`tests/`、`configs/` 下的 AGENTS。
