"""时钟同步工具.

确保本地时间与交易所时间偏差在可接受范围内.
"""

from __future__ import annotations

import time

import httpx
import structlog

logger = structlog.get_logger(__name__)

# 最大可接受偏差 (毫秒)
MAX_OFFSET_MS = 500


async def check_binance_time_offset() -> int:
    """检查本地时间与 Binance 服务器的偏差.

    Returns:
        偏差毫秒数 (正=本地快, 负=本地慢)

    """
    async with httpx.AsyncClient() as client:
        local_before_ms = int(time.time() * 1000)
        resp = await client.get("https://fapi.binance.com/fapi/v1/time", timeout=5.0)
        local_after_ms = int(time.time() * 1000)

        payload = resp.json()
        if not isinstance(payload, dict):
            raise RuntimeError("Invalid Binance server time response")

        server_time_raw = payload.get("serverTime")
        if not isinstance(server_time_raw, int):
            raise RuntimeError("Binance response missing integer serverTime")

        server_time_ms = server_time_raw
        local_time_ms = (local_before_ms + local_after_ms) // 2
        offset_ms = local_time_ms - server_time_ms

        logger.info(
            "time_sync_check",
            offset_ms=offset_ms,
            within_limit=abs(offset_ms) <= MAX_OFFSET_MS,
        )

        return offset_ms


async def assert_time_synced() -> None:
    """断言时钟同步, 否则抛异常."""
    offset = await check_binance_time_offset()
    if abs(offset) > MAX_OFFSET_MS:
        msg = f"时钟偏差过大: {offset}ms (限制: ±{MAX_OFFSET_MS}ms)"
        logger.critical("time_sync_failed", offset_ms=offset)
        raise RuntimeError(msg)
