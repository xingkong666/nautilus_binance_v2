#!/usr/bin/env python3
"""Binance Futures 历史 K 线下载脚本 (V2 Production).

支持按 symbol、时间范围、K 线周期批量下载 Binance U 本位合约历史数据，
并可选将数据写入 Nautilus Catalog 以供回测/实盘使用。

Usage:
    # 使用默认参数下载（需在 pyproject / 环境中配置 DEFAULT_INSTRUMENTS）
    python scripts/download_data.py

    # 指定 symbol、周期与时间范围
    python scripts/download_data.py \
        --symbols BTCUSDT ETHUSDT \
        --interval 1m \
        --start 2022-01-01 \
        --end 2026-02-19

    # 仅下载 CSV，不写入 Catalog
    python scripts/download_data.py --symbols BTCUSDT --start 2024-01-01 --end 2024-01-31 --download-only

    # 断点续传：从本地最新日期自动续下到昨天
    python scripts/download_data.py --symbols BTCUSDT --auto-latest

Features:
    - 参数严格校验（日期合法性、必填项检查）
    - 自动断点续传（--auto-latest，从本地最后一天 +1 开始）
    - 失败自动重试（最多 MAX_RETRY 次，间隔 2s）
    - tqdm 进度条（由 BinanceFuturesDownloader 内部驱动）
    - 单 symbol 失败不中断整体流程，打印错误后继续
    - 支持仅下载模式（--download-only）或下载 + 写入 Catalog 两种模式
"""
# ruff: noqa: E402

from __future__ import annotations

import argparse
import datetime as dt
import sys
import time
from pathlib import Path

# ------------------------------------------------------------------------------
# Path bootstrap
# ------------------------------------------------------------------------------

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# ------------------------------------------------------------------------------
# Imports
# ------------------------------------------------------------------------------

from nautilus_trader.test_kit.providers import TestInstrumentProvider

from src.core.config import load_app_config, load_yaml
from src.core.enums import DEFAULT_INSTRUMENTS, Interval
from src.core.logging import setup_logging
from src.data.loaders import BinanceFuturesDownloader, DataPipeline

# ------------------------------------------------------------------------------
# Globals
# ------------------------------------------------------------------------------

#: symbol -> TestInstrumentProvider 方法的映射，由 load_instruments_config() 填充。
INSTRUMENT_MAP: dict = {}

#: 单次下载任务最大重试次数。
MAX_RETRY = 3


# ------------------------------------------------------------------------------
# CLI
# ------------------------------------------------------------------------------


def parse_args() -> argparse.Namespace:
    """解析命令行参数。.

    Returns:
        argparse.Namespace: 包含以下字段：
            - symbols (list[str]): 要下载的交易对列表，默认为 DEFAULT_INSTRUMENTS。
            - interval (str): K 线周期，如 "1m"、"5m"、"1h"，需在 Interval 枚举中定义。
            - start (str | None): 开始日期，格式 YYYY-MM-DD；与 --auto-latest 互斥时可为 None。
            - end (str | None): 结束日期，格式 YYYY-MM-DD；--auto-latest 时自动设为昨天。
            - download_only (bool): 若为 True，仅下载 CSV 文件，不写入 Nautilus Catalog。
            - auto_latest (bool): 若为 True，从本地最新日期 +1 续传到昨天。
            - env (str | None): 配置环境标识，传给 load_app_config()；None 表示使用默认环境。

    """
    parser = argparse.ArgumentParser(description="Binance Futures 历史数据下载")

    parser.add_argument(
        "--symbols",
        nargs="+",
        default=DEFAULT_INSTRUMENTS,
        help="交易对列表，如 BTCUSDT ETHUSDT（默认：DEFAULT_INSTRUMENTS）",
    )

    parser.add_argument(
        "--interval",
        default="1m",
        choices=[i.value for i in Interval],
        help="K 线周期（默认：1m）",
    )

    parser.add_argument("--start", help="开始日期 YYYY-MM-DD")
    parser.add_argument("--end", help="结束日期 YYYY-MM-DD")

    parser.add_argument(
        "--download-only",
        action="store_true",
        help="仅下载 CSV，不写入 Nautilus Catalog",
    )

    parser.add_argument(
        "--auto-latest",
        action="store_true",
        help="从本地最后一天 +1 自动续传到昨天",
    )

    parser.add_argument(
        "--env",
        default=None,
        help="配置环境标识（默认：None，使用默认环境）",
    )

    return parser.parse_args()


# ------------------------------------------------------------------------------
# Helpers
# ------------------------------------------------------------------------------


def load_instruments_config() -> None:
    """从 configs/instruments.yaml 加载 instrument 配置，填充全局 INSTRUMENT_MAP。.

    读取 instruments.yaml 中每个 symbol 对应的 provider 方法名，并通过
    ``TestInstrumentProvider`` 反射获取对应的工厂函数，存入 ``INSTRUMENT_MAP``。
    若某 symbol 的 provider 方法不存在于 ``TestInstrumentProvider``，则跳过。

    Raises:
        FileNotFoundError: 若 configs/instruments.yaml 不存在。
        yaml.YAMLError: 若 YAML 文件格式有误。

    """
    config_path = ROOT / "configs" / "instruments.yaml"
    cfg = load_yaml(config_path)

    instruments = cfg.get("instruments", {})
    for symbol, info in instruments.items():
        provider_name = info.get("provider")
        provider_fn = getattr(TestInstrumentProvider, provider_name, None)
        if provider_fn:
            INSTRUMENT_MAP[symbol] = provider_fn


