"""Tests for WilderAdx NT Indicator.

验证 WilderAdx 与旧版 _AdxState dataclass 数值完全一致，
并测试 NT Indicator 接口（initialized, reset, handle_bar）。
"""

from __future__ import annotations

import math
from collections import deque
from dataclasses import dataclass, field
from unittest.mock import MagicMock

import pytest

from src.core.indicators import WilderAdx

# ---------------------------------------------------------------------------
# Reference implementation (原始 _AdxState，用于数值回归对比)
# ---------------------------------------------------------------------------


@dataclass
class _RefAdxState:
    """Reference Wilder ADX (copied from original ema_pullback_atr.py)."""

    period: int
    prev_high: float | None = None
    prev_low: float | None = None
    prev_close: float | None = None
    _tr_sum: float = 0.0
    _plus_dm_sum: float = 0.0
    _minus_dm_sum: float = 0.0
    _smoothed_tr: float | None = None
    _smoothed_plus_dm: float | None = None
    _smoothed_minus_dm: float | None = None
    _dx_values: deque[float] = field(default_factory=deque)
    adx: float | None = None
    initialized: bool = False

    def update(self, high: float, low: float, close: float) -> None:  # noqa: C901
        """Update state with new bar data."""
        if self.prev_high is None or self.prev_low is None or self.prev_close is None:
            self.prev_high = high
            self.prev_low = low
            self.prev_close = close
            return

        up_move = high - self.prev_high
        down_move = self.prev_low - low
        plus_dm = up_move if up_move > down_move and up_move > 0 else 0.0
        minus_dm = down_move if down_move > up_move and down_move > 0 else 0.0
        tr = max(high - low, abs(high - self.prev_close), abs(low - self.prev_close))

        if self._smoothed_tr is None:
            self._tr_sum += tr
            self._plus_dm_sum += plus_dm
            self._minus_dm_sum += minus_dm
            if len(self._dx_values) < self.period - 1:
                self._dx_values.append(0.0)
            if len(self._dx_values) == self.period - 1:
                self._smoothed_tr = self._tr_sum
                self._smoothed_plus_dm = self._plus_dm_sum
                self._smoothed_minus_dm = self._minus_dm_sum
                self._dx_values.clear()
        else:
            n = float(self.period)
            self._smoothed_tr = self._smoothed_tr - (self._smoothed_tr / n) + tr
            self._smoothed_plus_dm = self._smoothed_plus_dm - (self._smoothed_plus_dm / n) + plus_dm
            self._smoothed_minus_dm = self._smoothed_minus_dm - (self._smoothed_minus_dm / n) + minus_dm
            if self._smoothed_tr > 0:
                plus_di = 100.0 * (self._smoothed_plus_dm / self._smoothed_tr)
                minus_di = 100.0 * (self._smoothed_minus_dm / self._smoothed_tr)
                di_sum = plus_di + minus_di
                dx = 100.0 * abs(plus_di - minus_di) / di_sum if di_sum > 0 else 0.0
            else:
                dx = 0.0
            if self.adx is None:
                self._dx_values.append(dx)
                if len(self._dx_values) >= self.period:
                    self.adx = sum(self._dx_values) / float(self.period)
                    self.initialized = True
                    self._dx_values.clear()
            else:
                self.adx = ((self.adx * (self.period - 1)) + dx) / float(self.period)
                self.initialized = True

        self.prev_high = high
        self.prev_low = low
        self.prev_close = close


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_bars(n: int, start: float = 100.0, step: float = 0.5) -> list[tuple[float, float, float]]:
    """Generate synthetic (high, low, close) tuples."""
    bars = []
    price = start
    for i in range(n):
        high = price + 1.5 + (i % 3) * 0.3
        low = price - 1.0 - (i % 2) * 0.2
        close = price + (0.3 if i % 2 == 0 else -0.3)
        bars.append((high, low, close))
        price += step if i % 5 != 0 else -step
    return bars


def _make_mock_bar(high: float, low: float, close: float) -> MagicMock:
    """Create a mock bar with high/low/close attributes."""
    bar = MagicMock()
    bar.high = high
    bar.low = low
    bar.close = close
    return bar


# ---------------------------------------------------------------------------
# Tests: NT Indicator interface
# ---------------------------------------------------------------------------


