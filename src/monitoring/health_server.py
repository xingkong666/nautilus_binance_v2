"""健康检查 HTTP 服务.

提供 /health 和 /ready 端点.
"""

from __future__ import annotations

import json
import time
import threading
from http.server import HTTPServer, BaseHTTPRequestHandler
from typing import Any

import structlog

logger = structlog.get_logger()


class HealthStatus:
    """健康状态聚合器."""

    def __init__(self) -> None:
        self._checks: dict[str, bool] = {}
        self._last_heartbeat_ns: int = 0

    def set_check(self, name: str, healthy: bool) -> None:
        self._checks[name] = healthy

    def heartbeat(self) -> None:
        self._last_heartbeat_ns = time.time_ns()

    @property
    def is_healthy(self) -> bool:
        return all(self._checks.values()) if self._checks else True

    @property
    def is_ready(self) -> bool:
        return self.is_healthy and len(self._checks) > 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "healthy": self.is_healthy,
            "ready": self.is_ready,
            "checks": self._checks,
            "last_heartbeat": self._last_heartbeat_ns,
        }


# 全局实例
_health_status = HealthStatus()


def get_health_status() -> HealthStatus:
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

    def _respond(self, code: int, body: dict) -> None:
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps(body).encode())

    def log_message(self, format: str, *args: Any) -> None:
        pass  # 静默 HTTP 日志


class HealthServer:
    """健康检查 HTTP 服务."""

    def __init__(self, port: int = 8080) -> None:
        self._port = port

    def start(self) -> None:
        def _run() -> None:
            server = HTTPServer(("0.0.0.0", self._port), _HealthHandler)
            logger.info("health_server_started", port=self._port)
            server.serve_forever()

        thread = threading.Thread(target=_run, daemon=True)
        thread.start()
