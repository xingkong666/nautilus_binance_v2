# AGENTS.md — Nautilus Binance V2

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
  unit/         # 快速隔离单元测试
  integration/  # 多组件集成测试
  regression/   # 回归基准测试
```

---

## 环境搭建

```bash
# 推荐：uv（项目已锁定 uv.lock）
uv sync

# 或 pip 可编辑安装
pip install -e ".[dev]"

# 可选告警依赖
pip install -e ".[alerting]"
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

## 关键架构规则

1. **策略绝不直接提交订单**，只调用 `self._event_bus.publish(SignalEvent(...))`。
2. **信号 → OrderIntent → PreTradeRisk → OrderRouter → Exchange** 是固定管道。
3. **`submit_orders=False`** 执行完整管道但跳过最终 `submit_order()` — 用于空跑。
4. **忽略的交易对**：启动时检测到外部持仓或挂单，对应交易对加入忽略列表，此后不再对其下单。
5. **配置分层**：`configs/env/<env>.yaml` 覆盖基础配置；`.env` 环境变量覆盖 YAML。
6. **所有配置模型**使用 Pydantic v2（`BaseModel` / `BaseSettings`），公共 API 禁止裸 dict。
