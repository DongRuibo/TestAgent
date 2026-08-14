"""本地 POC Artifact Store：原子写入、哈希校验和路径隔离。"""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
from pathlib import Path
from typing import Any

from sandbox_runtime.models import ArtifactRecord


SAFE_COMPONENT = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")


class ArtifactStore:
    """把输入、输出和 Receipt 保存到 attempt 独立目录。"""

    def __init__(self, root: Path | str) -> None:
        self.root = Path(root).resolve()

    def put_bytes(
        self,
        attempt_id: str,
        name: str,
        data: bytes,
        media_type: str = "application/octet-stream",
    ) -> ArtifactRecord:
        attempt = self._safe_component(attempt_id, "attempt_id")
        filename = self._safe_component(name, "artifact name")
        target_dir = self.root / attempt
        target_dir.mkdir(parents=True, exist_ok=True)
        target = target_dir / filename

        temporary_name: str | None = None
        try:
            with tempfile.NamedTemporaryFile(dir=target_dir, delete=False) as temporary:
                temporary.write(data)
                temporary.flush()
                os.fsync(temporary.fileno())
                temporary_name = temporary.name
            os.replace(temporary_name, target)
        finally:
            if temporary_name and Path(temporary_name).exists():
                Path(temporary_name).unlink()

        return ArtifactRecord(
            name=filename,
            relative_path=str(target.relative_to(self.root)),
            sha256=hashlib.sha256(data).hexdigest(),
            size_bytes=len(data),
            media_type=media_type,
        )

    def put_json(
        self,
        attempt_id: str,
        name: str,
        value: Any,
    ) -> ArtifactRecord:
        encoded = json.dumps(value, ensure_ascii=False, indent=2).encode("utf-8") + b"\n"
        return self.put_bytes(attempt_id, name, encoded, "application/json")

    def read_bytes(self, record: ArtifactRecord) -> bytes:
        target = (self.root / record.relative_path).resolve()
        if not target.is_relative_to(self.root):
            raise ValueError("Artifact path escapes store root.")
        data = target.read_bytes()
        digest = hashlib.sha256(data).hexdigest()
        if digest != record.sha256:
            raise ValueError(f"Artifact hash mismatch for {record.name}.")
        return data

    @staticmethod
    def _safe_component(value: str, label: str) -> str:
        if not SAFE_COMPONENT.fullmatch(value):
            raise ValueError(f"Invalid {label}: {value!r}")
        return value
