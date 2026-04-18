"""实盘守护模块 (live).

包含实盘运行所需的所有后台守护服务：

- :class:`~src.live.supervisor.LiveSupervisor`：主管，协调所有子服务生命周期
- :class:`~src.live.account_sync.AccountSync`：账户状态同步，定期对账
- :class:`~src.live.watchdog.Watchdog`：看门狗，监控子服务存活
- :class:`~src.live.health.LiveHealthProbe`：健康探针，采集并发布健康状态

快速启动::

    from src.live import LiveSupervisor
    from src.app.bootstrap import bootstrap

    container = bootstrap(env="prod")
    supervisor = LiveSupervisor(container)
    supervisor.start()
    supervisor.join()
"""

from src.live.account_sync import AccountBalance, AccountSync, PositionSnapshot, SyncResult
from src.live.health import HealthStatus, LiveHealthProbe
from src.live.supervisor import LiveSupervisor, SupervisorState
from src.live.watchdog import WatchCheckResult, Watchdog, WatchEntry

__all__ = [
    # 督导器
    "LiveSupervisor",
    "SupervisorState",
    # 账户同步
    "AccountSync",
    "AccountBalance",
    "PositionSnapshot",
    "SyncResult",
    # 看门狗
    "Watchdog",
    "WatchEntry",
    "WatchCheckResult",
    # 健康检查
    "LiveHealthProbe",
    "HealthStatus",
]
