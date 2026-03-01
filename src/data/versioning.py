"""数据版本管理.

对处理后的数据进行版本化, 支持回滚和对比.
"""

from __future__ import annotations

import hashlib
import json
import shutil
import time
from pathlib import Path
from typing import Any, cast

import structlog

logger = structlog.get_logger()


class DataVersionManager:
    """数据版本管理器."""

    def __init__(self, versioned_dir: Path) -> None:
        self._versioned_dir = versioned_dir
        self._versioned_dir.mkdir(parents=True, exist_ok=True)
        self._manifest_path = versioned_dir / "manifest.json"
        self._manifest = self._load_manifest()

    def _load_manifest(self) -> dict[str, Any]:
        if self._manifest_path.exists():
            with open(self._manifest_path) as f:
                return cast("dict[str, Any]", json.load(f))
        return {"versions": []}

    def _save_manifest(self) -> None:
        with open(self._manifest_path, "w") as f:
            json.dump(self._manifest, f, indent=2)

    def create_version(self, source_dir: Path, description: str = "") -> str:
        """创建数据版本快照.

        Args:
            source_dir: 要版本化的数据目录
            description: 版本描述

        Returns:
            版本 ID
        """
        version_id = f"v_{int(time.time())}"
        version_dir = self._versioned_dir / version_id

        # 复制数据
        shutil.copytree(source_dir, version_dir)

        # 计算校验和
        checksum = self._compute_checksum(version_dir)

        # 更新 manifest
        self._manifest["versions"].append(
            {
                "id": version_id,
                "timestamp": time.time(),
                "description": description,
                "checksum": checksum,
                "path": str(version_dir),
            }
        )
        self._save_manifest()

        logger.info("data_version_created", version_id=version_id, checksum=checksum)
        return version_id

    def list_versions(self) -> list[dict[str, Any]]:
        """列出所有版本."""
        return cast("list[dict[str, Any]]", self._manifest.get("versions", []))

    @staticmethod
    def _compute_checksum(directory: Path) -> str:
        """计算目录内容的 SHA256 校验和."""
        hasher = hashlib.sha256()
        for file_path in sorted(directory.rglob("*")):
            if file_path.is_file():
                hasher.update(file_path.read_bytes())
        return hasher.hexdigest()[:16]
