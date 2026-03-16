"""特征存储.

管理策略用到的衍生特征 (指标值、市场状态等).
"""

from __future__ import annotations

from pathlib import Path

import pandas as pd
import structlog

logger = structlog.get_logger()


class FeatureStore:
    """特征存储管理器.

    使用 Parquet 存储计算好的特征, 避免重复计算.
    """

    def __init__(self, features_dir: Path) -> None:
        """Initialize the feature store.

        Args:
            features_dir: Directory for features.
        """
        self._features_dir = features_dir
        self._features_dir.mkdir(parents=True, exist_ok=True)

    def save_features(self, name: str, df: pd.DataFrame) -> Path:
        """保存特征到 Parquet.

        Args:
            name: 特征集名称
            df: 特征 DataFrame

        Returns:
            文件路径

        """
        path = self._features_dir / f"{name}.parquet"
        df.to_parquet(path, index=False)
        logger.info("features_saved", name=name, rows=len(df), path=str(path))
        return path

    def load_features(self, name: str) -> pd.DataFrame | None:
        """加载特征.

        Args:
            name: 特征集名称

        Returns:
            DataFrame 或 None

        """
        path = self._features_dir / f"{name}.parquet"
        if not path.exists():
            logger.warning("features_not_found", name=name)
            return None
        return pd.read_parquet(path)

    def list_features(self) -> list[str]:
        """列出所有已保存的特征集."""
        return [p.stem for p in self._features_dir.glob("*.parquet")]
