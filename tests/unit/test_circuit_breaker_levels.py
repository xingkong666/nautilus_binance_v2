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

    # 测试 NORMAL 级别
    state_normal = CircuitBreakerState(level=CircuitLevel.NORMAL)
    assert not state_normal.is_triggered  # 向后兼容
    assert state_normal.allows_new_positions
    assert state_normal.size_multiplier == 1.0

    # 测试 WARN 级别
    state_warn = CircuitBreakerState(level=CircuitLevel.WARN)
    assert not state_warn.is_triggered  # 警告不会触发向后兼容
    assert state_warn.allows_new_positions
    assert state_warn.size_multiplier == 1.0

    # 测试 DEGRADED 级别
    state_degraded = CircuitBreakerState(level=CircuitLevel.DEGRADED)
    assert state_degraded.is_triggered  # 向后兼容
    assert not state_degraded.allows_new_positions
    assert state_degraded.size_multiplier == 0.5

    # 测试 HALT 级别
    state_halt = CircuitBreakerState(level=CircuitLevel.HALT)
    assert state_halt.is_triggered  # 向后兼容
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
                # 未指定级别 - 应为default至停止
            },
        ]
    }

    event_bus = EventBus()
    cb = CircuitBreaker(event_bus, config)

    assert len(cb._triggers) == 4
    assert cb._triggers[0].level == CircuitLevel.WARN
    assert cb._triggers[1].level == CircuitLevel.DEGRADED
    assert cb._triggers[2].level == CircuitLevel.HALT
    assert cb._triggers[3].level == CircuitLevel.HALT  # 默认值


@pytest.mark.parametrize(
    "drawdown_pct,expected_level,expected_triggered",
    [
        (2.5, CircuitLevel.NORMAL, False),  # 低于所有阈值
        (3.5, CircuitLevel.WARN, False),  # 警告级别 - 在向后兼容中未“触发”
        (5.5, CircuitLevel.DEGRADED, True),  # 降级级别 - “已触发”
        (8.5, CircuitLevel.HALT, True),  # 停止级别 - “已触发”
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

    # 基于回撤触发
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

    # 测试 WARN 级别 (3% drawdown)
    cb.check_drawdown(3.5)
    assert cb.state.size_multiplier == 1.0

    # 重置并测试DEGRADED水平（5%回撤）
    cb._reset()
    cb.check_drawdown(5.5)
    assert cb.state.size_multiplier == 0.5

    # 重置并测试HALT水平（8%回撤）
    cb._reset()
    cb.check_drawdown(8.5)
    assert cb.state.size_multiplier == 0.0


def test_redis_serialization_with_levels():
    """Test Redis round-trip preserves circuit level correctly."""
    # 模拟Redis客户端
    redis_client = MagicMock()
    redis_client.is_available = True

    config = {
        "triggers": [
            {"type": "drawdown", "threshold_pct": 5.0, "level": "degraded"},
        ]
    }

    event_bus = EventBus()
    cb = CircuitBreaker(event_bus, config, redis_client=redis_client)

    # 触发DEGRADED级别
    cb.check_drawdown(5.5)

    # 验证 Redis 设定值 是否已使用 级别 调用
    redis_client.hset.assert_called_once()
    call_args = redis_client.hset.call_args[0]
    redis_data = call_args[1]

    assert redis_data["level"] == "degraded"
    assert redis_data["is_triggered"] == "1"  # 向后兼容


def test_restore_from_redis_with_level():
    """Test restoring circuit breaker state from Redis includes level."""
    # 具有存储状态的模拟Redis客户端
    redis_client = MagicMock()
    redis_client.is_available = True
    redis_client.hgetall.return_value = {
        "level": "degraded",
        "is_triggered": "1",
        "action": "halt_all",
        "reason": "test reason",
        "triggered_at_ns": str(time.time_ns()),
        "cooldown_until_ns": str(time.time_ns() + 3600 * 1_000_000_000),  # 1小时未来
    }

    config = {"triggers": []}
    event_bus = EventBus()

    # 创建新的断路器 - 应从Redis恢复
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

    # 普通的 -允许新位置
    assert cb.state.allows_new_positions

    # 警告 -仍然允许新职位
    cb.check_drawdown(3.5)
    assert cb.state.allows_new_positions

    # 降级-阻止新位置
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

    # 警告级别应“触发”不是以实现向后兼容性
    cb.check_drawdown(3.5)
    assert not cb._state.is_triggered
    assert not cb.is_active

    # DEGRADED级别SHOULD 被“触发”以实现向后兼容性
    cb._reset()
    cb.check_drawdown(5.5)
    assert cb._state.is_triggered
    assert cb.is_active
