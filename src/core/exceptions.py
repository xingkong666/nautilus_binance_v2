"""Trading domain exception hierarchy.

TradingError (base)
├── ExecutionError   — order submission, routing, fill handling
├── RiskError        — pre/post trade risk, circuit breaker, drawdown
├── DataError        — data loading, feed, historical data issues
└── ConfigError      — configuration validation, missing fields
"""

from __future__ import annotations

from typing import Any


class TradingError(Exception):
    """Base class for all trading system errors."""

    def __init__(
        self,
        message: str,
        *,
        symbol: str | None = None,
        order_id: str | None = None,
        context: dict[str, Any] | None = None,
    ) -> None:
        """Initialize trading error with optional context fields."""
        super().__init__(message)
        self.symbol = symbol
        self.order_id = order_id
        self.context: dict[str, Any] = context or {}

    def __str__(self) -> str:
        """Return string representation with context fields."""
        parts = [super().__str__()]
        if self.symbol:
            parts.append(f"symbol={self.symbol}")
        if self.order_id:
            parts.append(f"order_id={self.order_id}")
        return " | ".join(parts)


class ExecutionError(TradingError):
    """Raised when order execution fails (routing, submission, fill handling)."""


class RiskError(TradingError):
    """Raised when a risk check fails or circuit breaker trips."""


class DataError(TradingError):
    """Raised when market data loading or feed processing fails."""


class ConfigError(TradingError):
    """Raised when configuration is invalid or missing required fields."""
