"""日志集成测试.

验证 setup_logging 能正确初始化 structlog 和 Python stdlib logging。
NautilusTrader use_pyo3 桥接由 BinanceAdapter._build_node_config() 负责,
无需在此测试 NT init_logging 集成。
"""

from __future__ import annotations

import logging
import threading

import structlog

from src.core.config import LoggingConfig
from src.core.logging import get_logger, setup_logging


class TestSetupLogging:
    """测试 setup_logging 基础功能."""

    def setup_method(self) -> None:
        """每个测试前重置全局状态."""
        import src.core.logging as log_module

        log_module._INITIALIZED = False

    def test_setup_logging_defaults(self) -> None:
        """默认参数应正常初始化 structlog 和 stdlib logging."""
        setup_logging()
        root = logging.getLogger()
        assert root.level == logging.INFO
        assert len(root.handlers) == 1

    def test_setup_logging_uses_nautilus_cfg_level(self) -> None:
        """nautilus_cfg 参数应覆盖 level."""
        cfg = LoggingConfig(level="DEBUG", format="console", console=True)
        setup_logging(nautilus_cfg=cfg)
        root = logging.getLogger()
        assert root.level == logging.DEBUG

    def test_setup_logging_json_format(self) -> None:
        """Json format 应触发 JSONRenderer 并正常工作."""
        cfg = LoggingConfig(level="INFO", format="json")
        setup_logging(nautilus_cfg=cfg)
        bound = structlog.get_logger("test")
        assert bound is not None

    def test_setup_logging_console_format(self) -> None:
        """Console format 应触发 ConsoleRenderer."""
        cfg = LoggingConfig(level="INFO", format="console")
        setup_logging(nautilus_cfg=cfg)
        bound = structlog.get_logger("test")
        assert bound is not None

    def test_setup_logging_returns_none(self) -> None:
        """Setup_logging 应返回 None（不再返回 log_guard）."""
        result = setup_logging()
        assert result is None

    def test_setup_logging_idempotent_structlog(self) -> None:
        """重复调用 setup_logging 不应崩溃（幂等）."""
        setup_logging()
        setup_logging()  # 第二次调用不崩溃

    def test_setup_logging_idempotent_updates_level(self) -> None:
        """重复调用时，stdlib logging 级别应被更新（允许动态调整）."""
        setup_logging(level="INFO")
        setup_logging(level="WARNING")
        root = logging.getLogger()
        # 第二次调用更新了级别
        assert root.level == logging.WARNING

    def test_setup_logging_configures_stdlib_level(self) -> None:
        """setup_logging 应正确设置标准库 logging 级别."""
        cfg = LoggingConfig(level="WARNING")
        setup_logging(nautilus_cfg=cfg)
        root = logging.getLogger()
        assert root.level == logging.WARNING

    def test_setup_logging_thread_safe(self) -> None:
        """并发调用 setup_logging 不应导致竞态或异常."""
        errors: list[Exception] = []

        def call_setup() -> None:
            try:
                setup_logging(level="INFO")
            except Exception as exc:  # pragma: no cover
                errors.append(exc)

        threads = [threading.Thread(target=call_setup) for _ in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert not errors, f"Thread errors: {errors}"

    def test_setup_logging_no_console_uses_null_handler(self) -> None:
        """console=False 应使用 NullHandler，不向 stdout 写入."""
        setup_logging(console=False)
        root = logging.getLogger()
        assert any(isinstance(h, logging.NullHandler) for h in root.handlers)


class TestGetLogger:
    """测试 get_logger 工厂函数."""

    def test_get_logger_returns_bound_logger(self) -> None:
        """get_logger 应返回 structlog BoundLogger 实例."""
        setup_logging()
        log = get_logger("test.module")
        assert log is not None

    def test_get_logger_with_name(self) -> None:
        """get_logger 传入 name 应正常工作."""
        log = get_logger(__name__)
        assert log is not None

    def test_get_logger_without_name(self) -> None:
        """get_logger 不传 name 也应正常工作（向后兼容）."""
        log = get_logger()
        assert log is not None


class TestLoggingConfig:
    """测试 LoggingConfig Pydantic 模型."""

    def test_default_values(self) -> None:
        """默认值应正确."""
        cfg = LoggingConfig()
        assert cfg.level == "INFO"
        assert cfg.format == "json"
        assert cfg.console is True
        assert cfg.bypass_nt_logging is False
        assert cfg.log_directory is None

    def test_custom_values(self) -> None:
        """自定义值应被接受."""
        cfg = LoggingConfig(
            level="DEBUG",
            format="console",
            log_directory="/tmp/logs",
            log_file_name="trading.log",
            log_colors=False,
            log_component_levels={"DataEngine": "DEBUG"},
            bypass_nt_logging=True,
        )
        assert cfg.level == "DEBUG"
        assert cfg.format == "console"
        assert cfg.log_directory == "/tmp/logs"
        assert cfg.log_component_levels == {"DataEngine": "DEBUG"}
        assert cfg.bypass_nt_logging is True

    def test_level_file_optional(self) -> None:
        """level_file 应为可选字段，默认 None."""
        cfg = LoggingConfig()
        assert cfg.level_file is None

    def test_log_component_levels_default_empty(self) -> None:
        """log_component_levels 默认应为空字典."""
        cfg = LoggingConfig()
        assert cfg.log_component_levels == {}


class TestNautilusAdapterLoggingConfig:
    """测试 BinanceAdapterConfig 中的日志配置注入."""

    def test_binance_adapter_config_has_log_level_field(self) -> None:
        """BinanceAdapterConfig 应有 log_level 字段."""
        from src.exchange.binance_adapter import BinanceAdapterConfig

        cfg = BinanceAdapterConfig()
        assert hasattr(cfg, "log_level")
        assert cfg.log_level == "INFO"

    def test_binance_adapter_config_log_level_override(self) -> None:
        """BinanceAdapterConfig.log_level 应接受自定义值."""
        from src.exchange.binance_adapter import BinanceAdapterConfig

        cfg = BinanceAdapterConfig(log_level="DEBUG")
        assert cfg.log_level == "DEBUG"

    def test_build_node_config_uses_use_pyo3(self) -> None:
        """_build_node_config 应在 LoggingConfig 中设置 use_pyo3=True."""
        from unittest.mock import MagicMock, patch

        from nautilus_trader.config import LoggingConfig as NTLoggingConfig

        from src.exchange.binance_adapter import BinanceAdapter, BinanceAdapterConfig

        cfg = BinanceAdapterConfig(log_level="DEBUG")
        adapter = BinanceAdapter(cfg)

        # Mock TradingNode to capture config without connecting
        with patch("src.exchange.binance_adapter.TradingNode") as mock_node_cls:
            mock_node_cls.return_value = MagicMock()
            mock_node_cls.return_value.build = MagicMock()
            node_config = adapter._build_node_config()

        assert isinstance(node_config.logging, NTLoggingConfig)
        assert node_config.logging.use_pyo3 is True
        assert node_config.logging.log_level == "DEBUG"
