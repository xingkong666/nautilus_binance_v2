"""Live 策略历史预热."""

from __future__ import annotations

import re
from collections.abc import Iterable

import httpx
import structlog
from nautilus_trader.adapters.binance.common.enums import BinanceEnvironment
from nautilus_trader.model.data import Bar, BarType
from nautilus_trader.model.objects import Price, Quantity

from src.strategy.base import BaseStrategy

logger = structlog.get_logger(__name__)

_BAR_SPEC_RE = re.compile(r"(?P<count>\d+)-(?P<unit>SECOND|MINUTE|HOUR|DAY|WEEK|MONTH)")
_BINANCE_FUTURES_HTTP_BASE_URLS: dict[BinanceEnvironment, str] = {
    BinanceEnvironment.LIVE: "https://fapi.binance.com",
    BinanceEnvironment.TESTNET: "https://testnet.binancefuture.com",
    BinanceEnvironment.DEMO: "https://fapi.binance.com",
}
_UNIT_TO_BINANCE_INTERVAL_SUFFIX = {
    "MINUTE": "m",
    "HOUR": "h",
    "DAY": "d",
    "WEEK": "w",
    "MONTH": "M",
}


def bar_type_to_binance_interval(bar_type: BarType) -> str | None:
    """将 Nautilus BarType 转为 Binance K 线 interval。."""
    spec = str(bar_type.spec)
    match = _BAR_SPEC_RE.match(spec)
    if match is None:
        return None

    unit = match.group("unit")
    suffix = _UNIT_TO_BINANCE_INTERVAL_SUFFIX.get(unit)
    if suffix is None:
        return None

    return f"{int(match.group('count'))}{suffix}"


def fetch_binance_futures_bars(
    *,
    symbol: str,
    bar_type: BarType,
    interval: str,
    limit: int,
    environment: BinanceEnvironment,
    base_url_http: str | None = None,
    timeout: float = 10.0,
) -> list[Bar]:
    """从 Binance Futures REST 拉取最近一段 K 线并转换为 Nautilus Bar。."""
    if limit <= 0:
        return []

    base_url = (base_url_http or _BINANCE_FUTURES_HTTP_BASE_URLS[environment]).rstrip("/")
    response = httpx.get(
        f"{base_url}/fapi/v1/klines",
        params={
            "symbol": symbol,
            "interval": interval,
            "limit": limit,
        },
        timeout=timeout,
    )
    response.raise_for_status()

    bars: list[Bar] = []
    for item in response.json():
        close_time_ns = (int(item[6]) + 1) * 1_000_000
        bars.append(
            Bar(
                bar_type=bar_type,
                open=Price.from_str(str(item[1])),
                high=Price.from_str(str(item[2])),
                low=Price.from_str(str(item[3])),
                close=Price.from_str(str(item[4])),
                volume=Quantity.from_str(str(item[5])),
                ts_event=close_time_ns,
                ts_init=close_time_ns,
            )
        )

    return bars


def preload_strategy_warmup(
    strategy: BaseStrategy,
    *,
    environment: BinanceEnvironment,
    base_url_http: str | None = None,
    timeout: float = 10.0,
) -> int:
    """为单个 live 策略预加载历史 bars。."""
    warmup_bars = strategy._resolved_warmup_bars()
    if warmup_bars <= 0:
        return 0

    interval = bar_type_to_binance_interval(strategy.config.bar_type)
    if interval is None:
        logger.warning(
            "live_strategy_warmup_skipped_unsupported_bar_type",
            instrument_id=str(strategy.config.instrument_id),
            bar_type=str(strategy.config.bar_type),
        )
        return 0

    margin_bars = max(0, int(getattr(strategy.config, "live_warmup_margin_bars", 5)))
    request_limit = warmup_bars + margin_bars
    symbol = str(strategy.config.instrument_id).split("-", 1)[0]
    logger.info(
        "live_strategy_warmup_requesting",
        strategy=strategy.__class__.__name__,
        instrument_id=str(strategy.config.instrument_id),
        bar_type=str(strategy.config.bar_type),
        binance_interval=interval,
        warmup_bars=warmup_bars,
        margin_bars=margin_bars,
        request_limit=request_limit,
        environment=environment.value,
    )
    bars = fetch_binance_futures_bars(
        symbol=symbol,
        bar_type=strategy.config.bar_type,
        interval=interval,
        limit=request_limit,
        environment=environment,
        base_url_http=base_url_http,
        timeout=timeout,
    )
    if not bars:
        return 0

    loaded = strategy.preload_history(bars)
    logger.info(
        "live_strategy_warmup_preloaded",
        strategy=strategy.__class__.__name__,
        instrument_id=str(strategy.config.instrument_id),
        bar_type=str(strategy.config.bar_type),
        binance_interval=interval,
        requested_bars=request_limit,
        loaded_bars=loaded,
    )
    return loaded


def preload_strategies_warmup(
    strategies: Iterable[BaseStrategy],
    *,
    environment: BinanceEnvironment,
    base_url_http: str | None = None,
    timeout: float = 10.0,
) -> dict[str, int]:
    """为多个 live 策略预加载历史 bars，失败时降级为 warning。."""
    results: dict[str, int] = {}
    for strategy in strategies:
        instrument_id = str(strategy.config.instrument_id)
        try:
            results[instrument_id] = preload_strategy_warmup(
                strategy,
                environment=environment,
                base_url_http=base_url_http,
                timeout=timeout,
            )
        except Exception as exc:
            logger.warning(
                "live_strategy_warmup_failed",
                strategy=strategy.__class__.__name__,
                instrument_id=instrument_id,
                error=str(exc),
            )
            results[instrument_id] = 0

    logger.info("live_strategy_warmup_summary", results=results)
    return results
