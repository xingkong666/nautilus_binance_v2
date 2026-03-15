"""实盘预检辅助逻辑."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from src.core.config import AppConfig, EnvSettings


@dataclass(frozen=True)
class ReadinessCheck:
    """单项预检结果."""

    name: str
    passed: bool
    detail: str


def resolve_strategy_config_path(
    config: AppConfig,
    override: str = "",
    cwd: Path | None = None,
) -> Path:
    """解析策略配置路径."""
    raw_path = override or config.live.strategy_config
    if not raw_path:
        raise ValueError("Missing live strategy config path.")

    path = Path(raw_path).expanduser()
    base_dir = cwd or Path.cwd()
    if not path.is_absolute():
        path = base_dir / path
    return path.resolve()


def resolve_live_symbol(config: AppConfig, override: str = "") -> str:
    """解析 live 交易对."""
    return override or config.live.symbol


def required_credential_env_names(config: AppConfig) -> tuple[str, str]:
    """根据运行环境返回需要的 Binance 凭证环境变量名."""
    exchange_env = str(
        config.exchange.get(
            "environment",
            "LIVE" if config.env == "prod" else "TESTNET",
        )
    ).upper()
    if exchange_env == "LIVE":
        return "BINANCE_API_KEY", "BINANCE_API_SECRET"
    if exchange_env == "DEMO":
        return "BINANCE_DEMO_API_KEY", "BINANCE_DEMO_API_SECRET"
    return "BINANCE_TESTNET_API_KEY", "BINANCE_TESTNET_API_SECRET"


def credential_checks(config: AppConfig) -> list[ReadinessCheck]:
    """检查 Binance 凭证是否存在."""
    key_name, secret_name = required_credential_env_names(config)
    try:
        settings = EnvSettings()
    except Exception:
        settings = None

    def _present(env_name: str) -> bool:
        if os.environ.get(env_name):
            return True
        if settings is None:
            return False
        mapping = {
            "BINANCE_API_KEY": settings.binance_api_key,
            "BINANCE_API_SECRET": settings.binance_api_secret,
            "BINANCE_TESTNET_API_KEY": settings.binance_testnet_api_key,
            "BINANCE_TESTNET_API_SECRET": settings.binance_testnet_api_secret,
            "BINANCE_DEMO_API_KEY": settings.binance_demo_api_key,
            "BINANCE_DEMO_API_SECRET": settings.binance_demo_api_secret,
        }
        return bool(mapping.get(env_name, ""))

    checks = [
        ReadinessCheck(
            name="binance_api_key",
            passed=_present(key_name),
            detail=key_name,
        ),
        ReadinessCheck(
            name="binance_api_secret",
            passed=_present(secret_name),
            detail=secret_name,
        ),
    ]
    return checks
