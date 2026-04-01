"""Test CircuitBreaker four-level state machine."""

import time
from unittest.mock import MagicMock

import pytest

from src.core.events import EventBus
from src.risk.circuit_breaker import CircuitBreaker, CircuitLevel


def test_circuit_level_enum():
    """Test CircuitLevel enum values."""
    assert CircuitLevel.NORMAL.value == "normal"
    assert CircuitLevel.WARN.value == "warn"
    assert CircuitLevel.DEGRADED.value == "degraded"
    assert CircuitLevel.HALT.value == "halt"


def test_circuit_breaker_state_properties():
    """Test CircuitBreakerState backward compatibility and new properties."""
    from src.risk.circuit_breaker import CircuitBreakerState

    # Test NORMAL level
    state_normal = CircuitBreakerState(level=CircuitLevel.NORMAL)
    assert not state_normal.is_triggered  # backward compat
    assert state_normal.allows_new_positions
    assert state_normal.size_multiplier == 1.0

    # Test WARN level
    state_warn = CircuitBreakerState(level=CircuitLevel.WARN)
    assert not state_warn.is_triggered  # WARN doesn't trigger backward compat
    assert state_warn.allows_new_positions
    assert state_warn.size_multiplier == 1.0

    # Test DEGRADED level
    state_degraded = CircuitBreakerState(level=CircuitLevel.DEGRADED)
    assert state_degraded.is_triggered  # backward compat
    assert not state_degraded.allows_new_positions
    assert state_degraded.size_multiplier == 0.5

    # Test HALT level
    state_halt = CircuitBreakerState(level=CircuitLevel.HALT)
    assert state_halt.is_triggered  # backward compat
    assert not state_halt.allows_new_positions
    assert state_halt.size_multiplier == 0.0


def test_parse_triggers_with_levels():
    """Test parsing triggers with level configuration."""
    config = {
        "triggers": [
            {
                "type": "drawdown",
                "threshold_pct": 3.0,
                "level": "warn",
                "cooldown_minutes": 30,
            },
            {
                "type": "drawdown",
                "threshold_pct": 5.0,
                "level": "degraded",
                "cooldown_minutes": 60,
            },
            {
                "type": "drawdown",
                "threshold_pct": 8.0,
                "level": "halt",
                "cooldown_minutes": 120,
            },
            {
                "type": "drawdown",
                "threshold_pct": 10.0,
                # no level specified - should default to HALT
            },
        ]
    }

    event_bus = EventBus()
    cb = CircuitBreaker(event_bus, config)

    assert len(cb._triggers) == 4
    assert cb._triggers[0].level == CircuitLevel.WARN
    assert cb._triggers[1].level == CircuitLevel.DEGRADED
    assert cb._triggers[2].level == CircuitLevel.HALT
    assert cb._triggers[3].level == CircuitLevel.HALT  # default


@pytest.mark.parametrize(
    "drawdown_pct,expected_level,expected_triggered",
    [
        (2.5, CircuitLevel.NORMAL, False),  # below all thresholds
        (3.5, CircuitLevel.WARN, False),  # WARN level - not "triggered" in backward compat
        (5.5, CircuitLevel.DEGRADED, True),  # DEGRADED level - "triggered"
        (8.5, CircuitLevel.HALT, True),  # HALT level - "triggered"
    ],
)
def test_drawdown_level_transitions(drawdown_pct, expected_level, expected_triggered):
    """Test that drawdown triggers set correct circuit levels."""
    config = {
        "triggers": [
            {"type": "drawdown", "threshold_pct": 3.0, "level": "warn"},
            {"type": "drawdown", "threshold_pct": 5.0, "level": "degraded"},
            {"type": "drawdown", "threshold_pct": 8.0, "level": "halt"},
        ]
    }

    event_bus = EventBus()
    cb = CircuitBreaker(event_bus, config)

    # Trigger based on drawdown
    cb.check_drawdown(drawdown_pct)

    assert cb._state.level == expected_level
    assert cb._state.is_triggered == expected_triggered
    assert cb.is_active == expected_triggered


