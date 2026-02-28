"""数据模块.

提供数据下载、验证、加载到 Catalog 的完整管道.
"""

from src.data.loaders import (
    BinanceFuturesDownloader,
    DataPipeline,
    KlineCatalogLoader,
)
from src.data.validators import (
    DataValidationError,
    validate_data_completeness,
    validate_kline_dataframe,
)

__all__ = [
    "BinanceFuturesDownloader",
    "DataPipeline",
    "KlineCatalogLoader",
    "DataValidationError",
    "validate_data_completeness",
    "validate_kline_dataframe",
]
