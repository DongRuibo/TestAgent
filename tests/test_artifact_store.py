"""Artifact Store 的原子写入、哈希与路径边界测试。"""

from __future__ import annotations

from pathlib import Path

import pytest

from sandbox_runtime.artifact_store import ArtifactStore


def test_artifact_store_writes_and_verifies_hash(tmp_path: Path) -> None:
    store = ArtifactStore(tmp_path / "artifacts")
    record = store.put_bytes("attempt-1", "result.txt", b"hello", "text/plain")

    assert record.sha256 == "2cf24dba5fb0a30e26e83b2ac5b9e29e1b161e5c1fa7425e73043362938b9824"
    assert store.read_bytes(record) == b"hello"


def test_artifact_store_rejects_unsafe_components(tmp_path: Path) -> None:
    store = ArtifactStore(tmp_path / "artifacts")

    with pytest.raises(ValueError, match="attempt_id"):
        store.put_bytes("../outside", "result.txt", b"no")
    with pytest.raises(ValueError, match="artifact name"):
        store.put_bytes("attempt-1", "../result.txt", b"no")


def test_artifact_store_detects_tampering(tmp_path: Path) -> None:
    store = ArtifactStore(tmp_path / "artifacts")
    record = store.put_bytes("attempt-1", "result.txt", b"original")
    (store.root / record.relative_path).write_bytes(b"tampered")

    with pytest.raises(ValueError, match="hash mismatch"):
        store.read_bytes(record)
