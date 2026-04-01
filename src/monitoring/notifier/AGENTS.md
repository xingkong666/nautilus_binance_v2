<!-- Parent: ../AGENTS.md -->
<!-- Generated: 2026-04-01 | Updated: 2026-04-01 -->

# Notifier

## Purpose
多渠道告警通知实现层。`BaseNotifier` 定义统一接口，所有渠道实现（Telegram、Slack）均继承它，并按各自配置的 `min_level` 过滤低优先级消息。通知器通过 `AlertManager.add_notifier()` 注册，由 `AlertManager` 统一调度。所有 HTTP 调用使用同步 `httpx`，避免引入 asyncio 依赖。

## Key Files

| File | Description |
|------|-------------|
| `base.py` | `BaseNotifier`（ABC）、`AlertLevel`（`WARNING=1 / ERROR=2 / CRITICAL=3`）、`AlertMessage` 数据类 — 定义所有通知器必须实现的 `send()` 接口 |
| `telegram.py` | `TelegramNotifier` — 通过 Telegram Bot API（`https://api.telegram.org/bot{token}/sendMessage`）发送告警；Token 和 Chat ID 从环境变量 `TELEGRAM_BOT_TOKEN` / `TELEGRAM_CHAT_ID` 读取 |
| `slack.py` | `SlackNotifier` — 通过 Slack Incoming Webhook 发送告警；Webhook URL 从环境变量 `SLACK_WEBHOOK_URL` 或直接传参获取；按 `AlertLevel` 显示不同 attachment 颜色（橙/红/深红） |
| `__init__.py` | 模块公开导出：`BaseNotifier`、`AlertLevel`、`AlertMessage`、`TelegramNotifier`、`SlackNotifier` |

## For AI Agents

### Working In This Directory
- 新增渠道：继承 `BaseNotifier`，实现 `send(message: AlertMessage) -> None`，内部按 `self.min_level` 过滤
- `AlertLevel` 比较遵循枚举值大小（`WARNING < ERROR < CRITICAL`）；过滤逻辑：`if message.level.value < self.min_level.value: return`
- `TelegramNotifier` 使用同步 `httpx.post()`，Telegram Bot API 限速约 30 msg/s（同一 chat 1 msg/s），告警场景不会触发
- `SlackNotifier` 使用 Incoming Webhook（无需 slack-sdk），颜色映射：WARNING→`#FFA500`、ERROR→`#FF0000`、CRITICAL→`#8B0000`
- 凭证优先从环境变量读取，支持直接传参作为 fallback（与整体 `EnvSettings` 设计一致）
- `AlertManager` 不做级别过滤，过滤完全由各 Notifier 内部自主决定

### Testing Requirements
- 单元测试中 mock `httpx.post` 验证请求参数（URL、payload 格式）
- 测试 `min_level` 过滤：发送低于阈值的 `AlertMessage`，验证 `httpx.post` 未被调用
- 凭证缺失时应优雅降级（记录日志 + 跳过发送），不抛出异常中断主进程

### Common Patterns
```python
# 使用 TelegramNotifier
from src.monitoring.notifier.telegram import TelegramNotifier
from src.monitoring.notifier.base import AlertLevel

notifier = TelegramNotifier(
    token="BOT_TOKEN",       # 或省略，从环境变量 TELEGRAM_BOT_TOKEN 读取
    chat_id="CHAT_ID",       # 或省略，从环境变量 TELEGRAM_CHAT_ID 读取
    min_level=AlertLevel.ERROR,
)

# 使用 SlackNotifier
from src.monitoring.notifier.slack import SlackNotifier

notifier = SlackNotifier(
    webhook_url="https://hooks.slack.com/...",  # 或从 SLACK_WEBHOOK_URL 读取
    min_level=AlertLevel.WARNING,
)

# 自定义通知器
from src.monitoring.notifier.base import BaseNotifier, AlertMessage

class PagerDutyNotifier(BaseNotifier):
    def send(self, message: AlertMessage) -> None:
        if message.level.value < self.min_level.value:
            return
        # ... 实现发送逻辑
```

## Dependencies

### Internal
- `src.monitoring.notifier.base` — `BaseNotifier`、`AlertLevel`、`AlertMessage`（`telegram.py` / `slack.py` 均依赖）

### External
- `httpx` — 所有渠道的 HTTP 请求（同步调用）
- `structlog` — 结构化日志
- 环境变量：`TELEGRAM_BOT_TOKEN`、`TELEGRAM_CHAT_ID`、`SLACK_WEBHOOK_URL`

<!-- MANUAL: -->
