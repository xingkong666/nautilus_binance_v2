"""统一配置加载.

支持多环境(dev/stage/prod) + 分模块配置合并.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, Field
from pydantic_settings import BaseSettings, SettingsConfigDict

from src.core.constants import BASE_DIR, CONFIGS_DIR


def load_yaml(path: Path) -> dict[str, Any]:
    """加载 YAML 文件.

    Args:
        path: Filesystem path used by the operation.
    """
    with open(path) as f:
        return yaml.safe_load(f) or {}


def deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    """深度合并两个字典, override 覆盖 base.

    Args:
        base: Base.
        override: Explicit override value supplied by the caller.
    """
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
    binance_testnet_api_key: str = ""
    binance_testnet_api_secret: str = ""
    binance_demo_api_key: str = ""
    binance_demo_api_secret: str = ""
    telegram_bot_token: str = ""
    telegram_chat_id: str = ""
    prometheus_port: int | None = None
    database_url: str = ""
    redis_host: str = ""
    redis_port: int | None = None
    redis_password: str = ""
    redis_db: int | None = None
    live_strategy_config: str = ""
    submit_orders: bool | None = None
    exchange_environment: str = ""


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

    submit_orders: bool = True
    default_time_in_force: str = "GTC"
    default_order_type: str = "MARKET"
    slippage: dict[str, Any] = {}
    cost: dict[str, Any] = {}
    funding: dict[str, Any] = {}
    rate_limit: dict[str, Any] = {}
    algo: dict[str, Any] = {}


class MonitoringConfig(BaseModel):
    """监控配置."""

    enabled: bool = False
    prometheus_port: int = 9090
    alerting: dict[str, Any] = {}


class LiveConfig(BaseModel):
    """实盘启动配置."""

    strategy_config: str = ""
    symbol: str = ""
    symbols: list[str] = []
    universe_top_n: int = 200
    exclude_stablecoin_bases: bool = True
    timeout_seconds: float = 0.0


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
    database_url: str = "postgresql://postgres:postgres@127.0.0.1:5432/nautilus_trader"


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


class CacheModeConfig(BaseModel):
    """按运行模式覆盖的 cache 配置."""

    use_instance_id: bool | None = None
    flush_on_start: bool | None = None
    drop_instruments_on_reset: bool | None = None


class NautilusCacheConfig(BaseModel):
    """NautilusTrader Cache 配置."""

    enabled: bool = True
    database: str = "redis"
    encoding: str = "msgpack"
    timestamps_as_iso8601: bool = False
    persist_account_events: bool = True
    buffer_interval_ms: int | None = None
    bulk_read_batch_size: int | None = None
    use_trader_prefix: bool = True
    use_instance_id: bool = False
    flush_on_start: bool = False
    drop_instruments_on_reset: bool = True
    tick_capacity: int = 10_000
    bar_capacity: int = 10_000
    database_timeout: int = 20
    live: CacheModeConfig = Field(default_factory=CacheModeConfig)
    backtest: CacheModeConfig = Field(
        default_factory=lambda: CacheModeConfig(
            use_instance_id=True,
            flush_on_start=False,
            drop_instruments_on_reset=True,
        )
    )


class AppConfig(BaseModel):
    """应用总配置, 聚合所有子配置."""

    env: str = "dev"
    data: DataConfig = DataConfig()
    redis: RedisConfig = RedisConfig()
    cache: NautilusCacheConfig = Field(default_factory=NautilusCacheConfig)
    risk: RiskConfig = RiskConfig()
    execution: ExecutionConfig = ExecutionConfig()
    monitoring: MonitoringConfig = MonitoringConfig()
    live: LiveConfig = LiveConfig()
    account: AccountConfig = AccountConfig()
    exchange: dict[str, Any] = {}
    strategies: dict[str, Any] = {}


def load_app_config(env: str | None = None) -> AppConfig:
    """加载并合并所有配置文件.

    优先级: env yaml > 模块 yaml > 默认值

    Args:
        env: Environment config path or environment name.
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
    merged_execution = deep_merge(exec_cfg, env_cfg.get("execution", {}))
    merged_monitoring = deep_merge(
        env_cfg.get("monitoring", {}),
        {"alerting": alerts_cfg.get("alerting", {})},
    )
    merged_data = deep_merge(env_cfg.get("data", {}), _env_data_overrides(env_settings))
    merged_redis = deep_merge(env_cfg.get("redis", {}), _env_redis_overrides(env_settings))
    merged_execution = deep_merge(merged_execution, _env_execution_overrides(env_settings))
    merged_monitoring = deep_merge(merged_monitoring, _env_monitoring_overrides(env_settings))
    merged_live = deep_merge(env_cfg.get("live", {}), _env_live_overrides(env_settings))
    merged_exchange = deep_merge(env_cfg.get("exchange", {}), _env_exchange_overrides(env_settings))
    merged_cache = env_cfg.get("cache", {})

    return AppConfig(
        env=current_env,
        data=DataConfig(**merged_data) if merged_data else DataConfig(),
        redis=RedisConfig(**merged_redis) if merged_redis else RedisConfig(),
        cache=NautilusCacheConfig(**merged_cache) if merged_cache else NautilusCacheConfig(),
        risk=RiskConfig(**merged_risk) if merged_risk else RiskConfig(),
        execution=ExecutionConfig(**merged_execution) if merged_execution else ExecutionConfig(),
        monitoring=MonitoringConfig(**merged_monitoring) if merged_monitoring else MonitoringConfig(),
        live=LiveConfig(**merged_live) if merged_live else LiveConfig(),
        account=AccountConfig(**account_cfg) if account_cfg else AccountConfig(),
        exchange=dict(merged_exchange),
        strategies=dict(env_cfg.get("strategies", {})),
    )


