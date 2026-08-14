"""Gateway V0 的 SQLite 执行记录和本地上传文件仓储。"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from uuid import uuid4

from sandbox_contracts import ExecutionCreateRequest, ExecutionStatus


@dataclass(frozen=True)
class StoredFile:
    file_id: str
    tenant_id: str
    agent_id: str
    name: str
    media_type: str
    size_bytes: int
    sha256: str
    path: str


@dataclass
class StoredExecution:
    execution_id: str
    tenant_id: str
    agent_id: str
    request: ExecutionCreateRequest
    idempotency_key: str | None
    status: ExecutionStatus
    provider: str | None
    sandbox_id: str | None
    created_at: datetime
    updated_at: datetime
    started_at: datetime | None
    finished_at: datetime | None
    result: dict | None
    artifacts: list[dict]
    receipt: dict | None
    error_type: str | None
    error_message: str | None
    cancellation_requested: bool


class SQLiteRepository:
    """小型 V0 仓储；通过锁保证 Gateway 的后台线程安全。"""

    def __init__(self, path: Path | str) -> None:
        self.path = Path(path).resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._connection = sqlite3.connect(self.path, check_same_thread=False)
        self._connection.row_factory = sqlite3.Row
        self._initialize()

    def _initialize(self) -> None:
        with self._lock, self._connection:
            self._connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS executions (
                    execution_id TEXT PRIMARY KEY,
                    tenant_id TEXT NOT NULL,
                    agent_id TEXT NOT NULL,
                    request_json TEXT NOT NULL,
                    idempotency_key TEXT,
                    status TEXT NOT NULL,
                    provider TEXT,
                    sandbox_id TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    started_at TEXT,
                    finished_at TEXT,
                    result_json TEXT,
                    artifacts_json TEXT NOT NULL,
                    receipt_json TEXT,
                    error_type TEXT,
                    error_message TEXT,
                    cancellation_requested INTEGER NOT NULL DEFAULT 0
                );
                CREATE INDEX IF NOT EXISTS idx_executions_owner
                    ON executions (tenant_id, agent_id, created_at);
                CREATE UNIQUE INDEX IF NOT EXISTS idx_executions_idempotency
                    ON executions (tenant_id, agent_id, idempotency_key)
                    WHERE idempotency_key IS NOT NULL;
                CREATE TABLE IF NOT EXISTS files (
                    file_id TEXT PRIMARY KEY,
                    tenant_id TEXT NOT NULL,
                    agent_id TEXT NOT NULL,
                    name TEXT NOT NULL,
                    media_type TEXT NOT NULL,
                    size_bytes INTEGER NOT NULL,
                    sha256 TEXT NOT NULL,
                    path TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                """
            )

    def create_execution(
        self,
        execution_id: str,
        tenant_id: str,
        agent_id: str,
        request: ExecutionCreateRequest,
        now: datetime,
        idempotency_key: str | None = None,
    ) -> StoredExecution:
        record = StoredExecution(
            execution_id=execution_id,
            tenant_id=tenant_id,
            agent_id=agent_id,
            request=request,
            idempotency_key=idempotency_key,
            status=ExecutionStatus.QUEUED,
            provider=None,
            sandbox_id=None,
            created_at=now,
            updated_at=now,
            started_at=None,
            finished_at=None,
            result=None,
            artifacts=[],
            receipt=None,
            error_type=None,
            error_message=None,
            cancellation_requested=False,
        )
        self.save_execution(record)
        return record

    def save_execution(self, record: StoredExecution) -> None:
        with self._lock, self._connection:
            self._connection.execute(
                """
                INSERT OR REPLACE INTO executions (
                    execution_id, tenant_id, agent_id, request_json, idempotency_key, status,
                    provider, sandbox_id, created_at, updated_at, started_at,
                    finished_at, result_json, artifacts_json, receipt_json,
                    error_type, error_message, cancellation_requested
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    record.execution_id,
                    record.tenant_id,
                    record.agent_id,
                    json.dumps(record.request.model_dump(mode="json"), ensure_ascii=False),
                    record.idempotency_key,
                    record.status.value,
                    record.provider,
                    record.sandbox_id,
                    record.created_at.isoformat(),
                    record.updated_at.isoformat(),
                    record.started_at.isoformat() if record.started_at else None,
                    record.finished_at.isoformat() if record.finished_at else None,
                    json.dumps(record.result, ensure_ascii=False) if record.result is not None else None,
                    json.dumps(record.artifacts, ensure_ascii=False),
                    json.dumps(record.receipt, ensure_ascii=False) if record.receipt is not None else None,
                    record.error_type,
                    record.error_message,
                    1 if record.cancellation_requested else 0,
                ),
            )

    def find_by_idempotency(
        self,
        tenant_id: str,
        agent_id: str,
        idempotency_key: str,
    ) -> StoredExecution | None:
        with self._lock:
            row = self._connection.execute(
                """
                SELECT * FROM executions
                WHERE tenant_id = ? AND agent_id = ? AND idempotency_key = ?
                """,
                (tenant_id, agent_id, idempotency_key),
            ).fetchone()
        return self._row_to_execution(row) if row else None

    def get_execution(self, execution_id: str) -> StoredExecution | None:
        with self._lock:
            row = self._connection.execute(
                "SELECT * FROM executions WHERE execution_id = ?",
                (execution_id,),
            ).fetchone()
        return self._row_to_execution(row) if row else None

    def get_active_executions(self) -> list[StoredExecution]:
        with self._lock:
            rows = self._connection.execute(
                "SELECT * FROM executions WHERE status IN (?, ?, ?, ?, ?, ?)",
                tuple(
                    status.value
                    for status in (
                        ExecutionStatus.QUEUED,
                        ExecutionStatus.POLICY_CHECKED,
                        ExecutionStatus.CREATING,
                        ExecutionStatus.RUNNING,
                        ExecutionStatus.COLLECTING,
                        ExecutionStatus.TERMINATING,
                    )
                ),
            ).fetchall()
        return [self._row_to_execution(row) for row in rows]

    def create_file(
        self,
        tenant_id: str,
        agent_id: str,
        name: str,
        media_type: str,
        data: bytes,
        directory: Path,
    ) -> StoredFile:
        file_id = f"file-{uuid4().hex}"
        directory.mkdir(parents=True, exist_ok=True)
        target = directory / file_id
        target.write_bytes(data)
        record = StoredFile(
            file_id=file_id,
            tenant_id=tenant_id,
            agent_id=agent_id,
            name=name,
            media_type=media_type,
            size_bytes=len(data),
            sha256=hashlib.sha256(data).hexdigest(),
            path=str(target),
        )
        with self._lock, self._connection:
            self._connection.execute(
                """
                INSERT INTO files
                (file_id, tenant_id, agent_id, name, media_type, size_bytes, sha256, path, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    record.file_id,
                    record.tenant_id,
                    record.agent_id,
                    record.name,
                    record.media_type,
                    record.size_bytes,
                    record.sha256,
                    record.path,
                    datetime.now().astimezone().isoformat(),
                ),
            )
        return record

    def get_file(self, file_id: str) -> StoredFile | None:
        with self._lock:
            row = self._connection.execute(
                "SELECT * FROM files WHERE file_id = ?",
                (file_id,),
            ).fetchone()
        if row is None:
            return None
        return StoredFile(
            file_id=row["file_id"],
            tenant_id=row["tenant_id"],
            agent_id=row["agent_id"],
            name=row["name"],
            media_type=row["media_type"],
            size_bytes=int(row["size_bytes"]),
            sha256=row["sha256"],
            path=row["path"],
        )

    @staticmethod
    def _row_to_execution(row: sqlite3.Row) -> StoredExecution:
        return StoredExecution(
            execution_id=row["execution_id"],
            tenant_id=row["tenant_id"],
            agent_id=row["agent_id"],
            request=ExecutionCreateRequest.model_validate(json.loads(row["request_json"])),
            idempotency_key=row["idempotency_key"],
            status=ExecutionStatus(row["status"]),
            provider=row["provider"],
            sandbox_id=row["sandbox_id"],
            created_at=datetime.fromisoformat(row["created_at"]),
            updated_at=datetime.fromisoformat(row["updated_at"]),
            started_at=datetime.fromisoformat(row["started_at"]) if row["started_at"] else None,
            finished_at=datetime.fromisoformat(row["finished_at"]) if row["finished_at"] else None,
            result=json.loads(row["result_json"]) if row["result_json"] else None,
            artifacts=json.loads(row["artifacts_json"]),
            receipt=json.loads(row["receipt_json"]) if row["receipt_json"] else None,
            error_type=row["error_type"],
            error_message=row["error_message"],
            cancellation_requested=bool(row["cancellation_requested"]),
        )
