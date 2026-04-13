"""看门狗数据模型与协议.

定义 Watchdog 依赖的 Protocol、数据类，供 Watchdog 及外部模块使用。
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Protocol


class Watchable(Protocol):
    """被看门狗监控的服务协议.

    任何实现了 ``is_running`` 和 ``last_heartbeat_ns`` 属性的对象
    都可以注册到 Watchdog。
    """

    @property
    def is_running(self) -> bool:
        """返回服务是否正在运行."""
        ...

    @property
    def last_heartbeat_ns(self) -> int:
        """返回服务最近一次心跳时间戳（纳秒）."""
        ...


@dataclass
class WatchEntry:
    """单个被监控服务的记录.

    Attributes:
        name: 服务名称（唯一标识）。
        target: 被监控的服务对象（实现 Watchable 协议）。
        max_heartbeat_gap_sec: 允许的最大心跳间隔（秒），超过则认为异常。
        on_failure: 自定义失败回调，None 时仅告警。
        fail_count: 连续失败次数，用于抑制重复告警。

    """

    name: str
    target: Watchable
    max_heartbeat_gap_sec: float = 60.0
    on_failure: Callable[[], None] | None = None
    fail_count: int = field(default=0, init=False)


@dataclass
class WatchCheckResult:
    """单次全量检查结果.

    Attributes:
        timestamp_ns: 检查时间戳。
        healthy_count: 健康服务数量。
        unhealthy_names: 异常服务名称列表。
        system_ok: 系统资源是否正常。

    """

    timestamp_ns: int = field(default_factory=time.time_ns)
    healthy_count: int = 0
    unhealthy_names: list[str] = field(default_factory=list)
    system_ok: bool = True

    @property
    def all_ok(self) -> bool:
        """返回所有服务和系统资源是否均正常.

        Returns:
            True 表示无任何异常。

        """
        return len(self.unhealthy_names) == 0 and self.system_ok