def _env_data_overrides(env_settings: EnvSettings) -> dict[str, Any]:
    overrides: dict[str, Any] = {}
    database_url = getattr(env_settings, "database_url", "")
    if database_url:
        overrides["database_url"] = database_url
    return overrides


def _env_redis_overrides(env_settings: EnvSettings) -> dict[str, Any]:
    overrides: dict[str, Any] = {}
    redis_host = getattr(env_settings, "redis_host", "")
    redis_port = getattr(env_settings, "redis_port", None)
    redis_password = getattr(env_settings, "redis_password", "")
    redis_db = getattr(env_settings, "redis_db", None)
    if redis_host:
        overrides["host"] = redis_host
    if redis_port is not None:
        overrides["port"] = redis_port
    if redis_password:
        overrides["password"] = redis_password
    if redis_db is not None:
        overrides["db"] = redis_db
    return overrides


def _env_execution_overrides(env_settings: EnvSettings) -> dict[str, Any]:
    overrides: dict[str, Any] = {}
    submit_orders = getattr(env_settings, "submit_orders", None)
    if submit_orders is not None:
        overrides["submit_orders"] = submit_orders
    return overrides


def _env_monitoring_overrides(env_settings: EnvSettings) -> dict[str, Any]:
    overrides: dict[str, Any] = {}
    prometheus_port = getattr(env_settings, "prometheus_port", None)
    if prometheus_port is not None:
        overrides["prometheus_port"] = prometheus_port
    return overrides


def _env_live_overrides(env_settings: EnvSettings) -> dict[str, Any]:
    overrides: dict[str, Any] = {}
    strategy_config = getattr(env_settings, "live_strategy_config", "")
    if strategy_config:
        overrides["strategy_config"] = strategy_config
    return overrides


def _env_exchange_overrides(env_settings: EnvSettings) -> dict[str, Any]:
    overrides: dict[str, Any] = {}
    environment = str(getattr(env_settings, "exchange_environment", "")).strip().upper()
    if environment:
        overrides["environment"] = environment
    return overrides
