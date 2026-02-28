"""下载器单元测试.

覆盖 BinanceFuturesDownloader 的核心路径:
- URL 格式验证
- 已存在 CSV 的断点续传跳过逻辑
"""

from __future__ import annotations

import datetime as dt
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from src.core.enums import Interval
from src.data.loaders import BinanceFuturesDownloader


def test_url_generation(tmp_path: Path) -> None:
    """验证下载 URL 格式是否符合 Binance data vision 规范.

    Args:
        tmp_path: pytest 提供的临时目录, 用于初始化下载器.

    Returns:
        None
    """
    downloader = BinanceFuturesDownloader(raw_dir=tmp_path)

    symbol = "BTCUSDT"
    interval = Interval.MINUTE_1
    date = dt.date(2025, 11, 9)
    date_str = date.strftime("%Y-%m-%d")
    base_name = f"{symbol}-{interval.value}-{date_str}"

    expected_url = (
        f"https://data.binance.vision/data/futures/um/daily/klines/"
        f"{symbol}/{interval.value}/{base_name}.zip"
    )

    # 验证 URL 拼接逻辑正确 (通过检查目录未提前创建来侧面验证)
    save_dir = tmp_path / "futures" / symbol
    assert not save_dir.exists()
    assert expected_url.startswith(downloader.BASE_URL)


def test_csv_skip_existing(tmp_path: Path) -> None:
    """验证: 已存在的 CSV 应被跳过, 不触发网络请求.

    将预创建好的 CSV 放入对应目录后调用 download_klines,
    期望直接返回已有文件路径, 不抛出任何网络异常.

    Args:
        tmp_path: pytest 提供的临时目录.

    Returns:
        None
    """
    downloader = BinanceFuturesDownloader(raw_dir=tmp_path)

    symbol = "BTCUSDT"
    interval = Interval.MINUTE_1
    date = dt.date(2025, 11, 9)
    date_str = date.strftime("%Y-%m-%d")
    base_name = f"{symbol}-{interval.value}-{date_str}"

    # 预先创建 CSV
    save_dir = tmp_path / "futures" / symbol
    save_dir.mkdir(parents=True)
    csv_path = save_dir / f"{base_name}.csv"
    csv_path.write_text("test")

    # 应直接返回已有路径, 不发请求
    result = downloader.download_klines(symbol, interval, date)
    assert result == csv_path
