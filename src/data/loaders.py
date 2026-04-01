"""数据下载与加载.

从 Binance 下载合约历史 K 线 → 验证 → 写入 Nautilus ParquetDataCatalog.
基于 v1 load_data 模块重构, 新增: 数据验证 / 结构化日志 / 批量下载 / 断点续传.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import hashlib
import json
import zipfile
from contextlib import suppress
from pathlib import Path
from typing import Any

import httpx
import pandas as pd
import structlog
from nautilus_trader.model import BarType
from nautilus_trader.model.instruments import CryptoPerpetual
from nautilus_trader.persistence.catalog import ParquetDataCatalog
from nautilus_trader.persistence.wranglers_v2 import BarDataWranglerV2

from src.core.enums import INTERVAL_TO_NAUTILUS, Interval, TraderType
from src.core.exceptions import DataError
from src.data.validators import validate_data_completeness, validate_kline_dataframe

logger = structlog.get_logger()

# ---------------------------------------------------------------------------
# 下载器
# ---------------------------------------------------------------------------


class BaseBinanceDownloader:
    """Binance K 线下载器基类.

    提供下载、校验、断点续传、元数据记录等公共能力.
    合约/现货子类通过覆盖 TRADER_TYPE / PATH_TEMPLATE 实现差异化.
    """

    BASE_URL = "https://data.binance.vision"
    TRADER_TYPE = TraderType.FUTURES
    PATH_TEMPLATE = "data/futures/um/daily/klines"

    def __init__(self, raw_dir: Path) -> None:
        """初始化下载器.

        Args:
            raw_dir: 原始数据根目录, 下载文件存放在 raw_dir/{trader_type}/{symbol}/ 下.

        """
        self._raw_dir = raw_dir

    @staticmethod
    def _sha256(file_path: Path) -> str:
        """计算文件的 SHA256 哈希值.

        Args:
            file_path: 待计算的文件路径.

        Returns:
            十六进制格式的 SHA256 字符串.

        """
        h = hashlib.sha256()
        with open(file_path, "rb") as f:
            for chunk in iter(lambda: f.read(8192), b""):
                h.update(chunk)
        return h.hexdigest()

    def _download_checksum(self, url: str, timeout: float = 10.0) -> str | None:
        """下载 .CHECKSUM 文件并解析 SHA256 哈希.

        Binance 校验文件格式: ``<sha256>  <filename>``

        Args:
            url: .CHECKSUM 文件的完整 URL.
            timeout: HTTP 请求超时秒数.

        Returns:
            SHA256 哈希字符串; 下载失败或内容为空时返回 None.

        """
        try:
            resp = httpx.get(url, timeout=timeout)
            resp.raise_for_status()
            text = resp.text.strip()
            if not text:
                return None
            return text.split()[0]
        except (httpx.HTTPError, ConnectionError, TimeoutError) as e:
            logger.warning("checksum_download_failed", url=url, error=str(e))
            raise DataError(f"Failed to download checksum from {url}", context={"url": url, "error": str(e)}) from e

    def _validate_existing_csv(self, csv_path: Path) -> bool:
        """断点续传时校验本地 CSV 是否有效.

        空文件视为无效并自动删除.

        Args:
            csv_path: 待校验的 CSV 文件路径.

        Returns:
            True 表示文件存在且非空可直接复用; False 表示需重新下载.

        """
        if not csv_path.exists():
            return False
        if csv_path.stat().st_size <= 0:
            logger.warning("csv_empty_remove", path=str(csv_path))
            csv_path.unlink(missing_ok=True)
            return False
        return True

    def _validate_zip(self, zip_path: Path, checksum_url: str | None = None) -> bool:
        """校验 ZIP 文件完整性.

        按顺序检查: 文件大小 → SHA256 → 能否正常解压.

        Args:
            zip_path: ZIP 文件路径.
            checksum_url: 对应的 .CHECKSUM 文件 URL; 为 None 时跳过哈希校验.

        Returns:
            True 表示 ZIP 有效; False 表示校验失败.

        """
        if not zip_path.exists() or zip_path.stat().st_size <= 0:
            logger.warning("zip_invalid", path=str(zip_path))
            return False

        if checksum_url:
            expected = self._download_checksum(checksum_url)
            if expected:
                actual = self._sha256(zip_path)
                if actual != expected:
                    logger.warning("checksum_mismatch", expected=expected, actual=actual)
                    return False

        try:
            with zipfile.ZipFile(zip_path, "r") as zf:
                bad = zf.testzip()
                if bad is not None:
                    logger.warning("zip_corrupted", file=bad)
                    return False
        except zipfile.BadZipFile:
            logger.warning("bad_zip", path=str(zip_path))
            return False

        return True

    def _write_manifest(self, save_dir: Path, date_str: str, status: str) -> None:
        """写入下载元数据到 manifest.json.

        每次下载/跳过/失败都会更新 save_dir/manifest.json 中对应日期的状态.

        Args:
            save_dir: 数据目录 (raw_dir/{trader_type}/{symbol}/).
            date_str: 日期字符串, 格式 YYYY-MM-DD.
            status: 状态值, 如 "success" / "skipped" / "failed".

        Returns:
            None

        """
        manifest_path = save_dir / "manifest.json"
        data: dict[str, Any] = {
            "updated_at": dt.datetime.now(dt.UTC).isoformat(),
            "files": {},
        }
        if manifest_path.exists():
            with suppress(Exception):
                data = json.loads(manifest_path.read_text())
        data.setdefault("files", {})
        data["files"][date_str] = status
        manifest_path.write_text(json.dumps(data, ensure_ascii=False, indent=2))

    async def _download_async(self, client: httpx.AsyncClient, url: str, timeout: float) -> bytes:
        """异步发起 HTTP GET 请求并返回响应体.

        Args:
            client: 复用的 AsyncClient 实例.
            url: 目标 URL.
            timeout: 请求超时秒数.

        Returns:
            响应体字节内容.

        Raises:
            httpx.HTTPStatusError: HTTP 4xx/5xx 错误.

        """
        resp = await client.get(url, timeout=timeout)
        resp.raise_for_status()
        return resp.content

    async def download_klines_async(
        self,
        client: httpx.AsyncClient,
        semaphore: asyncio.Semaphore,
        symbol: str,
        interval: Interval,
        date: dt.date,
        *,
        timeout: float = 30.0,
        index: int | None = None,
        total: int | None = None,
    ) -> Path:
        """异步下载单日 K 线并解压为 CSV.

        已存在的有效 CSV 直接跳过 (断点续传). 下载完成后校验 ZIP 完整性,
        并更新 manifest.json.

        Args:
            client: 共享的 AsyncClient 实例.
            semaphore: 并发控制信号量.
            symbol: 交易对, 如 "BTCUSDT".
            interval: 时间间隔枚举.
            date: 目标日期.
            timeout: 单次请求超时秒数.
            index: 当前任务序号 (用于进度输出).
            total: 任务总数 (用于进度输出).

        Returns:
            解压后的 CSV 文件路径.

        Raises:
            httpx.HTTPStatusError: HTTP 请求失败.
            httpx.HTTPError: ZIP 校验失败.

        """
        async with semaphore:
            date_str = date.strftime("%Y-%m-%d")
            base_name = f"{symbol}-{interval.value}-{date_str}"
            save_dir = self._raw_dir / self.TRADER_TYPE.value / symbol
            save_dir.mkdir(parents=True, exist_ok=True)

            csv_path = save_dir / f"{base_name}.csv"
            if self._validate_existing_csv(csv_path):
                if index is not None and total is not None:
                    logger.info("progress", msg=f"[{index}/{total}] {symbol} {date_str} ✓")
                self._write_manifest(save_dir, date_str, "skipped")
                return csv_path

            url = f"{self.BASE_URL}/{self.PATH_TEMPLATE}/{symbol}/{interval.value}/{base_name}.zip"
            checksum_url = url + ".CHECKSUM"
            zip_path = save_dir / f"{base_name}.zip"

            logger.info("downloading", symbol=symbol, interval=interval.value, date=date_str, url=url)
            content = await self._download_async(client, url, timeout)
            zip_path.write_bytes(content)

            # 校验 ZIP (FIX-1)
            if not self._validate_zip(zip_path, checksum_url=checksum_url):
                zip_path.unlink(missing_ok=True)
                self._write_manifest(save_dir, date_str, "failed")
                raise httpx.HTTPError("zip validation failed")

            with zipfile.ZipFile(zip_path, "r") as zf:
                zf.extractall(save_dir)
            zip_path.unlink()
            logger.debug("unzipped", csv=str(csv_path))
            if index is not None and total is not None:
                logger.info("progress", msg=f"[{index}/{total}] {symbol} {date_str} ✓")
            self._write_manifest(save_dir, date_str, "success")
            return csv_path

    async def download_range_async(
        self,
        symbol: str,
        interval: Interval,
        start: dt.date,
        end: dt.date,
        *,
        timeout: float = 30.0,
        concurrency: int = 10,
    ) -> list[Path]:
        """异步并发下载日期范围内的 K 线.

        使用 asyncio.Semaphore 控制并发数, 失败的日期记录警告后跳过.

        Args:
            symbol: 交易对, 如 "BTCUSDT".
            interval: 时间间隔枚举.
            start: 开始日期 (含).
            end: 结束日期 (含).
            timeout: 单次请求超时秒数.
            concurrency: 最大并发下载数, 默认 10.

        Returns:
            成功下载的 CSV 文件路径列表.

        """
        total_days = (end - start).days + 1
        semaphore = asyncio.Semaphore(concurrency)
        async with httpx.AsyncClient() as client:
            tasks = []
            for i in range(total_days):
                day = start + dt.timedelta(days=i)
                tasks.append(
                    self.download_klines_async(
                        client,
                        semaphore,
                        symbol,
                        interval,
                        day,
                        timeout=timeout,
                        index=i + 1,
                        total=total_days,
                    )
                )
            results = await asyncio.gather(*tasks, return_exceptions=True)

        paths: list[Path] = []
        for res in results:
            if isinstance(res, BaseException):
                logger.warning("download_error", symbol=symbol, error=str(res))
                continue
            paths.append(res)
        return paths

    # ------ 同步兼容接口 ------

    def download_klines(
        self,
        symbol: str,
        interval: Interval,
        date: dt.date,
        *,
        timeout: float = 30.0,
    ) -> Path:
        """同步下载单日 K 线 (兼容接口).

        内部调用 asyncio.run 执行异步逻辑, 保持向后兼容.

        Args:
            symbol: 交易对, 如 "BTCUSDT".
            interval: 时间间隔枚举.
            date: 目标日期.
            timeout: 请求超时秒数.

        Returns:
            解压后的 CSV 文件路径.

        """
        date_str = date.strftime("%Y-%m-%d")
        base_name = f"{symbol}-{interval.value}-{date_str}"
        save_dir = self._raw_dir / self.TRADER_TYPE.value / symbol
        save_dir.mkdir(parents=True, exist_ok=True)
        csv_path = save_dir / f"{base_name}.csv"

        # 快路径：本地已有有效 CSV 时直接返回，避免创建 HTTP client。
        if self._validate_existing_csv(csv_path):
            self._write_manifest(save_dir, date_str, "skipped")
            return csv_path

        async def _run() -> Path:
            async with httpx.AsyncClient() as client:
                return await self.download_klines_async(
                    client=client,
                    semaphore=asyncio.Semaphore(1),
                    symbol=symbol,
                    interval=interval,
                    date=date,
                    timeout=timeout,
                )

        return asyncio.run(_run())

    def download_range(
        self,
        symbol: str,
        interval: Interval,
        start: dt.date,
        end: dt.date,
        *,
        timeout: float = 30.0,
    ) -> list[Path]:
        """同步批量下载日期范围内的 K 线 (兼容接口).

        内部调用 download_range_async 实现并发下载.

        Args:
            symbol: 交易对, 如 "BTCUSDT".
            interval: 时间间隔枚举.
            start: 开始日期 (含).
            end: 结束日期 (含).
            timeout: 单次请求超时秒数.

        Returns:
            成功下载的 CSV 文件路径列表.

        """
        return asyncio.run(
            self.download_range_async(
                symbol=symbol,
                interval=interval,
                start=start,
                end=end,
                timeout=timeout,
            )
        )


class BinanceFuturesDownloader(BaseBinanceDownloader):
    """Binance U 本位合约 K 线下载器."""

    TRADER_TYPE = TraderType.FUTURES
    PATH_TEMPLATE = "data/futures/um/daily/klines"


class BinanceSpotDownloader(BaseBinanceDownloader):
    """Binance 现货 K 线下载器 (FIX-4)."""

    TRADER_TYPE = TraderType.SPOT
    PATH_TEMPLATE = "data/spot/daily/klines"


# ---------------------------------------------------------------------------
# Catalog 加载器
# ---------------------------------------------------------------------------


class KlineCatalogLoader:
    """K 线数据 → Nautilus ParquetDataCatalog.

    流程: CSV → DataFrame → 验证 → Wrangler → Parquet
    """

    KLINE_COLUMNS = [
        "open_time",
        "open",
        "high",
        "low",
        "close",
        "volume",
        "close_time",
        "quote_volume",
        "count",
        "taker_buy_volume",
        "taker_buy_quote_volume",
        "ignore",
    ]

    def __init__(self, catalog_dir: Path) -> None:
        """初始化 Catalog 加载器.

        Args:
            catalog_dir: Parquet Catalog 根目录, 不存在时自动创建.

        """
        self._catalog_dir = catalog_dir
        self._catalog_dir.mkdir(parents=True, exist_ok=True)
        self._catalog = ParquetDataCatalog(catalog_dir)
        self._instrument_written: set[str] = set()

    @property
    def catalog(self) -> ParquetDataCatalog:
        """返回底层 ParquetDataCatalog 实例.

        Returns:
            ParquetDataCatalog 实例.

        """
        return self._catalog

    def _is_range_in_catalog(self, bar_type: BarType, start_ns: int, end_ns: int) -> bool:
        """检查给定时间范围内是否已存在数据, 用于去重判断.

        使用 ``ParquetDataCatalog.bars()`` 查询指定 bar_type 与时间范围内的数据.

        Args:
            bar_type: Nautilus BarType 实例.
            start_ns: 起始时间戳 (纳秒, Unix epoch).
            end_ns: 结束时间戳 (纳秒, Unix epoch).

        Returns:
            True 表示数据已存在; False 表示不存在或查询异常.

        """
        try:
            data = self._catalog.bars(
                bar_types=[str(bar_type)],
                start=start_ns,
                end=end_ns,
            )
            return len(data) > 0
        except (OSError, ValueError, AttributeError) as e:
            logger.error("catalog_bars_query_failed", bar_type=str(bar_type), error=str(e))
            raise DataError(
                f"Failed to query bars from catalog for {bar_type}",
                context={"bar_type": str(bar_type), "error": str(e)},
            ) from e

    # ------ 单文件加载 ------

    def load_csv(
        self,
        csv_path: Path,
        instrument: CryptoPerpetual,
        interval: Interval = Interval.MINUTE_1,
    ) -> int:
        """加载单个 CSV 到 catalog.

        Args:
            csv_path: CSV 文件路径
            instrument: Nautilus Instrument
            interval: 原始数据的时间间隔

        Returns:
            写入的 bar 数量

        """
        nautilus_interval = INTERVAL_TO_NAUTILUS[interval]
        bar_type = BarType.from_str(f"{instrument.id}-{nautilus_interval}-LAST-EXTERNAL")

        # 读取 + 标准化
        df = self._read_and_normalize(csv_path)

        # 验证
        validate_kline_dataframe(df)

        # 写入 instrument 信息 (FIX-3)
        instrument_key = str(instrument.id)
        if instrument_key not in self._instrument_written:
            self._catalog.write_data([instrument])
            self._instrument_written.add(instrument_key)

        # 避免重复写入 (FIX-3)
        start_ns = int(df["ts_event"].min())
        end_ns = int(df["ts_event"].max())
        if self._is_range_in_catalog(bar_type, start_ns, end_ns):
            logger.info("catalog_skip_duplicate", csv=csv_path.name, bar_type=str(bar_type))
            return 0

        # 转换 + 写入
        wrangler = BarDataWranglerV2(
            bar_type=str(bar_type),
            price_precision=instrument.price_precision,
            size_precision=instrument.size_precision,
        )
        bars = wrangler.from_pandas(df)
        self._catalog.write_data(bars)

        logger.info(
            "catalog_loaded",
            csv=csv_path.name,
            instrument=str(instrument.id),
            bar_type=str(bar_type),
            bar_count=len(bars),
        )
        return len(bars)

    # ------ 批量加载 ------

    def load_csvs(
        self,
        csv_paths: list[Path],
        instrument: CryptoPerpetual,
        interval: Interval = Interval.MINUTE_1,
        *,
        cleanup_raw: bool = False,
    ) -> int:
        """批量加载多个 CSV.

        Args:
            csv_paths: CSV 文件路径列表
            instrument: Nautilus Instrument
            interval: 时间间隔
            cleanup_raw: 是否在加载后删除 CSV (FIX-11)

        Returns:
            总写入 bar 数量

        """
        total = 0
        for csv_path in csv_paths:
            try:
                count = self.load_csv(csv_path, instrument, interval)
                total += count
                if cleanup_raw and csv_path.exists():
                    csv_path.unlink(missing_ok=True)
            except (OSError, ValueError, pd.errors.ParserError) as e:
                logger.exception("catalog_load_error", csv=str(csv_path))
                raise DataError(
                    f"Failed to load CSV {csv_path}", context={"csv_path": str(csv_path), "error": str(e)}
                ) from e
        logger.info("batch_catalog_load_done", total_bars=total, files=len(csv_paths))
        return total

    # ------ 内部方法 ------

    def _read_and_normalize(self, csv_path: Path) -> pd.DataFrame:
        """读取 CSV 并标准化列名与时间戳.

        兼容 Binance 有 header / 无 header 两种 CSV 格式.
        自动追加 Nautilus 所需的 ts_event 列 (纳秒时间戳).

        Args:
            csv_path: 待读取的 CSV 文件路径.

        Returns:
            包含标准列名和 ts_event 列的 DataFrame.

        """
        df_with_header = pd.read_csv(csv_path, header=0)
        if "open_time" not in df_with_header.columns:
            df = pd.read_csv(csv_path, header=None, names=self.KLINE_COLUMNS)
        else:
            df = df_with_header

        # 添加 Nautilus 需要的 ts_event 列
        df["ts_event"] = pd.to_datetime(df["open_time"], unit="ms", utc=True).astype("int64")
        return df


# ---------------------------------------------------------------------------
# 组合: 下载 + 加载 一条龙
# ---------------------------------------------------------------------------


class DataPipeline:
    """数据管道: 下载 → 验证 → 写入 Catalog.

    Usage:
        pipeline = DataPipeline(raw_dir=..., catalog_dir=...)
        pipeline.run(
            instrument=TestInstrumentProvider.btcusdt_perp_binance(),
            symbol="BTCUSDT",
            interval=Interval.MINUTE_1,
            start=date(2025, 11, 1),
            end=date(2025, 12, 16),
        )
    """

    def __init__(self, raw_dir: Path, catalog_dir: Path) -> None:
        """初始化数据管道.

        Args:
            raw_dir: 原始 CSV 存储根目录.
            catalog_dir: Parquet Catalog 根目录.

        """
        self.downloader = BinanceFuturesDownloader(raw_dir)
        self.loader = KlineCatalogLoader(catalog_dir)

    def run(
        self,
        instrument: CryptoPerpetual,
        symbol: str,
        interval: Interval,
        start: dt.date,
        end: dt.date,
        *,
        cleanup_raw: bool = False,
    ) -> int:
        """执行完整管道: 下载 → 加载.

        Args:
            instrument: Nautilus Instrument
            symbol: Binance 交易对名 (如 "BTCUSDT")
            interval: 时间间隔
            start: 开始日期
            end: 结束日期
            cleanup_raw: 是否清理原始 CSV (FIX-11)

        Returns:
            总写入 bar 数量

        """
        logger.info(
            "pipeline_start",
            symbol=symbol,
            interval=interval.value,
            start=str(start),
            end=str(end),
        )

        # 1. 下载
        csv_paths = self.downloader.download_range(symbol, interval, start, end)

        if not csv_paths:
            logger.warning("pipeline_no_data", symbol=symbol)
            return 0

        # 2. 加载到 catalog
        total_bars = self.loader.load_csvs(csv_paths, instrument, interval, cleanup_raw=cleanup_raw)

        # 3. 连续性检查 (FIX-2)
        try:
            df_all = pd.concat([self.loader._read_and_normalize(p) for p in csv_paths], ignore_index=True)
            gaps = validate_data_completeness(df_all)
            if gaps:
                logger.warning("data_gaps_report", symbol=symbol, gap_count=len(gaps), gaps=gaps)
        except (ValueError, TypeError, KeyError) as e:
            logger.exception("data_gap_check_failed", symbol=symbol)
            raise DataError(
                f"Failed to check data gaps for {symbol}", symbol=symbol, context={"symbol": symbol, "error": str(e)}
            ) from e

        logger.info(
            "pipeline_done",
            symbol=symbol,
            csv_files=len(csv_paths),
            total_bars=total_bars,
        )
        return total_bars

    def run_multi_symbol(
        self,
        instruments: dict[str, CryptoPerpetual],
        interval: Interval,
        start: dt.date,
        end: dt.date,
    ) -> dict[str, int]:
        """多交易对批量管道.

        Args:
            instruments: {symbol: instrument} 映射
            interval: 时间间隔
            start: 开始日期
            end: 结束日期

        Returns:
            {symbol: total_bars} 映射

        """
        results: dict[str, int] = {}
        for symbol, instrument in instruments.items():
            results[symbol] = self.run(instrument, symbol, interval, start, end)
        return results
