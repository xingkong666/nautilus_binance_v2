"""Prometheus 指标暴露服务."""

from __future__ import annotations

import threading

from prometheus_client import start_http_server

import structlog

logger = structlog.get_logger()


class PrometheusServer:
    """Prometheus 指标 HTTP 服务."""

    def __init__(self, port: int = 9090) -> None:
        self._port = port
        self._started = False

    def start(self) -> None:
        """启动 Prometheus HTTP 服务 (后台线程)."""
        if self._started:
            return

        def _run() -> None:
            start_http_server(self._port)
            logger.info("prometheus_server_started", port=self._port)

        thread = threading.Thread(target=_run, daemon=True)
        thread.start()
        self._started = True

    @property
    def is_running(self) -> bool:
        return self._started
