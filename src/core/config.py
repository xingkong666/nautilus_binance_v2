"""统一配置加载.

支持多环境(dev/stage/prod) + 分模块配置合并.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel
from pydantic_settings import BaseSettings, SettingsConfigDict

from src.core.constants import BASE_DIR, CONFIGS_DIR


def load_yaml(path: Path) -> dict[str, Any]:
    """加载 YAML 文件."""
    with open(path) as f:
        return yaml.safe_load(f) or {}


def deep_merge(base: dict, override: dict) -> dict:
    """深度合并两个字典, override 覆盖 base."""
    result = base.copy()
    for key, value in override.items():
        if key in result and isinstance(result[key], dict) and isinstance(value, dict):
            result[key] = deep_merge(result[key], value)
        else:
            result[key] = value
    return result


class EnvSettings(BaseSettings):
    """从环境变量读取的敏感配置."""

    model_config = SettingsConfigDict(
        env_file=str(BASE_DIR / ".env"),
        case_sensitive=False,
        extra="ignore",
    )

    env: str = "dev"
    binance_api_key: str = ""
    binance_api_secret: str = ""
    telegram_bot_token: str = ""
    telegram_chat_id: str = ""
    prometheus_port: int = 9090


class RiskConfig(BaseModel):
    """风控配置."""

    enabled: bool = True
    mode: str = "soft"
    pre_trade: dict[str, Any] = {}
    real_time: dict[str, Any] = {}
    circuit_breaker: dict[str, Any] = {}
    post_trade: dict[str, Any] = {}


class ExecutionConfig(BaseModel):
    """执行引擎配置."""

    default_time_in_force: str = "GTC"
    default_order_type: str = "MARKET"
    slippage: dict[str, Any] = {}
    cost: dict[str, Any] = {}
    rate_limit: dict[str, Any] = {}
    algo: dict[str, Any] = {}


class MonitoringConfig(BaseModel):
    """监控配置."""

    enabled: bool = False
    prometheus_port: int = 9090
    alerting: dict[str, Any] = {}


class AccountConfig(BaseModel):
    """账户配置."""

    name: str = "binance_futures_main"
    venue: str = "BINANCE"
    account_type: str = "MARGIN"
    oms_type: str = "HEDGING"
    base_currency: str = "USDT"
    starting_balance: int = 10000
    max_leverage: int = 10


class DataConfig(BaseModel):
    """数据配置."""

    catalog_dir: Path = BASE_DIR / "data" / "processed" / "catalog"
    raw_dir: Path = BASE_DIR / "data" / "raw"
    features_dir: Path = BASE_DIR / "data" / "features"
    database_url: str = "postgresql://admin:Longmao!666@127.0.0.1:5432/nautilus_trader"


class RedisConfig(BaseModel):
    """Redis 连接配置."""

    host: str = "127.0.0.1"
    port: int = 6379
    password: str = ""
    db: int = 0
    socket_timeout: float = 2.0
    socket_connect_timeout: float = 2.0

    @property
    def url(self) -> str:
        """构造 redis-py 连接 URL."""
        if self.password:
            return f"redis://:{self.password}@{self.host}:{self.port}/{self.db}"
        return f"redis://{self.host}:{self.port}/{self.db}"


class AppConfig(BaseModel):
    """应用总配置, 聚合所有子配置."""

    env: str = "dev"
    data: DataConfig = DataConfig()
    redis: RedisConfig = RedisConfig()
    risk: RiskConfig = RiskConfig()
    execution: ExecutionConfig = ExecutionConfig()
    monitoring: MonitoringConfig = MonitoringConfig()
    account: AccountConfig = AccountConfig()
    strategies: dict[str, Any] = {}


def load_app_config(env: str | None = None) -> AppConfig:
    """加载并合并所有配置文件.

    优先级: env yaml > 模块 yaml > 默认值
    """
    env_settings = EnvSettings()
    current_env = env or env_settings.env

    # 1. 加载环境配置
    env_file = CONFIGS_DIR / "env" / f"{current_env}.yaml"
    env_cfg = load_yaml(env_file) if env_file.exists() else {}

    # 2. 加载模块配置
    risk_file = CONFIGS_DIR / "risk" / "global_risk.yaml"
    risk_cfg = load_yaml(risk_file).get("risk", {}) if risk_file.exists() else {}

    exec_file = CONFIGS_DIR / "execution" / "execution.yaml"
    exec_cfg = load_yaml(exec_file).get("execution", {}) if exec_file.exists() else {}

    account_file = CONFIGS_DIR / "accounts" / "binance_futures.yaml"
    account_cfg = load_yaml(account_file).get("account", {}) if account_file.exists() else {}

    alerts_file = CONFIGS_DIR / "monitoring" / "alerts.yaml"
    alerts_cfg = load_yaml(alerts_file) if alerts_file.exists() else {}

    # 3. 合并
    merged_risk = deep_merge(risk_cfg, env_cfg.get("risk", {}))
    merged_monitoring = deep_merge(
        env_cfg.get("monitoring", {}),
        {"alerting": alerts_cfg.get("alerting", {})},
    )

    return AppConfig(
        env=current_env,
        data=DataConfig(**env_cfg.get("data", {})) if env_cfg.get("data") else DataConfig(),
        redis=RedisConfig(**env_cfg.get("redis", {})) if env_cfg.get("redis") else RedisConfig(),
        risk=RiskConfig(**merged_risk) if merged_risk else RiskConfig(),
        execution=ExecutionConfig(**exec_cfg) if exec_cfg else ExecutionConfig(),
        monitoring=MonitoringConfig(**merged_monitoring) if merged_monitoring else MonitoringConfig(),
        account=AccountConfig(**account_cfg) if account_cfg else AccountConfig(),
    )
