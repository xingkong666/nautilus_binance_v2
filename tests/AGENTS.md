# TESTS 目录知识库

## 概览
`tests/` 用于验证容器装配、领域行为与策略基线表现，按 unit/integration/regression 分层。

## 目录结构
```text
tests/
├── unit/         # 模块级行为验证
├── integration/  # 模块联动与生命周期路径
└── regression/   # 策略/回测基线期望
```

## 快速定位
| 任务 | 位置 | 说明 |
|---|---|---|
| 容器启动不变量 | `tests/integration/test_container_build.py` | 服务初始化与 teardown 路径 |
| 事件流水线行为 | `tests/integration/test_event_pipeline.py` | 跨模块事件流 |
| 风控集成校验 | `tests/integration/test_risk_integration.py` | 风控模块联动 |
| 资金分配行为 | `tests/unit/test_allocator.py` | equal/weight/risk_parity 与 rebalance |
| 状态对账行为 | `tests/unit/test_reconciliation.py` | 持久化与对账约束 |
| 策略基线快照 | `tests/regression/test_ema_cross_baseline.py`, `tests/regression/test_rsi_baseline.py` | 历史期望输出 |

## 约定
- 使用 `pytest`，并遵循 `asyncio_mode = "auto"` 配置。
- 测试按意图分层：unit / integration / regression。
- 分配与回测断言优先使用确定性 fixture 与明确数值容差。

## 反模式
- 不要让 unit 测试依赖外部在线服务。
- 不要把 regression 断言混入 integration，基线检查需隔离。
- 不要复制生产逻辑到测试中，统一从 `src` 导入。

## 备注
- Regression 套件应保持稳定，对期望输出采用保守策略。
- Integration 测试可构建完整 container，但必须覆盖 teardown 路径。