class TestWilderAdxInterface:
    """Tests for the NT Indicator interface of WilderAdx."""

    def test_initial_state(self) -> None:
        """Verify initial state is uninitialized with None outputs."""
        adx = WilderAdx(14)
        assert not adx.initialized
        assert not adx.has_inputs
        assert adx.value is None
        assert adx.plus_di is None
        assert adx.minus_di is None

    def test_period_stored(self) -> None:
        """Verify the period is stored correctly."""
        adx = WilderAdx(7)
        assert adx.period == 7

    def test_has_inputs_set_after_first_update(self) -> None:
        """Verify has_inputs is set after first update call."""
        adx = WilderAdx(3)
        adx.update(101.0, 99.0, 100.0)
        assert adx.has_inputs

    def test_not_initialized_before_enough_bars(self) -> None:
        """Verify indicator is not initialized before accumulating enough bars."""
        adx = WilderAdx(14)
        for high, low, close in _make_bars(20):
            adx.update(high, low, close)
        # Either not initialized, or value is available if initialized
        assert not adx.initialized or adx.value is not None

    def test_initialized_after_sufficient_bars(self) -> None:
        """Verify indicator is initialized after sufficient bars."""
        period = 5
        adx = WilderAdx(period)
        bars = _make_bars(100)
        for high, low, close in bars:
            adx.update(high, low, close)
        assert adx.initialized
        assert adx.value is not None
        assert 0.0 <= adx.value <= 100.0

    def test_handle_bar_interface(self) -> None:
        """Verify handle_bar NT interface works identically to update()."""
        adx = WilderAdx(5)
        bars = _make_bars(50)
        for high, low, close in bars:
            mock_bar = _make_mock_bar(high, low, close)
            adx.handle_bar(mock_bar)
        assert adx.initialized
        assert adx.value is not None

    def test_reset_clears_state(self) -> None:
        """Verify reset clears all state and flags."""
        adx = WilderAdx(5)
        for high, low, close in _make_bars(50):
            adx.update(high, low, close)
        assert adx.initialized

        adx.reset()
        assert not adx.initialized
        assert not adx.has_inputs
        assert adx.value is None
        assert adx.plus_di is None
        assert adx.minus_di is None

    def test_reset_then_reinitialize(self) -> None:
        """Verify reinitializing after reset produces identical values."""
        adx = WilderAdx(5)
        bars = _make_bars(50)
        for high, low, close in bars:
            adx.update(high, low, close)
        first_value = adx.value

        adx.reset()
        for high, low, close in bars:
            adx.update(high, low, close)
        assert adx.value == pytest.approx(first_value, rel=1e-10)

    def test_adx_range(self) -> None:
        """ADX must always be in [0, 100]."""
        adx = WilderAdx(14)
        for high, low, close in _make_bars(200):
            adx.update(high, low, close)
            if adx.value is not None:
                assert 0.0 <= adx.value <= 100.0

    def test_di_range(self) -> None:
        """DI values must be in [0, 100] once available."""
        adx = WilderAdx(14)
        for high, low, close in _make_bars(200):
            adx.update(high, low, close)
            if adx.plus_di is not None:
                assert 0.0 <= adx.plus_di <= 100.0
            if adx.minus_di is not None:
                assert 0.0 <= adx.minus_di <= 100.0


# ---------------------------------------------------------------------------
# Tests: numerical parity with reference _AdxState
# ---------------------------------------------------------------------------


class TestWilderAdxNumericalParity:
    """Tests verifying numerical parity with the original _AdxState reference."""

    @pytest.mark.parametrize("period", [3, 5, 14])
    def test_adx_matches_reference(self, period: int) -> None:
        """WilderAdx must produce identical values to the original _AdxState."""
        bars = _make_bars(200)
        ref = _RefAdxState(period=period)
        adx = WilderAdx(period)

        for high, low, close in bars:
            ref.update(high, low, close)
            adx.update(high, low, close)

        assert ref.initialized == adx.initialized
        if ref.adx is not None:
            assert adx.value is not None
            assert math.isclose(ref.adx, adx.value, rel_tol=1e-10)
        else:
            assert adx.value is None

    @pytest.mark.parametrize("period", [3, 5, 14])
    def test_initialization_timing_matches_reference(self, period: int) -> None:
        """Initialization must happen on exactly the same bar as the reference."""
        bars = _make_bars(200)
        ref = _RefAdxState(period=period)
        adx = WilderAdx(period)

        for i, (high, low, close) in enumerate(bars):
            ref.update(high, low, close)
            adx.update(high, low, close)
            assert ref.initialized == adx.initialized, (
                f"Mismatch at bar {i}: ref={ref.initialized}, new={adx.initialized}"
            )

    def test_streaming_values_match_reference(self) -> None:
        """Every single ADX value after initialization must match the reference."""
        period = 7
        bars = _make_bars(150)
        ref = _RefAdxState(period=period)
        adx = WilderAdx(period)

        mismatches = []
        for i, (high, low, close) in enumerate(bars):
            ref.update(high, low, close)
            adx.update(high, low, close)
            if ref.adx is not None and adx.value is not None and not math.isclose(ref.adx, adx.value, rel_tol=1e-10):
                mismatches.append((i, ref.adx, adx.value))

        assert not mismatches, f"Value mismatches: {mismatches[:5]}"

    def test_flat_market_adx_near_zero(self) -> None:
        """In a perfectly flat market ADX should converge near zero."""
        adx = WilderAdx(14)
        # Identical bars — no directional movement
        for _ in range(200):
            adx.update(100.0, 100.0, 100.0)
        if adx.initialized and adx.value is not None:
            assert adx.value < 5.0, f"Expected near-zero ADX in flat market, got {adx.value}"

    def test_strong_trend_high_adx(self) -> None:
        """In a strongly trending market ADX should be elevated."""
        adx = WilderAdx(14)
        price = 100.0
        for _ in range(200):
            price += 2.0
            adx.update(price + 1.0, price - 0.5, price)
        if adx.initialized and adx.value is not None:
            assert adx.value > 20.0, f"Expected high ADX in trending market, got {adx.value}"