def validate_dates(start: dt.date | None, end: dt.date | None) -> None:
    """校验起止日期的逻辑合法性。.

    Args:
        start: 起始日期；为 None 时跳过校验。
        end: 结束日期；为 None 时跳过校验。

    Raises:
        ValueError: 若 start 和 end 均不为 None 且 start > end。

    """
    if start and end and start > end:
        raise ValueError("start 不能大于 end")


def latest_local_date(raw_dir: Path, symbol: str) -> dt.date | None:
    """获取本地已下载数据中指定 symbol 的最新日期。.

    在 ``raw_dir/futures/<symbol>/`` 目录下扫描形如
    ``<symbol>-*-<YYYY-MM-DD>.csv`` 的文件，提取文件名末尾的日期并返回最大值。

    Args:
        raw_dir: 原始数据根目录（对应 config.data.raw_dir）。
        symbol: 交易对名称，如 "BTCUSDT"。

    Returns:
        本地已下载的最新日期（``dt.date``）；若目录不存在或无合法文件则返回 None。

    """
    folder = raw_dir / "futures" / symbol
    if not folder.exists():
        return None

    dates = []
    for p in folder.glob(f"{symbol}-*-*.csv"):
        try:
            date_str = p.stem.split("-")[-1]
            dates.append(dt.date.fromisoformat(date_str))
        except Exception:
            continue

    return max(dates) if dates else None


def retry_download(fn, *args):
    """带重试机制地执行下载函数。.

    最多重试 ``MAX_RETRY`` 次，每次失败后等待 2 秒再重试。
    若最后一次仍失败，则将异常向上抛出。

    Args:
        fn: 可调用的下载函数。
        *args: 传递给 ``fn`` 的位置参数。

    Returns:
        ``fn(*args)`` 的返回值。

    Raises:
        Exception: 重试次数耗尽后，重新抛出最后一次的异常。

    """
    for attempt in range(1, MAX_RETRY + 1):
        try:
            return fn(*args)
        except Exception as e:
            if attempt == MAX_RETRY:
                raise
            print(f"⚠️ 下载失败, 重试 {attempt}/{MAX_RETRY}: {e}")
            time.sleep(2)


# ------------------------------------------------------------------------------
# Main
# ------------------------------------------------------------------------------


def main() -> None:
    """脚本主入口：解析参数、初始化环境，并批量下载各 symbol 的历史数据。.

    执行流程：
        1. 解析 CLI 参数，加载应用配置与 instrument 映射。
        2. 根据 ``--auto-latest`` 自动确定各 symbol 的起止日期。
        3. 调用 ``BinanceFuturesDownloader.download_range()`` 下载 CSV 文件（含重试）。
        4. 若未指定 ``--download-only``，调用 ``DataPipeline.run()`` 写入 Nautilus Catalog。
        5. 单个 symbol 失败时打印错误并继续处理下一个，不中断整体流程。

    Raises:
        ValueError: 若缺少必要的日期参数，或 symbol 未在 instrument 配置中声明。
        SystemExit: argparse 参数错误时由 argparse 内部触发。

    """
    args = parse_args()

    config = load_app_config(env=args.env)
    setup_logging(level="INFO")

    load_instruments_config()

    interval = Interval(args.interval)
    if interval != Interval.MINUTE_1:
        raise ValueError("仅支持下载 1m 原始 K 线数据。请使用 --interval 1m，并在回测时通过聚合生成 15m/更高周期。")

    start = dt.date.fromisoformat(args.start) if args.start else None
    end = dt.date.fromisoformat(args.end) if args.end else None

    raw_dir = config.data.raw_dir
    catalog_dir = config.data.catalog_dir

    # --auto-latest 时将全局 end 设为昨天（各 symbol 的 start 在循环内单独处理）
    if args.auto_latest:
        end = dt.date.today() - dt.timedelta(days=1)

    validate_dates(start, end)

    print("=" * 70)
    print("📥 Binance Futures 历史数据下载 (V2)")
    print("=" * 70)
    print("Symbols :", args.symbols)
    print("Interval:", interval.value)
    print("Mode    :", "Download Only" if args.download_only else "Download + Catalog")
    print("=" * 70)

    downloader = BinanceFuturesDownloader(raw_dir)
    pipeline = None if args.download_only else DataPipeline(raw_dir, catalog_dir)

    total_symbols = len(args.symbols)

    for idx, symbol in enumerate(args.symbols, 1):
        print(f"\n[{idx}/{total_symbols}] ▶ {symbol}")

        try:
            symbol_start = start
            symbol_end = end

            if args.auto_latest:
                latest = latest_local_date(raw_dir, symbol)
                if latest:
                    # 从本地最新日期的下一天开始续传
                    symbol_start = latest + dt.timedelta(days=1)
                elif not start:
                    raise ValueError("首次下载需提供 --start")

            if symbol_start is None or symbol_end is None:
                raise ValueError("必须提供 --start/--end 或使用 --auto-latest")

            days = (symbol_end - symbol_start).days + 1
            print(f"  Range: {symbol_start} → {symbol_end} ({days} days)")

            # 下载 CSV 文件（失败自动重试）
            paths = retry_download(
                downloader.download_range,
                symbol,
                interval,
                symbol_start,
                symbol_end,
            )

            print(f"  ✅ Downloaded {len(paths)} files")

            if not args.download_only:
                provider_fn = INSTRUMENT_MAP.get(symbol)
                if not provider_fn:
                    raise ValueError(f"未配置 instrument provider: {symbol}")

                instrument = provider_fn()

                total = pipeline.run(
                    instrument,
                    symbol,
                    interval,
                    symbol_start,
                    symbol_end,
                )

                print(f"  📦 Catalog written: {total} bars")

        except Exception as e:
            print(f"  ❌ {symbol} 失败: {e}")
            continue

    print("\n🎉 All Done.")


if __name__ == "__main__":
    main()
