"""做市商队列位置与成交概率模型."""

from __future__ import annotations

from typing import Any


class QueueModelMixin:
    """提供队列位置、排队惩罚和成交概率估算逻辑."""

    def _estimate_queue_ahead(self: Any, side: str) -> float:
        """当前价位排队量估算（用 best_bid/ask_size 作为代理）."""
        if side == "BUY":
            return self._last_best_bid_size or 0.0
        return self._last_best_ask_size or 0.0

    def _calc_queue_penalty(self: Any, side: str) -> float:
        """队列越长惩罚越大，归一化到 [0, 1]."""
        queue = self._estimate_queue_ahead(side)
        return min(queue / max(self.config.queue_norm_volume, 1.0), 1.0)

    def _calc_queue_fill_prob(self: Any, side: str) -> float:
        """下单后的队列消耗估算成交概率：traded_volume / initial_queue."""
        qs = self._quote_state
        initial = qs.bid_queue_on_submit if side == "BUY" else qs.ask_queue_on_submit
        if initial is None or initial <= 0:
            return 1.0
        return min(self._queue_traded_volume / initial, 1.0)
