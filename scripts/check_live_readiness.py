#!/usr/bin/env python3
"""实盘预检脚本."""
# ruff: noqa: E402

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.app.bootstrap import _build_live_strategy, bootstrap_app
from src.live.readiness import (
    ReadinessCheck,
    credential_checks,
    resolve_live_symbol,
    resolve_strategy_config_path,
)


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="检查 live 路径上线前准备状态")
    parser.add_argument("--env", default=None, help="运行环境（dev/stage/prod）")
    parser.add_argument("--log-level", default="WARNING", help="日志级别")
    parser.add_argument("--strategy-config", default="", help="覆盖 live.strategy_config")
    parser.add_argument("--symbol", default="", help="覆盖 live.symbol")
    parser.add_argument(
        "--check-account-snapshot",
        action="store_true",
        help="额外检查账户快照查询（需要真实网络和有效 Binance 凭证）",
    )
    return parser.parse_args()


def _print_check(check: ReadinessCheck) -> None:
    status = "PASS" if check.passed else "FAIL"
    print(f"[{status}] {check.name}: {check.detail}")


def main() -> int:
    args = _parse_args()
    checks: list[ReadinessCheck] = []
    ctx = None
    adapter = None

    try:
        ctx = bootstrap_app(env=args.env, log_level=args.log_level)
        checks.append(ReadinessCheck("config_loaded", True, f"env={ctx.config.env}"))
        checks.extend(credential_checks(ctx.config))

        strategy_config_path = resolve_strategy_config_path(
            ctx.config,
            override=args.strategy_config,
            cwd=ROOT,
        )
        checks.append(
            ReadinessCheck(
                "strategy_config_exists",
                strategy_config_path.exists(),
                str(strategy_config_path),
            )
        )
        if not strategy_config_path.exists():
            raise FileNotFoundError(f"Strategy config not found: {strategy_config_path}")

        live_symbol = resolve_live_symbol(ctx.config, override=args.symbol)
        strategy = _build_live_strategy(
            strategy_config_path=strategy_config_path,
            container=ctx.container,
            symbol=live_symbol or None,
        )
        checks.append(
            ReadinessCheck(
                "strategy_loaded",
                True,
                f"{strategy.__class__.__name__} @ {strategy.config.instrument_id}",
            )
        )

        ctx.container.order_router.bind_strategy(strategy)
        adapter = ctx.factory.create_binance_adapter(symbols=[str(strategy.config.instrument_id)])
        adapter.register_strategy(strategy)
        adapter.build_node()
        checks.append(
            ReadinessCheck(
                "adapter_node_built",
                True,
                str(strategy.config.instrument_id),
            )
        )

        if args.check_account_snapshot:
            balances, positions = adapter.fetch_account_snapshot()
            checks.append(
                ReadinessCheck(
                    "account_snapshot_query",
                    True,
                    f"balances={len(balances)} positions={len(positions)}",
                )
            )
    except Exception as exc:
        checks.append(ReadinessCheck("exception", False, str(exc)))
    finally:
        if adapter is not None:
            adapter.dispose()
        if ctx is not None:
            ctx.container.teardown()

    for check in checks:
        _print_check(check)

    return 0 if all(check.passed for check in checks) else 1


if __name__ == "__main__":
    raise SystemExit(main())
