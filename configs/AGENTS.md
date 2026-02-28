# CONFIGS 目录知识库

## 概览
`configs/` 存放由 `src/core/config.py` 消费的环境与领域 YAML 配置。

## 目录结构
```text
configs/
├── env/          # dev/stage/prod 环境覆盖
├── accounts/     # 交易所账户配置
├── strategies/   # 策略参数
├── risk/         # 全局风控参数
├── execution/    # 执行默认值/速率控制
└── monitoring/   # 告警与监控配置
```

## 快速定位
| 任务 | 位置 | 说明 |
|---|---|---|
| 环境差异配置 | `configs/env/dev.yaml`, `configs/env/stage.yaml`, `configs/env/prod.yaml` | 由运行环境选择 |
| 账户与 venue 配置 | `configs/accounts/binance_futures.yaml` | 账户/交易场所语义 |
| 策略默认参数 | `configs/strategies/ema_cross.yaml` | 策略运行参数 |
| 风控阈值 | `configs/risk/global_risk.yaml` | 风控链路关键参数 |
| 执行策略参数 | `configs/execution/execution.yaml` | 下单/限速默认值 |
| 告警规则 | `configs/monitoring/alerts.yaml` | watcher 触发规则定义 |

## 约定
- 保持与 `src/core/config.py` 模型一致的 key 命名。
- 环境覆盖配置应最小化且明确。
- 敏感信息走环境变量，不写入版本化 YAML。

## 反模式
- 不要修改优先级语义：环境变量覆盖 YAML。
- 不要在所有 env 文件中无差别重复默认值。
- 不要把生产密钥写入可追踪配置文件。

## 备注
- 新增配置段时，先同步 `src/core/config.py` 的 schema。
- `configs/monitoring/alerts.yaml` 规则名需与 watcher 实现保持一致。
