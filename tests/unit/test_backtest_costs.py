"""Tests for test backtest costs."""

from __future__ import annotations

from types import SimpleNamespace

import pandas as pd

from src.backtest.costs import BacktestCostAnalyzer


def _make_reports(commissions: str) -> dict[str, pd.DataFrame]:
    fills = pd.DataFrame(
        [
            {
                "filled_qty": "1",
                "quantity": "1",
                "avg_px": "100",
                "liquidity_side": "TAKER",
                "commissions": commissions,
            }
        ]
    )
    positions = pd.DataFrame(
        [
            {
                "instrument_id": "BTCUSDT-PERP.BINANCE",
                "entry": "BUY",
                "peak_qty": "1",
                "avg_px_open": "100",
                "ts_opened": "2024-01-01 00:00:00+00:00",
                "ts_closed": "2024-01-01 12:00:00+00:00",
            }
        ]
    )
    return {"order_fills": fills, "positions": positions}


def _make_analyzer(tmp_path) -> BacktestCostAnalyzer:
    raw_dir = tmp_path / "raw"
    funding_dir = raw_dir / "funding"
    funding_dir.mkdir(parents=True, exist_ok=True)
    pd.DataFrame(
        [
            {
                "timestamp": "2024-01-01T08:00:00+00:00",
                "funding_rate": 0.0001,
                "mark_price": 100,
            }
        ]
    ).to_csv(funding_dir / "BTCUSDT.csv", index=False)

    execution_config = SimpleNamespace(
        cost={"maker_fee_bps": 2, "taker_fee_bps": 4},
        slippage={"model": "fixed", "fixed_bps": 2},
        funding={"enabled": True},
    )
    return BacktestCostAnalyzer(
        execution_config=execution_config,
        raw_dir=raw_dir,
        features_dir=tmp_path / "features",
    )


def test_cost_analyzer_uses_reported_commissions_without_double_counting(tmp_path) -> None:
    """Verify that cost analyzer uses reported commissions without double counting.

    Args:
        tmp_path: Path for tmp.
    """
    analyzer = _make_analyzer(tmp_path)
    reports = _make_reports("['0.04000000 USDT']")

    analysis = analyzer.analyze(reports=reports, starting_balance=1000, pnl_stats={"PnL (total)": "10"})

    assert analysis is not None
    assert analysis.commissions_source == "reported"
    assert float(analysis.commissions_total) == 0.04
    assert float(analysis.modeled_fee_cost) == 0.04
    assert float(analysis.modeled_slippage_cost) == 0.02
    assert float(analysis.funding_cost) == 0.01
    assert float(analysis.additional_cost_applied) == 0.03
    assert float(analysis.pnl_after_costs) == 9.97


def test_cost_analyzer_falls_back_to_modeled_fee_when_report_missing(tmp_path) -> None:
    """Verify that cost analyzer falls back to modeled fee when report missing.

    Args:
        tmp_path: Path for tmp.
    """
    analyzer = _make_analyzer(tmp_path)
    reports = _make_reports("")

    analysis = analyzer.analyze(reports=reports, starting_balance=1000, pnl_stats={"PnL (total)": "10"})

    assert analysis is not None
    assert analysis.commissions_source == "modeled"
    assert float(analysis.commissions_total) == 0.0
    assert float(analysis.modeled_fee_cost) == 0.04
    assert float(analysis.additional_cost_applied) == 0.07
    assert float(analysis.pnl_after_costs) == 9.93
