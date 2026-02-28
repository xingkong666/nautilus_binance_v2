"""全局常量."""

from pathlib import Path

# 项目根目录
BASE_DIR = Path(__file__).resolve().parent.parent.parent

# 默认配置路径
CONFIGS_DIR = BASE_DIR / "configs"
DATA_DIR = BASE_DIR / "data"

# 事件类型
EVENT_MARKET_DATA = "market_data"
EVENT_SIGNAL = "signal"
EVENT_ORDER_INTENT = "order_intent"
EVENT_ORDER_SUBMITTED = "order_submitted"
EVENT_ORDER_FILLED = "order_filled"
EVENT_ORDER_CANCELLED = "order_cancelled"
EVENT_ORDER_REJECTED = "order_rejected"
EVENT_RISK_ALERT = "risk_alert"
EVENT_RISK_BREACH = "risk_breach"
EVENT_CIRCUIT_BREAKER = "circuit_breaker"
EVENT_POSITION_CHANGED = "position_changed"
EVENT_RECONCILIATION = "reconciliation"
EVENT_HEALTH_CHECK = "health_check"

# 风控模式
RISK_MODE_SOFT = "soft"
RISK_MODE_HARD = "hard"

# 熔断动作
CB_HALT_ALL = "halt_all"
CB_REDUCE_ONLY = "reduce_only"
CB_ALERT_ONLY = "alert_only"
