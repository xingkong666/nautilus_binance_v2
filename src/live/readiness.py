"""实盘预检辅助逻辑."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

import structlog

from src.core.config import AppConfig, EnvSettings, load_yaml
from src.core.constants import CONFIGS_DIR

logger = structlog.get_logger()

_STABLECOIN_ASSETS = frozenset(
    {
        "USDT",
        "USDC",
        "FDUSD",
        "BUSD",
        "TUSD",
        "USDP",
        "DAI",
        "USDS",
        "UST",
        "USTC",
        "USD0",
        "USD1",
        "PYUSD",
        "GUSD",
        "EURC",
    }
)


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
    """解析策略配置路径.

    Args:
        config: Configuration object for the operation.
        override: Explicit override value supplied by the caller.
        cwd: Current working directory for path resolution.
    """
    raw_path = override or config.live.strategy_config
    if not raw_path:
        raise ValueError("Missing live strategy config path.")

    path = Path(raw_path).expanduser()
    base_dir = cwd or Path.cwd()
    if not path.is_absolute():
        path = base_dir / path
    return path.resolve()


def resolve_live_symbol(config: AppConfig, override: str = "") -> str:
    """解析 live 交易对.

    Args:
        config: Configuration object for the operation.
        override: Explicit override value supplied by the caller.
    """
    return override or config.live.symbol


def _normalize_symbol_list(raw_symbols: list[str] | tuple[str, ...] | None) -> list[str]:
    normalized: list[str] = []
    seen: set[str] = set()
    for raw_symbol in raw_symbols or []:
        symbol = str(raw_symbol).strip().upper()
        if not symbol or symbol in seen:
            continue
        seen.add(symbol)
        normalized.append(symbol)
    return normalized


def _extract_base_asset(symbol: str) -> str:
    normalized = symbol.strip().upper()
    for quote in sorted(_STABLECOIN_ASSETS, key=len, reverse=True):
        if normalized.endswith(quote) and len(normalized) > len(quote):
            return normalized[: -len(quote)]
    return normalized


def _is_stablecoin_base_symbol(symbol: str) -> bool:
    return _extract_base_asset(symbol) in _STABLECOIN_ASSETS


def _resolve_instruments_config_path(path: Path | None = None) -> Path:
    return (path or (CONFIGS_DIR / "instruments.yaml")).expanduser().resolve()


def _load_ranked_instrument_symbols(path: Path | None = None) -> list[str]:
    instruments_path = _resolve_instruments_config_path(path)
    if not instruments_path.exists():
        return []
    raw = load_yaml(instruments_path)
    instruments = raw.get("instruments", {})
    if not isinstance(instruments, dict):
        return []

    ranked: list[tuple[int, int, str]] = []
    for index, (symbol, metadata) in enumerate(instruments.items()):
        rank = index + 1
        if isinstance(metadata, dict):
            raw_rank = metadata.get("market_cap_rank")
            try:
                if raw_rank is not None:
                    rank = int(str(raw_rank))
            except (TypeError, ValueError):
                rank = index + 1
        ranked.append((rank, index, str(symbol).strip().upper()))

    ranked.sort(key=lambda item: (item[0], item[1]))
    return _normalize_symbol_list([symbol for _, _, symbol in ranked])


def resolve_live_symbols(
    config: AppConfig,
    symbol_override: str = "",
    symbols_override: list[str] | None = None,
    instruments_config_path: Path | None = None,
) -> list[str]:
    """解析 live 交易对列表.

    优先级：
    1. CLI `--symbols`
    2. CLI `--symbol`
    3. `live.symbols`
    4. `live.symbol`
    5. `configs/instruments.yaml` 中按市值顺序配置的前 `live.universe_top_n` 个交易对
       （可选排除稳定币 base asset）

    Args:
        config: Configuration object for the operation.
        symbol_override: Symbol override.
        symbols_override: Symbols override.
        instruments_config_path: Path for instruments config.
    """
    override_symbols = _normalize_symbol_list(symbols_override)
    if override_symbols:
        return override_symbols

    single_override = str(symbol_override).strip().upper()
    if single_override:
        return [single_override]

    configured_symbols = _normalize_symbol_list(config.live.symbols)
    if configured_symbols:
        return configured_symbols

    configured_symbol = str(config.live.symbol).strip().upper()
    if configured_symbol:
        return [configured_symbol]

    ranked_symbols = _load_ranked_instrument_symbols(instruments_config_path)
    if config.live.exclude_stablecoin_bases:
        ranked_symbols = [symbol for symbol in ranked_symbols if not _is_stablecoin_base_symbol(symbol)]

    top_n = max(int(config.live.universe_top_n), 0)
    if top_n > 0:
        ranked_symbols = ranked_symbols[:top_n]

    if not ranked_symbols:
        raise ValueError("No live symbols resolved. Configure live.symbol(s) or configs/instruments.yaml.")

    return ranked_symbols


def required_credential_env_names(config: AppConfig) -> tuple[str, str]:
    """根据运行环境返回需要的 Binance 凭证环境变量名.

    Args:
        config: Configuration object for the operation.
    """
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
    """检查 Binance 凭证是否存在.

    Args:
        config: Configuration object for the operation.
    """
    key_name, secret_name = required_credential_env_names(config)
    try:
        settings = EnvSettings()
    except Exception as exc:
        logger.error("env_settings_load_failed_readiness", error=str(exc), exc_info=True)
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


def collect_live_readiness_checks(
    config: AppConfig,
    strategy_override: str = "",
    symbol_override: str = "",
    symbols_override: list[str] | None = None,
    cwd: Path | None = None,
) -> tuple[list[ReadinessCheck], Path, list[str]]:
    """收集 live 启动前的关键预检结果."""
    checks = credential_checks(config)
    strategy_config_path = resolve_strategy_config_path(config, override=strategy_override, cwd=cwd)
    checks.append(
        ReadinessCheck(
            name="strategy_config_exists",
            passed=strategy_config_path.exists(),
            detail=str(strategy_config_path),
        )
    )

    live_symbols: list[str] = []
    symbol_error: Exception | None = None
    try:
        live_symbols = resolve_live_symbols(
            config=config,
            symbol_override=symbol_override,
            symbols_override=symbols_override,
        )
    except Exception as exc:
        symbol_error = exc

    checks.append(
        ReadinessCheck(
            name="live_symbols_resolved",
            passed=symbol_error is None and len(live_symbols) > 0,
            detail=",".join(live_symbols[:5]) if symbol_error is None else str(symbol_error),
        )
    )
    return checks, strategy_config_path, live_symbols


def ensure_live_readiness(
    config: AppConfig,
    strategy_override: str = "",
    symbol_override: str = "",
    symbols_override: list[str] | None = None,
    cwd: Path | None = None,
) -> tuple[Path, list[str]]:
    """验证 live 启动预检，失败时抛异常阻断启动."""
    checks, strategy_config_path, live_symbols = collect_live_readiness_checks(
        config=config,
        strategy_override=strategy_override,
        symbol_override=symbol_override,
        symbols_override=symbols_override,
        cwd=cwd,
    )
    failed = [check for check in checks if not check.passed]
    if failed:
        details = "; ".join(f"{check.name}={check.detail}" for check in failed)
        raise RuntimeError(f"Live readiness failed: {details}")
    return strategy_config_path, live_symbols