def test_size_multiplier_per_level():
    """Test size_multiplier returns correct values for each level."""
    config = {
        "triggers": [
            {"type": "drawdown", "threshold_pct": 3.0, "level": "warn"},
            {"type": "drawdown", "threshold_pct": 5.0, "level": "degraded"},
            {"type": "drawdown", "threshold_pct": 8.0, "level": "halt"},
        ]
    }

    event_bus = EventBus()
    cb = CircuitBreaker(event_bus, config)

    # Test WARN level (3% drawdown)
    cb.check_drawdown(3.5)
    assert cb.state.size_multiplier == 1.0

    # Reset and test DEGRADED level (5% drawdown)
    cb._reset()
    cb.check_drawdown(5.5)
    assert cb.state.size_multiplier == 0.5

    # Reset and test HALT level (8% drawdown)
    cb._reset()
    cb.check_drawdown(8.5)
    assert cb.state.size_multiplier == 0.0


def test_redis_serialization_with_levels():
    """Test Redis round-trip preserves circuit level correctly."""
    # Mock Redis client
    redis_client = MagicMock()
    redis_client.is_available = True

    config = {
        "triggers": [
            {"type": "drawdown", "threshold_pct": 5.0, "level": "degraded"},
        ]
    }

    event_bus = EventBus()
    cb = CircuitBreaker(event_bus, config, redis_client=redis_client)

    # Trigger DEGRADED level
    cb.check_drawdown(5.5)

    # Verify Redis hset was called with level
    redis_client.hset.assert_called_once()
    call_args = redis_client.hset.call_args[0]
    redis_data = call_args[1]

    assert redis_data["level"] == "degraded"
    assert redis_data["is_triggered"] == "1"  # backward compat


def test_restore_from_redis_with_level():
    """Test restoring circuit breaker state from Redis includes level."""
    # Mock Redis client with stored state
    redis_client = MagicMock()
    redis_client.is_available = True
    redis_client.hgetall.return_value = {
        "level": "degraded",
        "is_triggered": "1",
        "action": "halt_all",
        "reason": "test reason",
        "triggered_at_ns": str(time.time_ns()),
        "cooldown_until_ns": str(time.time_ns() + 3600 * 1_000_000_000),  # 1 hour future
    }

    config = {"triggers": []}
    event_bus = EventBus()

    # Create new circuit breaker - should restore from Redis
    cb = CircuitBreaker(event_bus, config, redis_client=redis_client)

    assert cb._state.level == CircuitLevel.DEGRADED
    assert cb._state.is_triggered
    assert cb._state.size_multiplier == 0.5


def test_allows_new_positions_property():
    """Test allows_new_positions property works correctly."""
    config = {
        "triggers": [
            {"type": "drawdown", "threshold_pct": 3.0, "level": "warn"},
            {"type": "drawdown", "threshold_pct": 5.0, "level": "degraded"},
        ]
    }

    event_bus = EventBus()
    cb = CircuitBreaker(event_bus, config)

    # NORMAL - allows new positions
    assert cb.state.allows_new_positions

    # WARN - still allows new positions
    cb.check_drawdown(3.5)
    assert cb.state.allows_new_positions

    # DEGRADED - blocks new positions
    cb._reset()
    cb.check_drawdown(5.5)
    assert not cb.state.allows_new_positions


def test_backward_compatibility():
    """Test that existing code using is_triggered still works."""
    config = {
        "triggers": [
            {"type": "drawdown", "threshold_pct": 3.0, "level": "warn"},
            {"type": "drawdown", "threshold_pct": 5.0, "level": "degraded"},
        ]
    }

    event_bus = EventBus()
    cb = CircuitBreaker(event_bus, config)

    # WARN level should NOT be "triggered" for backward compatibility
    cb.check_drawdown(3.5)
    assert not cb._state.is_triggered
    assert not cb.is_active

    # DEGRADED level SHOULD be "triggered" for backward compatibility
    cb._reset()
    cb.check_drawdown(5.5)
    assert cb._state.is_triggered
    assert cb.is_active
