"""使用 SQLite 持久化模块任务状态，支持 Agent 重启后的记录查询与中断恢复。"""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .models import JobRecord, WorkflowName


def utc_now() -> str:
    """返回可稳定排序和序列化的 UTC 时间字符串。"""

    return datetime.now(UTC).isoformat()


class JobRepository:
    """为任务创建、状态更新和查询提供最小 SQLite 访问层。"""

    def __init__(self, database_path: Path) -> None:
        """保存数据库绝对路径并创建父目录。"""

        self.database_path = database_path.resolve()
        self.database_path.parent.mkdir(parents=True, exist_ok=True)

    def connect(self) -> sqlite3.Connection:
        """创建启用字典行访问和忙等待的独立短连接。"""

        connection = sqlite3.connect(self.database_path, timeout=10)
        connection.row_factory = sqlite3.Row
        return connection

    def initialize(self) -> None:
        """创建模块 V1 的单一任务表和状态索引。"""

        with self.connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS jobs (
                    id TEXT PRIMARY KEY,
                    workflow TEXT NOT NULL,
                    status TEXT NOT NULL,
                    current_step TEXT NOT NULL,
                    input_json TEXT NOT NULL,
                    result_json TEXT,
                    error_code TEXT,
                    error_message TEXT,
                    created_at TEXT NOT NULL,
                    started_at TEXT,
                    finished_at TEXT
                );
                CREATE INDEX IF NOT EXISTS idx_jobs_status ON jobs(status);
                """
            )

    def create(self, job_id: str, workflow: WorkflowName, payload: dict[str, Any]) -> JobRecord:
        """新增 queued 任务并返回不含部署配置的任务快照。"""

        created_at = utc_now()
        with self.connect() as connection:
            connection.execute(
                """
                INSERT INTO jobs (id, workflow, status, current_step, input_json, created_at)
                VALUES (?, ?, 'queued', 'queued', ?, ?)
                """,
                (job_id, workflow, json.dumps(payload, ensure_ascii=False), created_at),
            )
        job = self.get(job_id)
        if job is None:
            raise RuntimeError("任务创建后无法读取")
        return job

    def get(self, job_id: str) -> JobRecord | None:
        """按任务 ID 返回解析后的不可变任务模型。"""

        with self.connect() as connection:
            row = connection.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
        return self._decode_row(row) if row else None

    def get_active_job(self) -> JobRecord | None:
        """返回当前 queued 或 running 任务，模块第一版最多存在一个。"""

        with self.connect() as connection:
            row = connection.execute(
                "SELECT * FROM jobs WHERE status IN ('queued', 'running') ORDER BY created_at LIMIT 1"
            ).fetchone()
        return self._decode_row(row) if row else None

    def recover_interrupted_jobs(self) -> int:
        """把上次异常退出遗留的活动任务转为失败，避免永久阻塞。"""

        with self.connect() as connection:
            cursor = connection.execute(
                """
                UPDATE jobs
                SET status = 'failed', current_step = 'failed',
                    error_code = 'module_interrupted',
                    error_message = '模块上次运行中断，请确认现场后重新创建任务',
                    finished_at = ?
                WHERE status IN ('queued', 'running')
                """,
                (utc_now(),),
            )
        return cursor.rowcount

    def update_running(self, job_id: str, current_step: str = "starting_browser") -> None:
        """把排队任务转为运行中并记录实际开始时间。"""

        with self.connect() as connection:
            connection.execute(
                "UPDATE jobs SET status = 'running', current_step = ?, started_at = ? "
                "WHERE id = ? AND status = 'queued'",
                (current_step, utc_now(), job_id),
            )

    def update_step(self, job_id: str, current_step: str) -> None:
        """只更新运行任务步骤，迟到的子进程不能覆盖终态。"""

        with self.connect() as connection:
            connection.execute(
                "UPDATE jobs SET current_step = ? WHERE id = ? AND status = 'running'",
                (current_step, job_id),
            )

    def complete(self, job_id: str, result: dict[str, Any]) -> None:
        """将运行任务标记成功并保存可序列化结果。"""

        with self.connect() as connection:
            connection.execute(
                "UPDATE jobs SET status = 'succeeded', current_step = 'completed', result_json = ?, "
                "finished_at = ? WHERE id = ? AND status = 'running'",
                (json.dumps(result, ensure_ascii=False), utc_now(), job_id),
            )

    def needs_user_action(self, job_id: str, message: str, result: dict[str, Any]) -> None:
        """将未登录或风控场景收口为人工介入终态。"""

        with self.connect() as connection:
            connection.execute(
                "UPDATE jobs SET status = 'needs_user_action', current_step = 'needs_user_action', "
                "result_json = ?, error_code = 'needs_user_action', error_message = ?, finished_at = ? "
                "WHERE id = ? AND status = 'running'",
                (json.dumps(result, ensure_ascii=False), message, utc_now(), job_id),
            )

    def fail(self, job_id: str, error_code: str, error_message: str) -> None:
        """将非终态任务标记失败，仅保存安全错误摘要。"""

        with self.connect() as connection:
            connection.execute(
                "UPDATE jobs SET status = 'failed', current_step = 'failed', error_code = ?, "
                "error_message = ?, finished_at = ? WHERE id = ? AND status IN ('queued', 'running')",
                (error_code, error_message, utc_now(), job_id),
            )

    def stop(self, job_id: str) -> None:
        """将排队或运行任务标记停止，不改变既有终态。"""

        with self.connect() as connection:
            connection.execute(
                "UPDATE jobs SET status = 'stopped', current_step = 'stopped', finished_at = ? "
                "WHERE id = ? AND status IN ('queued', 'running')",
                (utc_now(), job_id),
            )

    def _decode_row(self, row: sqlite3.Row) -> JobRecord:
        """解析数据库 JSON 字段并构造公开任务模型。"""

        record = dict(row)
        record["input"] = json.loads(record.pop("input_json"))
        result_json = record.pop("result_json")
        record["result"] = json.loads(result_json) if result_json else None
        return JobRecord.model_validate(record)
