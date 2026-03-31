from __future__ import annotations

# ruff: noqa: D100,D103
from decimal import Decimal

import pytest

from src.risk.drawdown_control import DrawdownController


def test_current_drawdown_pct_returns_five_percent_after_drop() -> None:
    controller = DrawdownController()
    controller.update_equity(Decimal("10000"))
    controller.update_equity(Decimal("9500"))

    assert controller.current_drawdown_pct == pytest.approx(5.0, abs=0.01)


def test_current_drawdown_pct_returns_zero_at_peak() -> None:
    controller = DrawdownController()
    controller.update_equity(Decimal("10000"))

    assert controller.current_drawdown_pct == pytest.approx(0.0, abs=0.01)
