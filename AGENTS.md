# AGENTS.md — Nautilus Binance V2

**Generated:** 2026-03-31 | **Commit:** 81179a4 | **Branch:** master

基于 [NautilusTrader](https://github.com/nautechsystems/nautilus_trader) 构建的机构级 Binance 合约交易系统。
策略只产信号，执行 / 风控 / 监控完全解耦。

---

## 项目结构

```
src/
  app/          # 依赖注入容器、工厂、Bootstrap 启动入口
  core/         # 共享基础设施（Config / Events / Logging / Constants / Enums）
  strategy/     # BaseStrategy + EMA / RSI / Turtle / VegasTunnel 等策略
  execution/    # OrderRouter、AlgoExecution、SignalProcessor
  risk/         # PreTradeRisk、DrawdownController、CircuitBreaker
  portfolio/    # PortfolioAllocator（equal / weight / risk_parity）
  state/        # Snapshot、Persistence、Reconciliation、Recovery
  live/         # Supervisor、Watchdog、Readiness、Warmup
  monitoring/   # Prometheus、HealthServer、Alerting、Notifiers
  backtest/     # Runner、WalkForward、Regime、Report
  data/         # Loaders、FeatureStore、Validators、Funding
  exchange/     # BinanceAdapter
  cache/        # RedisClient
tests/
  unit/         # 快速隔离单元测试（30 文件）
  integration/  # 多组件集成测试（5 文件）
  regression/   # 回归基准测试（6 文件）
scripts/        # 入口脚本：回测、数据下载、参数扫描、冒烟测试、生产启动
configs/        # 分层 YAML 配置（env / accounts / strategies / risk / execution / monitoring）
```

---

## WHERE TO LOOK

| 任务 | 位置 | 备注 |
|---|---|---|
| 添加新策略 | `src/strategy/<name>.py` + `src/app/bootstrap.py` `_STRATEGY_REGISTRY` + `src/app/factory.py` | 详见 `src/strategy/AGENTS.md` |
| 添加风控规则 | `src/risk/pre_trade.py` 或 `src/monitoring/watchers.py` | 违规发布事件，不抛异常 |
| 修改启动流程 | `src/app/bootstrap.py` `run_live()` | 主入口：`python -m src.app.bootstrap` |
| 修改容器服务 | `src/app/container.py` `build()` | 按依赖顺序初始化 |
| 修改订单管道 | `src/execution/signal_processor.py` → `order_router.py` | 信号→意图→风控→路由 |
| 修改告警通道 | `src/monitoring/notifier/telegram.py` 或 `slack.py` | 继承 `BaseNotifier` |
| 修改回测引擎 | `src/backtest/runner.py` | 数据需先下载到 `data/` |
| 修改配置加载 | `src/core/config.py` `load_app_config()` | 四层合并：env → YAML → config → defaults |
| 运行生产实盘 | `scripts/run_live_prod.sh` | 需 `CONFIRM_LIVE=YES` |
| 运行回测 | `scripts/run_backtest.py` | 支持所有已注册策略 |
| 下载数据 | `scripts/download_data.py` | 输出到 `data/raw/` + catalog |

---

## 环境搭建

```bash
# 推荐：uv（项目已锁定 uv.lock）
uv sync --all-extras
```

需要 **Python >= 3.13**。

---

## 构建 / 检查 / 测试命令

### 代码检查与格式化（Ruff）

```bash
# 检查并自动修复
uv run ruff check --fix src/ tests/

# 格式化
uv run ruff format src/ tests/

# 仅检查，不修改
uv run ruff check src/ tests/
uv run ruff format --check src/ tests/
```

### 类型检查（Pyrefly）

```bash
uv run pyrefly check
```

### 测试

```bash
# 运行全部测试
uv run pytest

# 仅跑单元测试（无外部依赖，速度快）
uv run pytest tests/unit/ -v

# 集成测试
uv run pytest tests/integration/ -v

# 回归基准测试
uv run pytest tests/regression/ -v

# 运行单个测试文件
uv run pytest tests/unit/test_order_router.py -v

# 运行单个测试用例
uv run pytest tests/unit/test_order_router.py::test_route_market_order -v

# 带覆盖率
uv run pytest --cov=src --cov-report=html

# 显示标准输出
uv run pytest -s tests/unit/test_order_router.py
```

### Pre-commit 钩子

```bash
# 安装钩子
uv run pre-commit install

# 手动运行全部钩子
uv run pre-commit run --all-files
```

---

## 代码风格规范

### Ruff 配置（pyproject.toml）

- **行长度**：120 字符
- **目标版本**：Python 3.13
- **启用规则集**：`E`、`F`、`W`、`I`（isort）、`UP`（pyupgrade）、`N`（pep8-naming）、
  `B`（bugbear）、`A`（builtins）、`C4`（comprehensions）、`SIM`（simplify）、`D`（docstrings）
- **文档字符串规范**：Google style

### 导入顺序

每个模块第一行必须是 `from __future__ import annotations`。
导入顺序（由 ruff/isort 强制执行）：

```python
from __future__ import annotations

# 1. 标准库
import time
from decimal import Decimal
from pathlib import Path

# 2. 第三方库
import structlog
from pydantic import BaseModel

# 3. nautilus_trader（视为第三方）
from nautilus_trader.model.data import Bar, BarType

# 4. 本地模块（src.*）
from src.core.events import EventBus, SignalEvent
```

禁止通配符导入。禁止相对导入，始终使用 `from src.module import ...`。

### 类型注解

- 所有函数签名和类属性必须有完整类型注解。
- 使用 `|` 联合语法（`str | None`），不用 `Optional[str]`。
- 使用 `collections.abc` 类型（`Callable`、`Iterable`），不用 `typing.Callable`。
- 使用内置泛型（`list[str]`、`dict[str, Any]`），不用 `List[str]`。
- `from typing import Any` 可接受；避免 `from typing import Dict, List, Tuple`。

### 命名规范

| 类型 | 规范 | 示例 |
|---|---|---|
| 类 | `PascalCase` | `OrderRouter`、`BaseStrategy` |
| 函数 / 方法 | `snake_case` | `check_pre_trade`、`route_order` |
| 私有属性 | `_snake_case` | `self._event_bus` |
| 模块级常量 | `UPPER_SNAKE` | `RISK_MODE_SOFT`、`DEFAULT_INSTRUMENTS` |
| 枚举成员 | `UPPER_SNAKE` | `EventType.ORDER_FILLED` |
| 配置类 | `PascalCase` + `Config` 后缀 | `BaseStrategyConfig`、`RiskConfig` |

### 文档字符串（Google Style）

所有公共类和公共函数必须有文档字符串。

```python
def check(
    self,
    intent: OrderIntentEvent,
    current_price: Decimal = Decimal(0),
) -> PreTradeCheckResult:
    """执行事前风控检查。

    Args:
        intent: 待校验的订单意图。
        current_price: 当前市场价格（USDT）。

    Returns:
        PreTradeCheckResult，含 passed 标志和原因。

    Raises:
        ValueError: 如果 intent 数量为负数。
    """
```

模块级文档字符串始终是文件第一行（中文简短描述完全可以）。

### 类的使用规范

- **Pydantic 模型**（`BaseModel`、`BaseSettings`）：用于配置和数据传输对象。
- **冻结配置**继承 `StrategyConfig` 并设置 `frozen=True`。
- **数据类**（`@dataclass`，通常 `frozen=True`）用于简单值对象和事件。
- **普通类**用于服务 / 管理器（除非继承 Nautilus，否则无需基类）。
- 私有状态使用 `_前缀` 实例属性，在 `__init__` 中赋值。

### 错误处理

- 捕获具体异常，禁止裸 `except:` 或 `except Exception:`。
- 在抛出或吞掉错误之前用 `structlog` 记录日志。
- 模块级使用 `logger = structlog.get_logger()`。
- 结构化日志调用：`logger.warning("message", key=value, ...)`。
- 风控违规通过 `EventBus` 发布 `RiskAlertEvent`，**不抛出异常**。
- 只对程序员错误 / 不变式违反才抛出异常；领域失败通过事件传递。

### Decimal 使用

- 所有财务数量使用 `decimal.Decimal`，禁止 `float`。
- 从字符串构造：`Decimal("0.01")`、`Decimal(str(value))`。
- 数量舍入使用 `ROUND_FLOOR`。

### 异步

- `pytest-asyncio` 使用 `asyncio_mode = "auto"` — 无需 `@pytest.mark.asyncio`。
- 除非操作确实是 I/O 密集型，否则优先使用同步代码。

---

## 测试规范

- 测试文件路径：`tests/{unit,integration,regression}/test_<module>.py`。
- Fixture 直接在测试文件中用 `@pytest.fixture` 定义（共享 fixture 放 `conftest.py`）。
- 外部 Nautilus 对象优先使用 `unittest.mock.MagicMock`。
- 所有创建 `EventBus` 的 fixture 必须 `yield` 并在 teardown 中调用 `bus.clear()`。
- 测试命名：`test_<被测行为>` 的描述性 snake_case。
- 回归测试将指标与存储的基准对比；未重新运行回测前不得收紧阈值。

---

## CODE MAP（复杂度热点）

| 文件 | 行数 | 角色 |
|---|---|---|
| `src/strategy/base.py` | 834 | 所有策略基类；定仓、止损/止盈、信号发布 |
| `src/exchange/binance_adapter.py` | 735 | Binance 适配器；节点构建与运行 |
| `src/data/loaders.py` | 723 | K 线下载 + Parquet 导入 |
| `src/app/factory.py` | 676 | 策略/适配器/回测运行器工厂 |
| `src/live/account_sync.py` | 652 | 定期账户对账与偏差检测 |
| `src/app/container.py` | 614 | 依赖注入容器 — 所有服务单例 |
| `src/app/bootstrap.py` | 457 | 启动引导 + `run_live()` 实盘入口 |
| `src/portfolio/allocator.py` | 464 | 多策略资金分配 |
| `src/live/watchdog.py` | 432 | 心跳监控 + 自动重启 |
| `src/backtest/runner.py` | 432 | 回测引擎封装 |
| `src/strategy/vegas_tunnel.py` | 416 | Vegas 隧道策略 |
| `src/monitoring/watchers.py` | 359 | 事件 Watcher 集合 |
| `src/backtest/regime.py` | 359 | 市场状态检测 |

---

## 关键架构规则

1. **策略绝不直接提交订单**，只调用 `self._event_bus.publish(SignalEvent(...))`。
2. **信号 → OrderIntent → PreTradeRisk → OrderRouter → Exchange** 是固定管道。
3. **`submit_orders=False`** 执行完整管道但跳过最终 `submit_order()` — 用于空跑。
4. **忽略的交易对**：启动时检测到外部持仓或挂单，对应交易对加入忽略列表，此后不再对其下单。
5. **配置分层**：`configs/env/<env>.yaml` 覆盖基础配置；`.env` 环境变量覆盖 YAML。
6. **所有配置模型**使用 Pydantic v2（`BaseModel` / `BaseSettings`），公共 API 禁止裸 dict。

---

## 反模式（此项目特有禁令）

- **禁止 `assert` 做运行时校验** — `container.py`、`redis_client.py`、策略文件中存在 `assert` 做 None 检查。`python -O` 会移除 assert，导致生产隐患。应改用 `if x is None: raise RuntimeError(...)`。
- **禁止策略层导入执行层** — `src/execution/` 不得从 `src/strategy/` 导入；信号单向流入。
- **禁止裸 `except Exception:`** — 当前 `base.py` 和 `param_sweep.py` 使用 `# noqa: BLE001` 绕过。需收窄异常类型。
- **`# type: ignore[return-value]`** — `redis_client.py` 有大量类型忽略，应通过规范化返回类型消除。
- **`exchange` 和 `cache` 的导入约束** — `src/AGENTS.md` 声明只有 `app/` 和 `live/` 可导入。**实际** `risk/real_time.py`、`risk/circuit_breaker.py`、`execution/rate_limiter.py` 也直接导入 `RedisClient`。需决定：更新文档以反映现实，或重构为通过 Container 注入。
- **禁止 `float` 表示财务数值** — 始终使用 `decimal.Decimal`。
- **禁止通配符导入和相对导入**。

---

## 注意事项

- **无 CI 工作流** — 仓库中无 `.github/workflows/`，测试/lint 依赖本地执行或外部 CI。
- **无应用 Dockerfile** — `docker-compose.yml` 仅启动基础设施（Postgres/Redis/Prometheus/Grafana），应用直接在主机运行。
- **`.env` 含真实密钥** — 已提交到仓库，需清理 git 历史并加入 `.gitignore`。
- **Python ≥ 3.13** — 编译/CI 需显式指定版本。
- **pyrefly 替代 mypy** — `nautilus_trader.*`、`pandas`、`yaml` 的导入被替换为 `Any`（`replace_imports_with_any`），类型检查对这些库宽松。
- **hatchling 构建后端** — 非 setuptools，CI 需安装 hatchling 或使用 `python -m build`。
- **uv 包管理器** — `uv.lock` 锁定依赖，推荐 `uv sync` 安装。
