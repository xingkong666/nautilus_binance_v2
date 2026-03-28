"""集成测试：Container 组装和服务生命周期.

验证 Container.build() 能正确实例化所有关键服务，
并通过完整的应用配置路径初始化，最终 teardown 不报错。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from src.app.container import Container
from src.core.config import (
    AppConfig,
    DataConfig,
    ExecutionConfig,
    MonitoringConfig,
    RiskConfig,
)

PG_URL = "postgresql://admin:Longmao!666@127.0.0.1:5432/nautilus_trader"


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def make_config(tmp_path: Path) -> AppConfig:
    """构建测试用 AppConfig，使用临时目录避免污染。.

    Args:
        tmp_path: Temporary filesystem path provided by pytest.
    """
    return AppConfig(
        env="dev",
        data=DataConfig(
            catalog_dir=tmp_path / "catalog",
            raw_dir=tmp_path / "raw",
            features_dir=tmp_path / "features",
            database_url=PG_URL,
        ),
        risk=RiskConfig(
            enabled=True,
            pre_trade={
                "max_order_size_usd": 50000,
                "max_position_size_usd": 200000,
                "max_leverage": 10,
                "min_order_interval_ms": 0,
                "max_open_orders": 20,
            },
            circuit_breaker={
                "triggers": [
                    {"type": "daily_loss", "threshold_usd": 5000, "action": "halt_all", "cooldown_minutes": 60},
                ]
            },
            real_time={
                "trailing_drawdown_pct": 3.0,
                "max_drawdown_pct": 5.0,
            },
        ),
        execution=ExecutionConfig(
            rate_limit={"max_rate": 10, "per_seconds": 1},
        ),
        monitoring=MonitoringConfig(enabled=False),
        strategies={},
    )


@pytest.fixture
def tmp_container(tmp_path):
    """返回已 build 的 Container，测试后自动 teardown。.

    Args:
        tmp_path: Temporary filesystem path provided by pytest.
    """
    cfg = make_config(tmp_path)
    c = Container(cfg)
    c.build()
    yield c
    c.teardown()


# ---------------------------------------------------------------------------
# 基础组装测试
# ---------------------------------------------------------------------------


class TestContainerBuild:
    """Test cases for container build."""

    def test_build_succeeds(self, tmp_path):
        """Container.build() 不报错。.

        Args:
            tmp_path: Temporary filesystem path provided by pytest.
        """
        cfg = make_config(tmp_path)
        c = Container(cfg)
        c.build()
        c.teardown()

    def test_double_build_is_idempotent(self, tmp_path):
        """重复调用 build() 不报错，返回同一容器。.

        Args:
            tmp_path: Temporary filesystem path provided by pytest.
        """
        cfg = make_config(tmp_path)
        c = Container(cfg)
        c.build()
        c.build()  # 第二次调用应幂等
        c.teardown()

    def test_access_before_build_raises(self, tmp_path):
        """build() 前访问服务属性应抛出 RuntimeError。.

        Args:
            tmp_path: Temporary filesystem path provided by pytest.
        """
        cfg = make_config(tmp_path)
        c = Container(cfg)
        with pytest.raises(RuntimeError, match="not built"):
            _ = c.event_bus

    def test_all_core_services_initialized(self, tmp_container):
        """所有核心服务属性在 build 后非 None。.

        Args:
            tmp_container: Tmp container.
        """
        c = tmp_container
        assert c.event_bus is not None
        assert c.persistence is not None
        assert c.snapshot_manager is not None
        assert c.rate_limiter is not None
        assert c.position_sizer is not None
        assert c.pre_trade_risk is not None
        assert c.circuit_breaker is not None
        assert c.drawdown_controller is not None
        assert c.fill_handler is not None
        assert c.alert_manager is not None

    def test_no_binance_adapter_in_dev(self, tmp_container):
        """Dev 环境且无 exchange 配置时，binance_adapter 为 None。.

        Args:
            tmp_container: Tmp container.
        """
        assert tmp_container.binance_adapter is None

    def test_no_portfolio_allocator_when_not_configured(self, tmp_container):
        """未配置 portfolio 节时，portfolio_allocator 为 None。.

        Args:
            tmp_container: Tmp container.
        """
        assert tmp_container.portfolio_allocator is None

    def test_health_server_none_when_monitoring_disabled(self, tmp_container):
        """monitoring.enabled=False 时，health_server 为 None。.

        Args:
            tmp_container: Tmp container.
        """
        assert tmp_container.health_server is None
        assert tmp_container.prometheus_server is None


class TestContainerWithPortfolio:
    """Test cases for container with portfolio."""

    def test_portfolio_allocator_initialized(self, tmp_path):
        """配置了 portfolio 节时，portfolio_allocator 正确初始化。.

        Args:
            tmp_path: Temporary filesystem path provided by pytest.
        """
        cfg = make_config(tmp_path)
        cfg.strategies["portfolio"] = {
            "mode": "equal",
            "reserve_pct": 5.0,
            "min_allocation": "100",
            "strategies": [
                {"strategy_id": "ema_cross", "weight": 1.0},
                {"strategy_id": "mean_revert", "weight": 1.0},
            ],
        }
        c = Container(cfg)
        c.build()

        assert c.portfolio_allocator is not None
        c.teardown()

    def test_portfolio_allocator_mode(self, tmp_path):
        """portfolio_allocator 的模式正确加载。.

        Args:
            tmp_path: Temporary filesystem path provided by pytest.
        """
        cfg = make_config(tmp_path)
        cfg.strategies["portfolio"] = {
            "mode": "weight",
            "reserve_pct": 10.0,
            "strategies": [
                {"strategy_id": "s1", "weight": 2.0},
                {"strategy_id": "s2", "weight": 1.0},
            ],
        }
        c = Container(cfg)
        c.build()

        alloc = c.portfolio_allocator
        assert alloc is not None
        assert alloc._mode == "weight"
        c.teardown()


class TestContainerMonitoring:
    """Test cases for container monitoring services."""

    def test_monitoring_enabled_starts_exporters(self, tmp_path):
        """Verify that monitoring enabled starts health and metrics exporters."""
        cfg = make_config(tmp_path)
        cfg.monitoring = MonitoringConfig(enabled=True, prometheus_port=9201)

        c = Container(cfg)
        c.build()

        assert c.health_server is not None
        assert c.prometheus_server is not None
        assert c.prometheus_server.is_running is True
        c.teardown()
