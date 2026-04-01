"""健康检查 HTTP 服务.

提供 /health 和 /ready 端点.
"""

from __future__ import annotations

import json
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any

import structlog

logger = structlog.get_logger(__name__)


class HealthStatus:
    """健康状态聚合器."""

    def __init__(self) -> None:
        """Initialize the health status."""
        self._checks: dict[str, bool] = {}
        self._last_heartbeat_ns: int = 0

    def set_check(self, name: str, healthy: bool) -> None:
        """Run set check.

        Args:
            name: Name.
            healthy: Healthy.
        """
        self._checks[name] = healthy

    def heartbeat(self) -> None:
        """Run heartbeat."""
        self._last_heartbeat_ns = time.time_ns()

    @property
    def is_healthy(self) -> bool:
        """Return whether healthy.

        Returns:
            bool: Whether the condition is met.
        """
        return all(self._checks.values()) if self._checks else True

    @property
    def is_ready(self) -> bool:
        """Return whether ready.

        Returns:
            bool: Whether the condition is met.
        """
        return self.is_healthy and len(self._checks) > 0

    def to_dict(self) -> dict[str, Any]:
        """Convert the object to dict.

        Returns:
            dict[str, Any]: Dictionary representation of the result.
        """
        return {
            "healthy": self.is_healthy,
            "ready": self.is_ready,
            "checks": self._checks,
            "last_heartbeat": self._last_heartbeat_ns,
        }


# 全局实例
_health_status = HealthStatus()


def get_health_status() -> HealthStatus:
    """Return health status.

    Returns:
        HealthStatus: Result of get health status.
    """
    return _health_status


class _HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        if self.path == "/health":
            status = get_health_status()
            code = 200 if status.is_healthy else 503
            self._respond(code, status.to_dict())
        elif self.path == "/ready":
            status = get_health_status()
            code = 200 if status.is_ready else 503
            self._respond(code, {"ready": status.is_ready})
        else:
            self._respond(404, {"error": "not found"})

    def _respond(self, code: int, body: dict[str, Any]) -> None:
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps(body).encode())

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
        pass  # 静默 HTTP 日志


class _ReusableHTTPServer(HTTPServer):
    allow_reuse_address = True


class HealthServer:
    """健康检查 HTTP 服务."""

    def __init__(self, port: int = 8080) -> None:
        """Initialize the health server.

        Args:
            port: Port number for the server.
        """
        self._port = port
        self._server: HTTPServer | None = None
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        """Run start."""
        if self._thread is not None and self._thread.is_alive():
            return

        def _run() -> None:
            server = _ReusableHTTPServer(("0.0.0.0", self._port), _HealthHandler)
            self._server = server
            logger.info("health_server_started", port=self._port)
            server.serve_forever()

        self._thread = threading.Thread(target=_run, daemon=True)
        self._thread.start()

    def stop(self, timeout: float = 5.0) -> None:
        """Run stop.

        Args:
            timeout: Timeout in seconds.
        """
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
            self._server = None
        if self._thread is not None:
            self._thread.join(timeout=timeout)
            self._thread = None
        logger.info("health_server_stopped", port=self._port)
