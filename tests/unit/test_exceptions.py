"""Tests for trading domain exception hierarchy."""

from src.core.exceptions import ConfigError, DataError, ExecutionError, RiskError, TradingError


def test_hierarchy():
    """Test exception class hierarchy relationships."""
    assert issubclass(ExecutionError, TradingError)
    assert issubclass(RiskError, TradingError)
    assert issubclass(DataError, TradingError)
    assert issubclass(ConfigError, TradingError)


def test_context_fields():
    """Test that symbol and order_id are stored and rendered in str()."""
    err = ExecutionError("order failed", symbol="BTCUSDT", order_id="123")
    assert err.symbol == "BTCUSDT"
    assert err.order_id == "123"
    assert "symbol=BTCUSDT" in str(err)


def test_trading_error_is_exception():
    """Test TradingError is a subclass of Exception."""
    err = TradingError("test")
    assert isinstance(err, Exception)


def test_context_dict():
    """Test that arbitrary context dict is stored on the exception."""
    err = RiskError("risk check failed", context={"position_size": 1000})
    assert err.context["position_size"] == 1000


def test_str_representation():
    """Test __str__ includes message, symbol and order_id."""
    err = DataError("feed timeout", symbol="ETHUSDT", order_id="456")
    str_repr = str(err)
    assert "feed timeout" in str_repr
    assert "symbol=ETHUSDT" in str_repr
    assert "order_id=456" in str_repr
