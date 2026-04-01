"""测试 FillHandler 与 PostTradeAnalyzer 的集成."""

from decimal import Decimal
from unittest.mock import MagicMock

import pytest

from src.core.events import EventBus
from src.execution.fill_handler import FillHandler
from src.risk.post_trade import PostTradeAnalyzer
from src.state.persistence import TradePersistence


@pytest.fixture
def event_bus():
    """Create event bus fixture."""
    return EventBus()


@pytest.fixture
def persistence():
    """Create mock persistence fixture."""
    return MagicMock(spec=TradePersistence)


@pytest.fixture
def post_trade_analyzer():
    """Create post trade analyzer fixture."""
    return PostTradeAnalyzer()


@pytest.fixture
def fill_handler(event_bus, persistence, post_trade_analyzer):
    """Create fill handler with post trade analyzer."""
    return FillHandler(
        event_bus=event_bus,
        persistence=persistence,
        post_trade_analyzer=post_trade_analyzer,
    )


def test_fill_handler_records_trade_to_post_trade_analyzer(fill_handler, post_trade_analyzer):
    """测试 FillHandler 向 PostTradeAnalyzer 记录交易."""
    # Given
    instrument_id = "BTCUSDT"
    side = "BUY"
    quantity = "1.5"
    price = "45000.0"
    fees = "10.0"

    # When
    fill_handler.on_fill(
        instrument_id=instrument_id,
        side=side,
        quantity=quantity,
        price=price,
        fees=fees,
    )

    # Then
    assert len(post_trade_analyzer._trades) == 1
    trade = post_trade_analyzer._trades[0]

    assert trade.instrument_id == instrument_id
    assert trade.side == side
    assert trade.quantity == Decimal(quantity)
    assert trade.entry_price == Decimal(price)
    assert trade.exit_price == Decimal(price)
    assert trade.fees == Decimal(fees)
    assert trade.pnl == Decimal("0")
    assert trade.slippage_bps == 0.0
    assert trade.duration_seconds == 0.0


def test_fill_handler_generates_report_with_one_trade(fill_handler, post_trade_analyzer):
    """测试处理一笔成交后生成报告."""
    # Given
    fill_handler.on_fill(
        instrument_id="ETHUSDT",
        side="SELL",
        quantity="2.0",
        price="3000.0",
        fees="5.0",
    )

    # When
    report = post_trade_analyzer.generate_report("2024-01-01")

    # Then
    assert report.total_trades == 1
    assert report.winning_trades == 0  # Zero PnL means not winning
    assert report.losing_trades == 1  # Zero PnL counts as losing
    assert report.total_pnl == Decimal("0")
    assert report.total_fees == Decimal("5.0")
    assert report.net_pnl == Decimal("-5.0")  # Negative due to fees


def test_fill_handler_without_post_trade_analyzer(event_bus, persistence):
    """测试没有 PostTradeAnalyzer 的 FillHandler 正常工作."""
    # Given
    fill_handler = FillHandler(
        event_bus=event_bus,
        persistence=persistence,
        post_trade_analyzer=None,
    )

    # When - should not raise exception
    fill_handler.on_fill(
        instrument_id="ADAUSDT",
        side="BUY",
        quantity="100.0",
        price="1.5",
        fees="0.15",
    )

    # Then - persistence should still be called
    persistence.record_trade.assert_called_once()


def test_fill_handler_handles_post_trade_analyzer_exception(event_bus, persistence):
    """测试 PostTradeAnalyzer 异常时 FillHandler 仍正常工作."""
    # Given
    mock_analyzer = MagicMock(spec=PostTradeAnalyzer)
    mock_analyzer.record_trade.side_effect = ValueError("Test error")

    fill_handler = FillHandler(
        event_bus=event_bus,
        persistence=persistence,
        post_trade_analyzer=mock_analyzer,
    )

    # When - should not raise exception despite analyzer error
    fill_handler.on_fill(
        instrument_id="DOTUSDT",
        side="SELL",
        quantity="50.0",
        price="25.0",
        fees="1.25",
    )

    # Then - persistence should still be called
    persistence.record_trade.assert_called_once()
    mock_analyzer.record_trade.assert_called_once()
