"""Prometheus 指标暴露服务."""

from __future__ import annotations

from threading import Thread
from typing import Any

import structlog
from prometheus_client import start_http_server

logger = structlog.get_logger(__name__)


class PrometheusServer:
    """Prometheus 指标 HTTP 服务."""

    def __init__(self, port: int = 9090) -> None:
        """Initialize the prometheus server.

        Args:
            port: Port number for the server.
        """
        self._port = port
        self._started = False
        self._server: Any = None
        self._thread: Thread | None = None

    def start(self) -> None:
        """启动 Prometheus HTTP 服务 (后台线程)."""
        if self._started:
            return

        self._server, self._thread = start_http_server(self._port)
        self._started = True
        logger.info("prometheus_server_started", port=self._port)

    def stop(self, timeout: float = 5.0) -> None:
        """停止 Prometheus HTTP 服务."""
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
            self._server = None
        if self._thread is not None:
            self._thread.join(timeout=timeout)
            self._thread = None
        self._started = False
        logger.info("prometheus_server_stopped", port=self._port)

    @property
    def is_running(self) -> bool:
        """Return whether running.

        Returns:
            bool: Whether the condition is met.
        """
        return self._started
