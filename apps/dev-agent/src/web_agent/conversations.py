"""本地 Web Agent 的最小 SQLite 对话存储。"""

from __future__ import annotations

import json
import sqlite3
import stat
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta, timezone
from pathlib import Path, PurePosixPath
from typing import Any
from urllib.parse import urlsplit
from uuid import UUID, uuid4


OWNER_REMINDER_INTERVAL_HOURS = 4
OWNER_ESCALATION_AFTER_HOURS = 24
CHINA_TIMEZONE = timezone(timedelta(hours=8))
OWNER_STATUS_LABELS = {
    "detected": "待处理",
    "assigned": "已分派",
    "completed": "已完成",
    "escalated": "已升级",
}
OWNER_SEVERITY_LABELS = {
    "critical": "紧急",
    "high": "重要",
    "medium": "一般",
}
OWNER_ACTIONABLE_STATUSES = {"detected", "assigned"}


class ConversationNotFoundError(LookupError):
    pass


class AttachmentNotFoundError(LookupError):
    pass


class GenerationUiArtifactNotFoundError(LookupError):
    pass


class GenerationUiActionConflictError(RuntimeError):
    pass


class ConversationResourceNotFoundError(LookupError):
    pass


@dataclass(frozen=True)
class StoredConversationMessage:
    message_id: int
    role: str
    content: str
    status: str = "completed"
    turn_id: str | None = None
    trace: tuple[str, ...] = ()
    duration_ms: int | None = None
    context: dict[str, Any] | None = None


@dataclass(frozen=True)
class ConversationSummary:
    conversation_id: str
    title: str
    updated_at: str
    message_count: int


@dataclass(frozen=True)
class ConversationResource:
    resource_id: str
    conversation_id: str
    message_id: int
    kind: str
    title: str
    subtitle: str | None
    url: str | None
    path: str | None
    media_type: str | None


@dataclass(frozen=True)
class ConversationUiArtifact:
    artifact_id: str
    conversation_id: str
    message_id: int
    scenario: str
    text: str
    spec: dict[str, Any]
    data: dict[str, Any]


@dataclass(frozen=True)
class ConversationAttachment:
    attachment_id: str
    conversation_id: str
    knowledge_base_kind: str | None
    knowledge_base_id: str | None
    document_id: str | None
    job_id: str | None
    filename: str
    content_type: str
    size_bytes: int
    status: str
    progress: float
    error_message: str | None
    created_at: str
    updated_at: str

    def to_dict(self) -> dict[str, str | int | float | None]:
        return {
            "attachment_id": self.attachment_id,
            "conversation_id": self.conversation_id,
            "knowledge_base_kind": self.knowledge_base_kind,
            "knowledge_base_id": self.knowledge_base_id,
            "document_id": self.document_id,
            "job_id": self.job_id,
            "filename": self.filename,
            "content_type": self.content_type,
            "size_bytes": self.size_bytes,
            "status": self.status,
            "progress": self.progress,
            "error_message": self.error_message,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }


class SQLiteConversationStore:
    def __init__(self, path: Path) -> None:
        self.path = path
        if self.path.is_symlink() or self.path.exists():
            metadata = self.path.lstat()
            if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
                raise ValueError("Conversation database must be a regular file")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()
        self.path.chmod(0o600)

    def create(self) -> str:
        conversation_id = str(uuid4())
        now = _now()
        with self._connect() as connection:
            connection.execute(
                "INSERT INTO conversations (conversation_id, created_at, updated_at) VALUES (?, ?, ?)",
                (conversation_id, now, now),
            )
        return conversation_id

    def rename(self, conversation_id: str, title: str) -> None:
        _validate_conversation_id(conversation_id)
        normalized = title.strip()
        if not normalized or len(normalized) > 80:
            raise ValueError("Conversation title must contain 1-80 characters")
        with self._connect() as connection:
            updated = connection.execute(
                """
                UPDATE conversations
                SET custom_title = ?, updated_at = ?
                WHERE conversation_id = ?
                """,
                (normalized, _now(), conversation_id),
            ).rowcount
            if updated != 1:
                raise ConversationNotFoundError(conversation_id)

    def conversation_summaries(
        self, *, limit: int = 50
    ) -> list[ConversationSummary]:
        if (
            isinstance(limit, bool)
            or not isinstance(limit, int)
            or not 1 <= limit <= 100
        ):
            raise ValueError("Conversation summary limit must be between 1 and 100")
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT
                    conversations.conversation_id,
                    conversations.custom_title,
                    COALESCE(
                        (
                            SELECT content
                            FROM messages AS first_message
                            WHERE first_message.conversation_id = conversations.conversation_id
                              AND first_message.role = 'user'
                            ORDER BY first_message.message_id ASC
                            LIMIT 1
                        ),
                        ''
                    ) AS first_user_message,
                    conversations.updated_at,
                    COUNT(messages.message_id) AS message_count
                FROM conversations
                LEFT JOIN messages
                    ON messages.conversation_id = conversations.conversation_id
                GROUP BY conversations.conversation_id, conversations.custom_title,
                         conversations.updated_at
                ORDER BY conversations.updated_at DESC, conversations.created_at DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
        return [
            ConversationSummary(
                conversation_id=str(conversation_id),
                title=(
                    str(custom_title).strip()
                    if custom_title is not None and str(custom_title).strip()
                    else _conversation_title(str(first_user_message))
                ),
                updated_at=str(updated_at),
                message_count=int(message_count),
            )
            for conversation_id, custom_title, first_user_message, updated_at, message_count in rows
        ]

    def message_records(self, conversation_id: str) -> list[StoredConversationMessage]:
        _validate_conversation_id(conversation_id)
        with self._connect() as connection:
            exists = connection.execute(
                "SELECT 1 FROM conversations WHERE conversation_id = ?",
                (conversation_id,),
            ).fetchone()
            if exists is None:
                raise ConversationNotFoundError(conversation_id)
            rows = connection.execute(
                """
                SELECT
                    messages.message_id,
                    messages.role,
                    messages.content,
                    messages.status,
                    messages.turn_id,
                    message_execution_details.trace_json,
                    message_execution_details.duration_ms,
                    message_execution_details.context_json
                FROM messages
                LEFT JOIN message_execution_details
                    ON message_execution_details.message_id = messages.message_id
                WHERE messages.conversation_id = ?
                ORDER BY messages.message_id ASC
                """,
                (conversation_id,),
            ).fetchall()
        return [
            StoredConversationMessage(
                message_id=int(message_id),
                role=str(role),
                content=str(content),
                status=str(status),
                turn_id=str(turn_id) if turn_id is not None else None,
                trace=tuple(_json_string_list(trace_json, "execution trace"))
                if trace_json is not None
                else (),
                duration_ms=int(duration_ms) if duration_ms is not None else None,
                context=_json_object(context_json, "execution context")
                if context_json is not None
                else None,
            )
            for (
                message_id,
                role,
                content,
                status,
                turn_id,
                trace_json,
                duration_ms,
                context_json,
            ) in rows
        ]

    def messages(self, conversation_id: str) -> list[dict[str, str]]:
        return [
            {"role": item.role, "content": item.content}
            for item in self.message_records(conversation_id)
            if item.status == "completed"
        ]

    def begin_turn(
        self,
        conversation_id: str,
        user: str,
        *,
        turn_id: str | None = None,
    ) -> str:
        """立即保存用户消息，避免停止生成时丢失已发送的问题。"""
        _validate_conversation_id(conversation_id)
        identifier = turn_id or str(uuid4())
        _validate_uuid(identifier, "turn_id")
        now = _now()
        with self._connect() as connection:
            updated = connection.execute(
                "UPDATE conversations SET updated_at = ? WHERE conversation_id = ?",
                (now, conversation_id),
            ).rowcount
            if updated != 1:
                raise ConversationNotFoundError(conversation_id)
            connection.execute(
                """
                INSERT INTO messages (
                    conversation_id, role, content, status, turn_id, created_at
                ) VALUES (?, 'user', ?, 'pending', ?, ?)
                """,
                (conversation_id, user, identifier, now),
            )
        return identifier

    def stop_turn(
        self,
        conversation_id: str,
        turn_id: str,
        *,
        partial_answer: str = "",
        status: str = "stopped",
        trace: Sequence[str] = (),
        duration_ms: int | None = None,
    ) -> int | None:
        """结束未完成轮次；用户消息始终保留，部分回答按需保存。"""
        _validate_conversation_id(conversation_id)
        _validate_uuid(turn_id, "turn_id")
        if status not in {"stopped", "failed"}:
            raise ValueError("Incomplete turn status must be stopped or failed")
        serialized_trace = _serialize_trace(trace)
        now = _now()
        assistant_message_id: int | None = None
        with self._connect() as connection:
            updated = connection.execute(
                """
                UPDATE messages SET status = ?
                WHERE conversation_id = ? AND turn_id = ?
                  AND role = 'user' AND status = 'pending'
                """,
                (status, conversation_id, turn_id),
            ).rowcount
            if updated != 1:
                raise ConversationNotFoundError(conversation_id)
            if partial_answer:
                cursor = connection.execute(
                    """
                    INSERT INTO messages (
                        conversation_id, role, content, status, turn_id, created_at
                    ) VALUES (?, 'assistant', ?, ?, ?, ?)
                    """,
                    (conversation_id, partial_answer, status, turn_id, now),
                )
                assistant_message_id = int(cursor.lastrowid)
                if serialized_trace or duration_ms is not None:
                    connection.execute(
                        """
                        INSERT INTO message_execution_details (
                            message_id, trace_json, duration_ms, context_json
                        ) VALUES (?, ?, ?, NULL)
                        """,
                        (
                            assistant_message_id,
                            json.dumps(
                                serialized_trace,
                                ensure_ascii=False,
                                separators=(",", ":"),
                            ),
                            duration_ms,
                        ),
                    )
            connection.execute(
                "UPDATE conversations SET updated_at = ? WHERE conversation_id = ?",
                (now, conversation_id),
            )
        return assistant_message_id

    def append_exchange(
        self,
        conversation_id: str,
        user: str,
        assistant: str,
        *,
        ui_artifacts: Sequence[dict[str, Any]] = (),
        resources: Sequence[dict[str, Any]] = (),
        trace: Sequence[str] = (),
        duration_ms: int | None = None,
        context: dict[str, Any] | None = None,
    ) -> int:
        _validate_conversation_id(conversation_id)
        serialized_artifacts = [_serialize_ui_artifact(item) for item in ui_artifacts]
        serialized_resources = [_serialize_resource(item) for item in resources]
        serialized_trace = _serialize_trace(trace)
        if duration_ms is not None and (
            isinstance(duration_ms, bool)
            or not isinstance(duration_ms, int)
            or duration_ms < 0
        ):
            raise ValueError("Conversation execution duration must be non-negative")
        if context is not None and not isinstance(context, dict):
            raise ValueError("Conversation execution context must be an object")
        turn_id = self.begin_turn(conversation_id, user)
        return self.complete_turn(
            conversation_id,
            turn_id,
            assistant,
            ui_artifacts=serialized_artifacts,
            resources=serialized_resources,
            trace=serialized_trace,
            duration_ms=duration_ms,
            context=context,
            serialized=True,
        )

    def complete_turn(
        self,
        conversation_id: str,
        turn_id: str,
        assistant: str,
        *,
        ui_artifacts: Sequence[dict[str, Any]] = (),
        resources: Sequence[dict[str, Any]] = (),
        trace: Sequence[str] = (),
        duration_ms: int | None = None,
        context: dict[str, Any] | None = None,
        serialized: bool = False,
    ) -> int:
        """原子地完成 pending 轮次并保存助手消息和相关产物。"""
        _validate_conversation_id(conversation_id)
        _validate_uuid(turn_id, "turn_id")
        serialized_artifacts = (
            list(ui_artifacts)
            if serialized
            else [_serialize_ui_artifact(item) for item in ui_artifacts]
        )
        serialized_resources = (
            list(resources)
            if serialized
            else [_serialize_resource(item) for item in resources]
        )
        serialized_trace = list(trace) if serialized else _serialize_trace(trace)
        if duration_ms is not None and (
            isinstance(duration_ms, bool)
            or not isinstance(duration_ms, int)
            or duration_ms < 0
        ):
            raise ValueError("Conversation execution duration must be non-negative")
        if context is not None and not isinstance(context, dict):
            raise ValueError("Conversation execution context must be an object")
        now = _now()
        with self._connect() as connection:
            updated = connection.execute(
                """
                UPDATE messages SET status = 'completed'
                WHERE conversation_id = ? AND turn_id = ?
                  AND role = 'user' AND status = 'pending'
                """,
                (conversation_id, turn_id),
            ).rowcount
            if updated != 1:
                raise ConversationNotFoundError(conversation_id)
            assistant_message_id = _insert_assistant_response(
                connection,
                conversation_id,
                assistant,
                turn_id=turn_id,
                serialized_artifacts=serialized_artifacts,
                serialized_resources=serialized_resources,
                serialized_trace=serialized_trace,
                duration_ms=duration_ms,
                context=context,
                now=now,
            )
        return assistant_message_id

    def append_assistant_response(
        self,
        conversation_id: str,
        assistant: str,
        *,
        ui_artifacts: Sequence[dict[str, Any]] = (),
        resources: Sequence[dict[str, Any]] = (),
        trace: Sequence[str] = (),
        duration_ms: int | None = None,
        context: dict[str, Any] | None = None,
    ) -> int:
        """保存由已审计 UI 事件触发的助手结果，不伪造用户消息。"""
        _validate_conversation_id(conversation_id)
        serialized_artifacts = [_serialize_ui_artifact(item) for item in ui_artifacts]
        serialized_resources = [_serialize_resource(item) for item in resources]
        serialized_trace = _serialize_trace(trace)
        if duration_ms is not None and (
            isinstance(duration_ms, bool)
            or not isinstance(duration_ms, int)
            or duration_ms < 0
        ):
            raise ValueError("Conversation execution duration must be non-negative")
        if context is not None and not isinstance(context, dict):
            raise ValueError("Conversation execution context must be an object")
        with self._connect() as connection:
            return _insert_assistant_response(
                connection,
                conversation_id,
                assistant,
                turn_id=None,
                serialized_artifacts=serialized_artifacts,
                serialized_resources=serialized_resources,
                serialized_trace=serialized_trace,
                duration_ms=duration_ms,
                context=context,
                now=_now(),
            )

    def conversation_resources(
        self, conversation_id: str
    ) -> list[ConversationResource]:
        _validate_conversation_id(conversation_id)
        with self._connect() as connection:
            exists = connection.execute(
                "SELECT 1 FROM conversations WHERE conversation_id = ?",
                (conversation_id,),
            ).fetchone()
            if exists is None:
                raise ConversationNotFoundError(conversation_id)
            rows = connection.execute(
                """
                SELECT resource_id, conversation_id, message_id, kind, title,
                       subtitle, url, path, media_type
                FROM conversation_resources
                WHERE conversation_id = ?
                ORDER BY message_id ASC, created_at ASC
                """,
                (conversation_id,),
            ).fetchall()
        return [ConversationResource(*row) for row in rows]

    def resource(self, resource_id: str) -> ConversationResource:
        try:
            UUID(resource_id)
        except ValueError as exc:
            raise ConversationResourceNotFoundError(resource_id) from exc
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT resource_id, conversation_id, message_id, kind, title,
                       subtitle, url, path, media_type
                FROM conversation_resources
                WHERE resource_id = ?
                """,
                (resource_id,),
            ).fetchone()
        if row is None:
            raise ConversationResourceNotFoundError(resource_id)
        return ConversationResource(*row)

    def generation_ui_artifacts(
        self, conversation_id: str
    ) -> list[ConversationUiArtifact]:
        _validate_conversation_id(conversation_id)
        with self._connect() as connection:
            exists = connection.execute(
                "SELECT 1 FROM conversations WHERE conversation_id = ?",
                (conversation_id,),
            ).fetchone()
            if exists is None:
                raise ConversationNotFoundError(conversation_id)
            rows = connection.execute(
                """
                SELECT artifact_id, conversation_id, message_id, scenario,
                       text, spec_json, data_json
                FROM generation_ui_artifacts
                WHERE conversation_id = ?
                ORDER BY message_id ASC, created_at ASC
                """,
                (conversation_id,),
            ).fetchall()
            artifacts = [
                _conversation_ui_artifact(connection, row) for row in rows
            ]
        return artifacts

    def generation_ui_artifact(
        self, conversation_id: str, artifact_id: str
    ) -> ConversationUiArtifact:
        _validate_conversation_id(conversation_id)
        _validate_uuid(artifact_id, "artifact_id")
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT artifact_id, conversation_id, message_id, scenario,
                       text, spec_json, data_json
                FROM generation_ui_artifacts
                WHERE conversation_id = ? AND artifact_id = ?
                """,
                (conversation_id, artifact_id),
            ).fetchone()
            if row is None:
                raise GenerationUiArtifactNotFoundError(artifact_id)
            return _conversation_ui_artifact(connection, row)

    def confirm_renovation_project(
        self,
        conversation_id: str,
        artifact_id: str,
        *,
        address: str,
        idempotency_key: str,
    ) -> dict[str, Any]:
        """按 artifact 中的可信草稿创建一次本地装修项目。"""
        _validate_conversation_id(conversation_id)
        _validate_uuid(artifact_id, "artifact_id")
        _validate_uuid(idempotency_key, "idempotency_key")
        now = _now()
        with self._connect() as connection:
            existing_receipt = connection.execute(
                """
                SELECT conversation_id, artifact_id, action_id,
                       arguments_json, response_json
                FROM generation_ui_actions
                WHERE idempotency_key = ?
                """,
                (idempotency_key,),
            ).fetchone()
            if existing_receipt is not None:
                if tuple(existing_receipt[:3]) != (
                    conversation_id,
                    artifact_id,
                    "confirm_create_project",
                ):
                    raise GenerationUiActionConflictError(
                        "idempotency_key 已被其他 Action 使用。"
                    )
                stored_arguments = _json_object(
                    existing_receipt[3], "arguments_json"
                )
                if stored_arguments != {"address": address}:
                    raise GenerationUiActionConflictError(
                        "同一 idempotency_key 不能提交不同参数。"
                    )
                return _json_object(existing_receipt[4], "response_json")

            row = connection.execute(
                """
                SELECT scenario, data_json
                FROM generation_ui_artifacts
                WHERE conversation_id = ? AND artifact_id = ?
                """,
                (conversation_id, artifact_id),
            ).fetchone()
            if row is None:
                raise GenerationUiArtifactNotFoundError(artifact_id)
            if row[0] != "renovation_project_create":
                raise GenerationUiActionConflictError(
                    "该 Artifact 不支持创建装修项目。"
                )
            data = _json_object(row[1], "data_json")
            city = data.get("city")
            area_m2 = data.get("area_m2")
            style = data.get("style")
            if not isinstance(city, str) or not isinstance(style, str) or not isinstance(
                area_m2, (int, float)
            ):
                raise GenerationUiActionConflictError("项目草稿数据不完整。")

            existing_project = connection.execute(
                """
                SELECT project_id, address
                FROM renovation_projects
                WHERE conversation_id = ? AND artifact_id = ?
                """,
                (conversation_id, artifact_id),
            ).fetchone()
            project_id = (
                str(existing_project[0]) if existing_project is not None else str(uuid4())
            )
            stored_address = (
                str(existing_project[1]) if existing_project is not None else address
            )
            if existing_project is not None and stored_address != address:
                raise GenerationUiActionConflictError(
                    "项目已经创建，不能通过重复 Action 修改地址。"
                )
            if existing_project is None:
                connection.execute(
                    """
                    INSERT INTO renovation_projects (
                        project_id, conversation_id, artifact_id, city,
                        area_m2, style, address, status, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        project_id,
                        conversation_id,
                        artifact_id,
                        city,
                        area_m2,
                        style,
                        stored_address,
                        "已创建",
                        now,
                    ),
                )
            data.update(
                {
                    "project_id": project_id,
                    "address": stored_address,
                    "status": "已创建",
                }
            )
            connection.execute(
                "UPDATE generation_ui_artifacts SET data_json = ? WHERE artifact_id = ?",
                (json.dumps(data, ensure_ascii=False, separators=(",", ":")), artifact_id),
            )
            response = {
                "success": True,
                "message": "装修项目已创建。",
                "status": "completed",
                "data_patch": {
                    "project_id": project_id,
                    "address": stored_address,
                    "status": "已创建",
                },
                "agent_message": f"装修项目已创建，项目编号为 {project_id}。",
            }
            connection.execute(
                """
                INSERT INTO generation_ui_actions (
                    idempotency_key, conversation_id, artifact_id,
                    action_id, arguments_json, response_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    idempotency_key,
                    conversation_id,
                    artifact_id,
                    "confirm_create_project",
                    json.dumps({"address": stored_address}, ensure_ascii=False),
                    json.dumps(response, ensure_ascii=False, separators=(",", ":")),
                    now,
                ),
            )
        return response

    def submit_generation_ui_input(
        self,
        conversation_id: str,
        artifact_id: str,
        *,
        arguments: dict[str, Any],
        idempotency_key: str,
    ) -> tuple[dict[str, Any], bool]:
        """保存一次安全表单提交，并标记当前请求是否取得续跑权。"""
        _validate_conversation_id(conversation_id)
        _validate_uuid(artifact_id, "artifact_id")
        _validate_uuid(idempotency_key, "idempotency_key")
        now = _now()
        with self._connect() as connection:
            existing_receipt = connection.execute(
                """
                SELECT conversation_id, artifact_id, action_id,
                       arguments_json, response_json
                FROM generation_ui_actions
                WHERE idempotency_key = ?
                """,
                (idempotency_key,),
            ).fetchone()
            if existing_receipt is not None:
                if tuple(existing_receipt[:3]) != (
                    conversation_id,
                    artifact_id,
                    "submit_structured_input",
                ):
                    raise GenerationUiActionConflictError(
                        "idempotency_key 已被其他 Action 使用。"
                    )
                if _json_object(existing_receipt[3], "arguments_json") != arguments:
                    raise GenerationUiActionConflictError(
                        "同一 idempotency_key 不能提交不同参数。"
                    )
                return _json_object(existing_receipt[4], "response_json"), False

            row = connection.execute(
                """
                SELECT scenario, spec_json, data_json
                FROM generation_ui_artifacts
                WHERE conversation_id = ? AND artifact_id = ?
                """,
                (conversation_id, artifact_id),
            ).fetchone()
            if row is None:
                raise GenerationUiArtifactNotFoundError(artifact_id)
            if row[0] != "structured_input":
                raise GenerationUiActionConflictError(
                    "该 Artifact 不支持结构化表单提交。"
                )
            spec = _json_object(row[1], "spec_json")
            data = _json_object(row[2], "data_json")
            previous_submission = data.get("submission")
            if previous_submission is not None and previous_submission != arguments:
                raise GenerationUiActionConflictError(
                    "表单已经提交，不能通过重复 Action 修改答案。"
                )
            if previous_submission == arguments:
                prior_action = connection.execute(
                    """
                    SELECT arguments_json, response_json
                    FROM generation_ui_actions
                    WHERE conversation_id = ? AND artifact_id = ?
                      AND action_id = 'submit_structured_input'
                    ORDER BY created_at DESC
                    LIMIT 1
                    """,
                    (conversation_id, artifact_id),
                ).fetchone()
                if (
                    prior_action is not None
                    and _json_object(prior_action[0], "arguments_json") == arguments
                ):
                    prior_response = _json_object(
                        prior_action[1], "response_json"
                    )
                    if prior_response.get("status") != "failed":
                        return prior_response, False

            fields = data.get("fields")
            if not isinstance(fields, list):
                raise GenerationUiActionConflictError("表单字段定义不完整。")
            data.update({"status": "已提交", "submission": arguments})
            form_defaults = spec.get("form_defaults")
            if not isinstance(form_defaults, dict):
                form_defaults = {}
            form_defaults.update(arguments)
            spec["form_defaults"] = form_defaults
            connection.execute(
                """
                UPDATE generation_ui_artifacts
                SET spec_json = ?, data_json = ?
                WHERE artifact_id = ?
                """,
                (
                    json.dumps(spec, ensure_ascii=False, separators=(",", ":")),
                    json.dumps(data, ensure_ascii=False, separators=(",", ":")),
                    artifact_id,
                ),
            )
            response = {
                "success": True,
                "message": "参数已提交，Agent 正在计算。",
                "status": "processing",
                "data_patch": {
                    "status": "已提交",
                    "submission": arguments,
                },
                "form_patch": arguments,
                "spec_patch": {},
                "agent_message": "已收到表单信息，正在继续处理。",
            }
            connection.execute(
                """
                INSERT INTO generation_ui_actions (
                    idempotency_key, conversation_id, artifact_id,
                    action_id, arguments_json, response_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    idempotency_key,
                    conversation_id,
                    artifact_id,
                    "submit_structured_input",
                    json.dumps(arguments, ensure_ascii=False, separators=(",", ":")),
                    json.dumps(response, ensure_ascii=False, separators=(",", ":")),
                    now,
                ),
            )
        return response, True

    def finish_generation_ui_input(
        self,
        conversation_id: str,
        artifact_id: str,
        *,
        idempotency_key: str,
        response: dict[str, Any],
    ) -> None:
        """把结构化输入 Action 的最终续跑结果写回同一幂等回执。"""
        _validate_conversation_id(conversation_id)
        _validate_uuid(artifact_id, "artifact_id")
        _validate_uuid(idempotency_key, "idempotency_key")
        with self._connect() as connection:
            updated = connection.execute(
                """
                UPDATE generation_ui_actions SET response_json = ?
                WHERE idempotency_key = ? AND conversation_id = ?
                  AND artifact_id = ? AND action_id = 'submit_structured_input'
                """,
                (
                    json.dumps(response, ensure_ascii=False, separators=(",", ":")),
                    idempotency_key,
                    conversation_id,
                    artifact_id,
                ),
            ).rowcount
            if updated != 1:
                raise GenerationUiActionConflictError(
                    "结构化表单 Action 回执不存在。"
                )

    def review_content_plan(
        self,
        conversation_id: str,
        artifact_id: str,
        *,
        arguments: dict[str, Any],
        idempotency_key: str,
    ) -> dict[str, Any]:
        """保存内容审核决定；确认内容时图片已经由 Web 层完成真实文件校验。"""

        _validate_conversation_id(conversation_id)
        _validate_uuid(artifact_id, "artifact_id")
        _validate_uuid(idempotency_key, "idempotency_key")
        content_id = arguments.get("content_id")
        decision = arguments.get("decision")
        note = arguments.get("note", "")
        image_paths = arguments.get("image_paths", [])
        if not isinstance(content_id, str) or decision not in {
            "确认内容", "要求修改", "放弃选题"
        }:
            raise GenerationUiActionConflictError("内容审核参数无效。")
        if not isinstance(note, str) or not isinstance(image_paths, list):
            raise GenerationUiActionConflictError("审核说明或图片参数无效。")
        if decision == "要求修改" and not note.strip():
            raise GenerationUiActionConflictError("要求修改时必须填写审核说明。")
        if decision == "确认内容" and not image_paths:
            raise GenerationUiActionConflictError("确认内容前必须提供有效图片。")

        now = _now()
        with self._connect() as connection:
            existing = _generation_action_receipt(
                connection,
                idempotency_key,
                conversation_id,
                artifact_id,
                "review_content_plan",
                arguments,
            )
            if existing is not None:
                return existing
            row = connection.execute(
                """
                SELECT scenario, data_json
                FROM generation_ui_artifacts
                WHERE conversation_id = ? AND artifact_id = ?
                """,
                (conversation_id, artifact_id),
            ).fetchone()
            if row is None:
                raise GenerationUiArtifactNotFoundError(artifact_id)
            if row[0] != "content_weekly_plan":
                raise GenerationUiActionConflictError("该卡片不是内容周计划。")
            data = _json_object(row[1], "data_json")
            contents = data.get("contents")
            if not isinstance(contents, list):
                raise GenerationUiActionConflictError("内容计划数据不完整。")
            item = next(
                (
                    candidate
                    for candidate in contents
                    if isinstance(candidate, dict)
                    and candidate.get("content_id") == content_id
                ),
                None,
            )
            if item is None:
                raise GenerationUiActionConflictError("内容编号不存在。")
            if item.get("rpa_status") in {"running", "draft_saved"}:
                raise GenerationUiActionConflictError(
                    "RPA 已开始或草稿已保存，不能再修改审核状态。"
                )
            next_status = {
                "确认内容": ("approved", "已审核"),
                "要求修改": ("revision_requested", "待修改"),
                "放弃选题": ("discarded", "已放弃"),
            }[decision]
            item.update(
                {
                    "status": next_status[0],
                    "status_label": next_status[1],
                    "review_note": note.strip(),
                    "image_paths": image_paths if decision == "确认内容" else [],
                    "approved_at": now if decision == "确认内容" else "",
                }
            )
            _refresh_content_metrics(data)
            connection.execute(
                "UPDATE generation_ui_artifacts SET data_json = ? WHERE artifact_id = ?",
                (json.dumps(data, ensure_ascii=False, separators=(",", ":")), artifact_id),
            )
            response = {
                "success": True,
                "message": f"内容 {content_id} 已记录审核结果：{decision}。",
                "status": next_status[0],
                "data_patch": {"contents": contents, "metrics": data["metrics"]},
                "form_patch": {
                    "content_id": content_id,
                    "decision": decision,
                    "note": note.strip(),
                    "image_paths_text": "\n".join(image_paths),
                },
                "spec_patch": {},
                "agent_message": f"内容 {content_id} 当前状态为{next_status[1]}。",
            }
            _insert_generation_action(
                connection,
                idempotency_key,
                conversation_id,
                artifact_id,
                "review_content_plan",
                arguments,
                response,
                now,
            )
        return response

    def prepare_content_xiaohongshu_rpa(
        self,
        conversation_id: str,
        artifact_id: str,
        *,
        arguments: dict[str, Any],
        idempotency_key: str,
    ) -> dict[str, Any]:
        """幂等领取一条已审核内容，并在执行 RPA 前写入运行中状态。"""

        _validate_conversation_id(conversation_id)
        _validate_uuid(artifact_id, "artifact_id")
        _validate_uuid(idempotency_key, "idempotency_key")
        content_id = arguments.get("content_id")
        if not isinstance(content_id, str):
            raise GenerationUiActionConflictError("内容编号无效。")
        with self._connect() as connection:
            existing = _generation_action_receipt(
                connection,
                idempotency_key,
                conversation_id,
                artifact_id,
                "save_xiaohongshu_draft",
                arguments,
            )
            if existing is not None:
                return {"existing_response": existing}
            row = connection.execute(
                """
                SELECT scenario, data_json
                FROM generation_ui_artifacts
                WHERE conversation_id = ? AND artifact_id = ?
                """,
                (conversation_id, artifact_id),
            ).fetchone()
            if row is None:
                raise GenerationUiArtifactNotFoundError(artifact_id)
            if row[0] != "content_weekly_plan":
                raise GenerationUiActionConflictError("该卡片不是内容周计划。")
            data = _json_object(row[1], "data_json")
            contents = data.get("contents")
            if not isinstance(contents, list):
                raise GenerationUiActionConflictError("内容计划数据不完整。")
            item = next(
                (
                    candidate
                    for candidate in contents
                    if isinstance(candidate, dict)
                    and candidate.get("content_id") == content_id
                ),
                None,
            )
            if item is None:
                raise GenerationUiActionConflictError("内容编号不存在。")
            if item.get("status") != "approved":
                raise GenerationUiActionConflictError("只有已审核内容才能保存到小红书草稿箱。")
            if item.get("rpa_status") == "running":
                raise GenerationUiActionConflictError("该内容的 RPA 正在执行。")
            if item.get("rpa_status") == "draft_saved":
                raise GenerationUiActionConflictError("该内容已经保存到小红书草稿箱。")
            image_paths = item.get("image_paths")
            if not isinstance(image_paths, list) or not image_paths:
                raise GenerationUiActionConflictError("已审核内容缺少图片。")
            item.update(
                {
                    "rpa_status": "running",
                    "rpa_status_label": "执行中",
                    "rpa_job_id": "",
                    "rpa_message": "正在启动小红书专用 Chrome。",
                }
            )
            connection.execute(
                "UPDATE generation_ui_artifacts SET data_json = ? WHERE artifact_id = ?",
                (json.dumps(data, ensure_ascii=False, separators=(",", ":")), artifact_id),
            )
            return {
                "content_id": content_id,
                "title": str(item.get("title", "")),
                "body": str(item.get("body", "")),
                "tags": list(item.get("tags", [])),
                "image_paths": list(image_paths),
            }

    def finish_content_xiaohongshu_rpa(
        self,
        conversation_id: str,
        artifact_id: str,
        *,
        arguments: dict[str, Any],
        idempotency_key: str,
        rpa_result: dict[str, Any],
    ) -> dict[str, Any]:
        """把小红书草稿 RPA 的终态写回卡片和 Action 审计。"""

        content_id = arguments["content_id"]
        now = _now()
        with self._connect() as connection:
            existing = _generation_action_receipt(
                connection,
                idempotency_key,
                conversation_id,
                artifact_id,
                "save_xiaohongshu_draft",
                arguments,
            )
            if existing is not None:
                return existing
            row = connection.execute(
                """
                SELECT data_json FROM generation_ui_artifacts
                WHERE conversation_id = ? AND artifact_id = ?
                """,
                (conversation_id, artifact_id),
            ).fetchone()
            if row is None:
                raise GenerationUiArtifactNotFoundError(artifact_id)
            data = _json_object(row[0], "data_json")
            contents = data.get("contents", [])
            item = next(
                (
                    candidate
                    for candidate in contents
                    if isinstance(candidate, dict)
                    and candidate.get("content_id") == content_id
                ),
                None,
            )
            if item is None:
                raise GenerationUiActionConflictError("内容编号不存在。")
            if rpa_result.get("success") is True:
                rpa_status, rpa_label = "draft_saved", "草稿已保存"
            elif rpa_result.get("needs_user_action") is True:
                rpa_status, rpa_label = "needs_user_action", "需要人工处理"
            else:
                rpa_status, rpa_label = "failed", "执行失败"
            item.update(
                {
                    "rpa_status": rpa_status,
                    "rpa_status_label": rpa_label,
                    "rpa_job_id": str(rpa_result.get("job_id") or ""),
                    "rpa_message": str(rpa_result.get("message") or ""),
                }
            )
            _refresh_content_metrics(data)
            connection.execute(
                "UPDATE generation_ui_artifacts SET data_json = ? WHERE artifact_id = ?",
                (json.dumps(data, ensure_ascii=False, separators=(",", ":")), artifact_id),
            )
            response = {
                "success": True,
                "message": item["rpa_message"] or f"RPA 状态：{rpa_label}。",
                "status": rpa_status,
                "data_patch": {"contents": contents, "metrics": data["metrics"]},
                "form_patch": {"content_id": content_id},
                "spec_patch": {},
                "agent_message": (
                    f"内容 {content_id} 的小红书草稿任务状态为{rpa_label}；"
                    "没有执行最终发布。"
                ),
            }
            _insert_generation_action(
                connection,
                idempotency_key,
                conversation_id,
                artifact_id,
                "save_xiaohongshu_draft",
                arguments,
                response,
                now,
            )
        return response

    def transition_owner_briefing_task(
        self,
        conversation_id: str,
        artifact_id: str,
        *,
        arguments: dict[str, Any],
        idempotency_key: str,
    ) -> dict[str, Any]:
        """更新老板早报任务状态，并把动作写入本地幂等审计记录。"""

        _validate_conversation_id(conversation_id)
        _validate_uuid(artifact_id, "artifact_id")
        _validate_uuid(idempotency_key, "idempotency_key")
        anomaly_key = arguments.get("anomaly_key")
        action = arguments.get("action")
        assignee = arguments.get("assignee", "")
        note = arguments.get("note", "")
        if not isinstance(anomaly_key, str) or action not in {"分派", "完成", "升级"}:
            raise GenerationUiActionConflictError("老板早报任务参数无效。")
        if not isinstance(assignee, str) or not isinstance(note, str):
            raise GenerationUiActionConflictError("承接人和处理说明必须是文本。")

        now = _now()
        with self._connect() as connection:
            existing_receipt = connection.execute(
                """
                SELECT conversation_id, artifact_id, action_id,
                       arguments_json, response_json
                FROM generation_ui_actions
                WHERE idempotency_key = ?
                """,
                (idempotency_key,),
            ).fetchone()
            if existing_receipt is not None:
                if tuple(existing_receipt[:3]) != (
                    conversation_id,
                    artifact_id,
                    "transition_owner_briefing_task",
                ):
                    raise GenerationUiActionConflictError(
                        "idempotency_key 已被其他 Action 使用。"
                    )
                if _json_object(existing_receipt[3], "arguments_json") != arguments:
                    raise GenerationUiActionConflictError(
                        "同一 idempotency_key 不能提交不同参数。"
                    )
                return _json_object(existing_receipt[4], "response_json")

            row = connection.execute(
                """
                SELECT scenario, spec_json, data_json
                FROM generation_ui_artifacts
                WHERE conversation_id = ? AND artifact_id = ?
                """,
                (conversation_id, artifact_id),
            ).fetchone()
            if row is None:
                raise GenerationUiArtifactNotFoundError(artifact_id)
            if row[0] != "owner_daily_briefing":
                raise GenerationUiActionConflictError(
                    "该 Artifact 不支持老板早报任务处理。"
                )
            spec = _json_object(row[1], "spec_json")
            data = _json_object(row[2], "data_json")
            anomalies = data.get("anomalies")
            if not isinstance(anomalies, list):
                raise GenerationUiActionConflictError("老板早报异常数据不完整。")
            for item in anomalies:
                if isinstance(item, dict):
                    item["severity_label"] = _owner_severity_label(
                        item.get("severity")
                    )
            anomaly = next(
                (
                    item
                    for item in anomalies
                    if isinstance(item, dict) and item.get("anomaly_key") == anomaly_key
                ),
                None,
            )
            if anomaly is None:
                raise GenerationUiActionConflictError("老板早报中不存在该异常。")
            task_id = anomaly.get("task_id")
            if not isinstance(task_id, str):
                raise GenerationUiActionConflictError("老板早报任务编号无效。")
            task_row = connection.execute(
                """
                SELECT status, assignee, note, assigned_at, due_at,
                       next_reminder_at, last_reminded_at, reminder_count,
                       escalation_pending
                FROM owner_briefing_tasks
                WHERE task_id = ?
                """,
                (task_id,),
            ).fetchone()
            if task_row is None:
                raise GenerationUiActionConflictError("老板早报任务不存在。")
            current_status = str(task_row[0])
            stored_assignee = str(task_row[1] or "")
            stored_note = str(task_row[2] or "")
            assigned_at = str(task_row[3] or "")
            due_at = str(task_row[4] or "")
            next_reminder_at = str(task_row[5] or "")
            last_reminded_at = str(task_row[6] or "")
            reminder_count = int(task_row[7] or 0)
            escalation_pending = bool(task_row[8])
            if action == "分派":
                if current_status != "detected":
                    raise GenerationUiActionConflictError("只有待处理任务可以分派。")
                if not assignee.strip():
                    raise GenerationUiActionConflictError("分派任务必须填写承接人。")
                next_status = "assigned"
                next_assignee = assignee.strip()
                next_note = note.strip()
                assigned_at = now
                due_at = _after_hours(now, OWNER_ESCALATION_AFTER_HOURS)
                next_reminder_at = _after_hours(now, OWNER_REMINDER_INTERVAL_HOURS)
                last_reminded_at = ""
                reminder_count = 0
                escalation_pending = False
            elif action == "完成":
                if current_status != "assigned":
                    raise GenerationUiActionConflictError("只有已分派任务可以完成。")
                next_status = "completed"
                next_assignee = stored_assignee
                next_note = note.strip() or stored_note
                next_reminder_at = ""
                escalation_pending = False
            else:
                if current_status != "assigned":
                    raise GenerationUiActionConflictError("只有已分派任务可以升级。")
                if not note.strip():
                    raise GenerationUiActionConflictError("升级任务必须填写原因。")
                next_status = "escalated"
                next_assignee = stored_assignee
                next_note = note.strip()
                next_reminder_at = ""
                escalation_pending = False

            connection.execute(
                """
                UPDATE owner_briefing_tasks
                SET status = ?, assignee = ?, note = ?, assigned_at = ?,
                    due_at = ?, next_reminder_at = ?, last_reminded_at = ?,
                    reminder_count = ?, escalation_pending = ?, updated_at = ?
                WHERE task_id = ?
                """,
                (
                    next_status,
                    next_assignee,
                    next_note,
                    assigned_at or None,
                    due_at or None,
                    next_reminder_at or None,
                    last_reminded_at or None,
                    reminder_count,
                    int(escalation_pending),
                    now,
                    task_id,
                ),
            )
            anomaly.update(
                {
                    "status": next_status,
                    "assignee": next_assignee,
                    "note": next_note,
                    "assigned_at": assigned_at,
                    "due_at": due_at,
                    "next_reminder_at": next_reminder_at,
                    "last_reminded_at": last_reminded_at,
                    "reminder_count": reminder_count,
                    "escalation_pending": escalation_pending,
                }
            )
            anomaly.update(
                _owner_display_fields(
                    next_status,
                    due_at=due_at,
                    escalation_pending=escalation_pending,
                )
            )
            spec = _upgrade_owner_briefing_spec(spec, data)
            connection.execute(
                """
                UPDATE generation_ui_artifacts
                SET spec_json = ?, data_json = ?
                WHERE artifact_id = ?
                """,
                (
                    json.dumps(spec, ensure_ascii=False, separators=(",", ":")),
                    json.dumps(data, ensure_ascii=False, separators=(",", ":")),
                    artifact_id,
                ),
            )
            defaults = spec.get("form_defaults", {})
            next_form_key = (
                anomaly_key
                if next_status == "assigned"
                else str(defaults.get("anomaly_key") or "")
            )
            next_form_action = (
                "完成"
                if next_status == "assigned"
                else str(defaults.get("action") or "")
            )
            next_form_assignee = next_assignee if next_status == "assigned" else ""
            next_form_note = next_note if next_status == "assigned" else ""
            response = {
                "success": True,
                "message": f"任务已{action}。",
                "status": next_status,
                "data_patch": {"anomalies": anomalies},
                "form_patch": {
                    "anomaly_key": next_form_key,
                    "action": next_form_action,
                    "assignee": next_form_assignee,
                    "note": next_form_note,
                },
                "spec_patch": {
                    "root": spec["root"],
                    "interaction_mode": spec["interaction_mode"],
                    "actions": spec["actions"],
                    "form_defaults": spec["form_defaults"],
                },
                "agent_message": (
                    f"异常 {anomaly_key} 已{action}，当前状态为 {next_status}。"
                ),
            }
            connection.execute(
                """
                INSERT INTO generation_ui_actions (
                    idempotency_key, conversation_id, artifact_id,
                    action_id, arguments_json, response_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    idempotency_key,
                    conversation_id,
                    artifact_id,
                    "transition_owner_briefing_task",
                    json.dumps(arguments, ensure_ascii=False, separators=(",", ":")),
                    json.dumps(response, ensure_ascii=False, separators=(",", ":")),
                    now,
                ),
            )
        return response

    def scan_owner_briefing_reminders(
        self, *, now: str | datetime | None = None
    ) -> dict[str, Any]:
        """扫描已分派任务，生成本地提醒或待升级确认事件。"""

        scan_at = _utc_datetime(now)
        scan_at_text = scan_at.isoformat()
        events: list[dict[str, Any]] = []
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT task_id, anomaly_key, title, assignee, due_at,
                       next_reminder_at, reminder_count, escalation_pending
                FROM owner_briefing_tasks
                WHERE status = 'assigned'
                """
            ).fetchall()
            for row in rows:
                task_id = str(row[0])
                due_at = _optional_utc_datetime(row[4])
                reminder_at = _optional_utc_datetime(row[5])
                if due_at is not None and due_at <= scan_at and not bool(row[7]):
                    event = {
                        "reminder_id": str(uuid4()),
                        "task_id": task_id,
                        "anomaly_key": str(row[1]),
                        "reminder_type": "escalation_due",
                        "scheduled_for": due_at.isoformat(),
                        "message": (
                            f"任务已超过 24 小时：{row[2]}。请老板确认是否升级，"
                            "系统不会自动执行升级。"
                        ),
                        "created_at": scan_at_text,
                    }
                    inserted = connection.execute(
                        """
                        INSERT OR IGNORE INTO owner_briefing_reminders (
                            reminder_id, task_id, reminder_type, scheduled_for,
                            message, created_at
                        ) VALUES (?, ?, ?, ?, ?, ?)
                        """,
                        (
                            event["reminder_id"],
                            task_id,
                            event["reminder_type"],
                            event["scheduled_for"],
                            event["message"],
                            scan_at_text,
                        ),
                    ).rowcount
                    connection.execute(
                        """
                        UPDATE owner_briefing_tasks
                        SET escalation_pending = 1, next_reminder_at = NULL,
                            updated_at = ?
                        WHERE task_id = ?
                        """,
                        (scan_at_text, task_id),
                    )
                    if inserted:
                        events.append(event)
                    continue
                if reminder_at is None or reminder_at > scan_at:
                    continue
                event = {
                    "reminder_id": str(uuid4()),
                    "task_id": task_id,
                    "anomaly_key": str(row[1]),
                    "reminder_type": "reminder",
                    "scheduled_for": reminder_at.isoformat(),
                    "message": f"任务待回写：{row[2]}（承接人：{row[3]}）。",
                    "created_at": scan_at_text,
                }
                inserted = connection.execute(
                    """
                    INSERT OR IGNORE INTO owner_briefing_reminders (
                        reminder_id, task_id, reminder_type, scheduled_for,
                        message, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        event["reminder_id"],
                        task_id,
                        event["reminder_type"],
                        event["scheduled_for"],
                        event["message"],
                        scan_at_text,
                    ),
                ).rowcount
                if inserted:
                    connection.execute(
                        """
                        UPDATE owner_briefing_tasks
                        SET last_reminded_at = ?, reminder_count = reminder_count + 1,
                            next_reminder_at = ?, updated_at = ?
                        WHERE task_id = ?
                        """,
                        (
                            scan_at_text,
                            (scan_at + timedelta(hours=OWNER_REMINDER_INTERVAL_HOURS)).isoformat(),
                            scan_at_text,
                            task_id,
                        ),
                    )
                    events.append(event)
        return {
            "success": True,
            "scanned_at": scan_at_text,
            "created_count": len(events),
            "reminder_count": sum(
                item["reminder_type"] == "reminder" for item in events
            ),
            "escalation_due_count": sum(
                item["reminder_type"] == "escalation_due" for item in events
            ),
            "events": events,
        }

    def owner_briefing_reminders(self, *, limit: int = 50) -> list[dict[str, Any]]:
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 100:
            raise ValueError("Owner reminder limit must be between 1 and 100")
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT r.reminder_id, r.task_id, r.reminder_type,
                       r.scheduled_for, r.message, r.created_at,
                       t.anomaly_key, t.title, t.assignee, t.status,
                       t.escalation_pending
                FROM owner_briefing_reminders AS r
                JOIN owner_briefing_tasks AS t ON t.task_id = r.task_id
                ORDER BY r.created_at DESC, r.reminder_id DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
        return [
            {
                "reminder_id": str(row[0]),
                "task_id": str(row[1]),
                "reminder_type": str(row[2]),
                "scheduled_for": str(row[3]),
                "message": str(row[4]),
                "created_at": str(row[5]),
                "anomaly_key": str(row[6]),
                "title": str(row[7]),
                "assignee": str(row[8]),
                "status": str(row[9]),
                "escalation_pending": bool(row[10]),
            }
            for row in rows
        ]

    def owner_next_day_review(
        self, report_date: str, *, current_issue_keys: Sequence[str] = ()
    ) -> dict[str, Any]:
        """汇总昨日闭环结果，并识别今日再次出现的同一业务问题。"""

        with self._connect() as connection:
            return _owner_next_day_review(
                connection,
                report_date,
                current_issue_keys=current_issue_keys,
            )

    def reset_owner_briefing_demo(self) -> dict[str, int | bool]:
        """清空老板早报演示对话、任务、提醒和 Action 审计。"""

        with self._connect() as connection:
            conversation_ids = [
                str(row[0])
                for row in connection.execute(
                    """
                    SELECT DISTINCT conversation_id
                    FROM generation_ui_artifacts
                    WHERE scenario = 'owner_daily_briefing'
                    """
                ).fetchall()
            ]
            task_count = int(
                connection.execute(
                    "SELECT COUNT(*) FROM owner_briefing_tasks"
                ).fetchone()[0]
            )
            reminder_count = int(
                connection.execute(
                    "SELECT COUNT(*) FROM owner_briefing_reminders"
                ).fetchone()[0]
            )
            action_count = int(
                connection.execute(
                    """
                    SELECT COUNT(*) FROM generation_ui_actions
                    WHERE action_id = 'transition_owner_briefing_task'
                    """
                ).fetchone()[0]
            )
            connection.execute("DELETE FROM owner_briefing_reminders")
            connection.execute("DELETE FROM owner_briefing_tasks")
            connection.execute(
                """
                DELETE FROM generation_ui_actions
                WHERE action_id = 'transition_owner_briefing_task'
                """
            )
            if conversation_ids:
                placeholders = ",".join("?" for _ in conversation_ids)
                connection.execute(
                    f"DELETE FROM conversations WHERE conversation_id IN ({placeholders})",
                    conversation_ids,
                )
        return {
            "success": True,
            "deleted_conversation_count": len(conversation_ids),
            "deleted_task_count": task_count,
            "deleted_reminder_count": reminder_count,
            "deleted_action_count": action_count,
        }

    def create_attachment(
        self,
        conversation_id: str,
        *,
        knowledge_base_kind: str,
        knowledge_base_id: str,
        filename: str,
        content_type: str,
        size_bytes: int,
    ) -> ConversationAttachment:
        _validate_conversation_id(conversation_id)
        if knowledge_base_kind not in {"pdf", "office", "image", "media"}:
            raise ValueError("Unsupported attachment knowledge base kind")
        UUID(knowledge_base_id)
        attachment_id = str(uuid4())
        now = _now()
        with self._connect() as connection:
            exists = connection.execute(
                "SELECT 1 FROM conversations WHERE conversation_id = ?",
                (conversation_id,),
            ).fetchone()
            if exists is None:
                raise ConversationNotFoundError(conversation_id)
            connection.execute(
                """
                INSERT INTO attachments (
                    attachment_id, conversation_id, knowledge_base_kind,
                    knowledge_base_id, filename, content_type, size_bytes,
                    status, progress, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, 'UPLOADING', 0, ?, ?)
                """,
                (
                    attachment_id,
                    conversation_id,
                    knowledge_base_kind,
                    knowledge_base_id,
                    filename,
                    content_type,
                    size_bytes,
                    now,
                    now,
                ),
            )
        return self.attachment(conversation_id, attachment_id)

    def mark_attachment_uploaded(
        self,
        conversation_id: str,
        attachment_id: str,
        *,
        document_id: str,
        job_id: str,
    ) -> ConversationAttachment:
        _validate_attachment_ids(conversation_id, attachment_id)
        UUID(document_id)
        UUID(job_id)
        now = _now()
        with self._connect() as connection:
            updated = connection.execute(
                """
                UPDATE attachments
                SET document_id = ?, job_id = ?, status = 'PROCESSING',
                    progress = 0, error_message = NULL, updated_at = ?
                WHERE conversation_id = ? AND attachment_id = ?
                """,
                (document_id, job_id, now, conversation_id, attachment_id),
            ).rowcount
            if updated != 1:
                raise AttachmentNotFoundError(attachment_id)
        return self.attachment(conversation_id, attachment_id)

    def update_attachment_status(
        self,
        conversation_id: str,
        attachment_id: str,
        *,
        status: str,
        progress: float,
        error_message: str | None = None,
    ) -> ConversationAttachment:
        _validate_attachment_ids(conversation_id, attachment_id)
        if status not in {"UPLOADING", "PROCESSING", "READY", "FAILED"}:
            raise ValueError("Unsupported attachment status")
        if isinstance(progress, bool) or not 0 <= progress <= 1:
            raise ValueError("Attachment progress must be between 0 and 1")
        with self._connect() as connection:
            updated = connection.execute(
                """
                UPDATE attachments
                SET status = ?, progress = ?, error_message = ?, updated_at = ?
                WHERE conversation_id = ? AND attachment_id = ?
                """,
                (
                    status,
                    progress,
                    error_message,
                    _now(),
                    conversation_id,
                    attachment_id,
                ),
            ).rowcount
            if updated != 1:
                raise AttachmentNotFoundError(attachment_id)
        return self.attachment(conversation_id, attachment_id)

    def attachment(
        self, conversation_id: str, attachment_id: str
    ) -> ConversationAttachment:
        _validate_attachment_ids(conversation_id, attachment_id)
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT attachment_id, conversation_id, knowledge_base_kind,
                       knowledge_base_id, document_id, job_id, filename,
                       content_type, size_bytes, status, progress, error_message,
                       created_at, updated_at
                FROM attachments
                WHERE conversation_id = ? AND attachment_id = ?
                """,
                (conversation_id, attachment_id),
            ).fetchone()
        if row is None:
            raise AttachmentNotFoundError(attachment_id)
        return ConversationAttachment(*row)

    def attachments(self, conversation_id: str) -> list[ConversationAttachment]:
        _validate_conversation_id(conversation_id)
        with self._connect() as connection:
            exists = connection.execute(
                "SELECT 1 FROM conversations WHERE conversation_id = ?",
                (conversation_id,),
            ).fetchone()
            if exists is None:
                raise ConversationNotFoundError(conversation_id)
            rows = connection.execute(
                """
                SELECT attachment_id, conversation_id, knowledge_base_kind,
                       knowledge_base_id, document_id, job_id, filename,
                       content_type, size_bytes, status, progress, error_message,
                       created_at, updated_at
                FROM attachments
                WHERE conversation_id = ?
                ORDER BY created_at ASC
                """,
                (conversation_id,),
            ).fetchall()
        return [ConversationAttachment(*row) for row in rows]

    def document_reference_count(self, document_id: str) -> int:
        UUID(document_id)
        with self._connect() as connection:
            row = connection.execute(
                "SELECT COUNT(*) FROM attachments WHERE document_id = ?",
                (document_id,),
            ).fetchone()
        return int(row[0]) if row is not None else 0

    def remove_attachment(self, conversation_id: str, attachment_id: str) -> None:
        _validate_attachment_ids(conversation_id, attachment_id)
        with self._connect() as connection:
            deleted = connection.execute(
                """
                DELETE FROM attachments
                WHERE conversation_id = ? AND attachment_id = ?
                """,
                (conversation_id, attachment_id),
            ).rowcount
            if deleted != 1:
                raise AttachmentNotFoundError(attachment_id)

    def delete(self, conversation_id: str) -> None:
        _validate_conversation_id(conversation_id)
        with self._connect() as connection:
            deleted = connection.execute(
                "DELETE FROM conversations WHERE conversation_id = ?",
                (conversation_id,),
            ).rowcount
            if deleted != 1:
                raise ConversationNotFoundError(conversation_id)

    def _initialize(self) -> None:
        with self._connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS conversations (
                    conversation_id TEXT PRIMARY KEY,
                    custom_title TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS messages (
                    message_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    conversation_id TEXT NOT NULL REFERENCES conversations(conversation_id)
                        ON DELETE CASCADE,
                    role TEXT NOT NULL CHECK (role IN ('user', 'assistant')),
                    content TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'completed' CHECK (
                        status IN ('pending', 'completed', 'stopped', 'failed')
                    ),
                    turn_id TEXT,
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_messages_conversation
                    ON messages(conversation_id, message_id);
                CREATE TABLE IF NOT EXISTS message_execution_details (
                    message_id INTEGER PRIMARY KEY REFERENCES messages(message_id)
                        ON DELETE CASCADE,
                    trace_json TEXT NOT NULL,
                    duration_ms INTEGER CHECK (duration_ms >= 0),
                    context_json TEXT
                );
                CREATE TABLE IF NOT EXISTS generation_ui_artifacts (
                    artifact_id TEXT PRIMARY KEY,
                    conversation_id TEXT NOT NULL REFERENCES conversations(conversation_id)
                        ON DELETE CASCADE,
                    message_id INTEGER NOT NULL REFERENCES messages(message_id)
                        ON DELETE CASCADE,
                    scenario TEXT NOT NULL,
                    text TEXT NOT NULL,
                    spec_json TEXT NOT NULL,
                    data_json TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_generation_ui_conversation
                    ON generation_ui_artifacts(conversation_id, message_id);
                CREATE TABLE IF NOT EXISTS conversation_resources (
                    resource_id TEXT PRIMARY KEY,
                    conversation_id TEXT NOT NULL REFERENCES conversations(conversation_id)
                        ON DELETE CASCADE,
                    message_id INTEGER NOT NULL REFERENCES messages(message_id)
                        ON DELETE CASCADE,
                    kind TEXT NOT NULL CHECK (kind IN ('artifact', 'source', 'evidence')),
                    title TEXT NOT NULL,
                    subtitle TEXT,
                    url TEXT,
                    path TEXT,
                    media_type TEXT,
                    created_at TEXT NOT NULL,
                    CHECK (url IS NOT NULL OR path IS NOT NULL OR kind = 'evidence')
                );
                CREATE INDEX IF NOT EXISTS idx_conversation_resources_message
                    ON conversation_resources(conversation_id, message_id);
                CREATE TABLE IF NOT EXISTS renovation_projects (
                    project_id TEXT PRIMARY KEY,
                    conversation_id TEXT NOT NULL REFERENCES conversations(conversation_id)
                        ON DELETE CASCADE,
                    artifact_id TEXT NOT NULL UNIQUE REFERENCES generation_ui_artifacts(artifact_id)
                        ON DELETE CASCADE,
                    city TEXT NOT NULL,
                    area_m2 REAL NOT NULL CHECK (area_m2 > 0),
                    style TEXT NOT NULL,
                    address TEXT NOT NULL,
                    status TEXT NOT NULL CHECK (status = '已创建'),
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS generation_ui_actions (
                    idempotency_key TEXT PRIMARY KEY,
                    conversation_id TEXT NOT NULL REFERENCES conversations(conversation_id)
                        ON DELETE CASCADE,
                    artifact_id TEXT NOT NULL REFERENCES generation_ui_artifacts(artifact_id)
                        ON DELETE CASCADE,
                    action_id TEXT NOT NULL,
                    arguments_json TEXT NOT NULL,
                    response_json TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_generation_ui_actions_artifact
                    ON generation_ui_actions(conversation_id, artifact_id);
                CREATE TABLE IF NOT EXISTS owner_briefing_tasks (
                    task_id TEXT PRIMARY KEY,
                    anomaly_key TEXT NOT NULL,
                    issue_key TEXT NOT NULL,
                    report_date TEXT NOT NULL,
                    title TEXT NOT NULL,
                    owner TEXT NOT NULL,
                    assignee TEXT NOT NULL DEFAULT '',
                    status TEXT NOT NULL CHECK (
                        status IN ('detected', 'assigned', 'completed', 'escalated')
                    ),
                    note TEXT NOT NULL DEFAULT '',
                    source_artifact_id TEXT NOT NULL,
                    assigned_at TEXT,
                    due_at TEXT,
                    next_reminder_at TEXT,
                    last_reminded_at TEXT,
                    reminder_count INTEGER NOT NULL DEFAULT 0,
                    escalation_pending INTEGER NOT NULL DEFAULT 0 CHECK (
                        escalation_pending IN (0, 1)
                    ),
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_owner_briefing_tasks_report
                    ON owner_briefing_tasks(report_date, status);
                CREATE TABLE IF NOT EXISTS owner_briefing_reminders (
                    reminder_id TEXT PRIMARY KEY,
                    task_id TEXT NOT NULL,
                    reminder_type TEXT NOT NULL CHECK (
                        reminder_type IN ('reminder', 'escalation_due')
                    ),
                    scheduled_for TEXT NOT NULL,
                    message TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    UNIQUE(task_id, reminder_type, scheduled_for)
                );
                CREATE INDEX IF NOT EXISTS idx_owner_briefing_reminders_created
                    ON owner_briefing_reminders(created_at);
                CREATE TABLE IF NOT EXISTS attachments (
                    attachment_id TEXT PRIMARY KEY,
                    conversation_id TEXT NOT NULL REFERENCES conversations(conversation_id)
                        ON DELETE CASCADE,
                    knowledge_base_kind TEXT,
                    knowledge_base_id TEXT,
                    document_id TEXT,
                    job_id TEXT,
                    filename TEXT NOT NULL,
                    content_type TEXT NOT NULL,
                    size_bytes INTEGER NOT NULL CHECK (size_bytes >= 0),
                    status TEXT NOT NULL CHECK (
                        status IN ('UPLOADING', 'PROCESSING', 'READY', 'FAILED')
                    ),
                    progress REAL NOT NULL CHECK (progress >= 0 AND progress <= 1),
                    error_message TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_attachments_conversation
                    ON attachments(conversation_id, created_at);
                CREATE INDEX IF NOT EXISTS idx_attachments_document
                    ON attachments(document_id);
                """
            )
            conversation_columns = {
                str(row[1])
                for row in connection.execute("PRAGMA table_info(conversations)")
            }
            if "custom_title" not in conversation_columns:
                connection.execute(
                    "ALTER TABLE conversations ADD COLUMN custom_title TEXT"
                )
            message_columns = {
                str(row[1])
                for row in connection.execute("PRAGMA table_info(messages)")
            }
            if "status" not in message_columns:
                connection.execute(
                    "ALTER TABLE messages ADD COLUMN status TEXT NOT NULL DEFAULT 'completed'"
                )
            if "turn_id" not in message_columns:
                connection.execute("ALTER TABLE messages ADD COLUMN turn_id TEXT")
            columns = {
                str(row[1])
                for row in connection.execute("PRAGMA table_info(attachments)")
            }
            migrated_attachment_route = False
            if "knowledge_base_kind" not in columns:
                connection.execute(
                    "ALTER TABLE attachments ADD COLUMN knowledge_base_kind TEXT"
                )
                migrated_attachment_route = True
            if "knowledge_base_id" not in columns:
                connection.execute(
                    "ALTER TABLE attachments ADD COLUMN knowledge_base_id TEXT"
                )
                migrated_attachment_route = True
            if migrated_attachment_route:
                connection.execute(
                    """
                    UPDATE attachments
                    SET status = 'FAILED', progress = 0,
                        error_message = 'ATTACHMENT_ROUTE_MIGRATION_REQUIRED',
                        updated_at = ?
                    WHERE knowledge_base_kind IS NULL OR knowledge_base_id IS NULL
                    """,
                    (_now(),),
                )
            owner_columns = {
                str(row[1])
                for row in connection.execute("PRAGMA table_info(owner_briefing_tasks)")
            }
            owner_migrations = {
                "issue_key": "TEXT NOT NULL DEFAULT ''",
                "assigned_at": "TEXT",
                "due_at": "TEXT",
                "next_reminder_at": "TEXT",
                "last_reminded_at": "TEXT",
                "reminder_count": "INTEGER NOT NULL DEFAULT 0",
                "escalation_pending": "INTEGER NOT NULL DEFAULT 0",
            }
            for column, definition in owner_migrations.items():
                if column not in owner_columns:
                    connection.execute(
                        f"ALTER TABLE owner_briefing_tasks ADD COLUMN {column} {definition}"
                    )
            connection.execute(
                "UPDATE owner_briefing_tasks SET issue_key = anomaly_key WHERE issue_key = ''"
            )
            legacy_assigned = connection.execute(
                """
                SELECT task_id, updated_at
                FROM owner_briefing_tasks
                WHERE status = 'assigned'
                  AND escalation_pending = 0
                  AND (
                      assigned_at IS NULL OR due_at IS NULL
                      OR (next_reminder_at IS NULL AND last_reminded_at IS NULL)
                  )
                """
            ).fetchall()
            for task_id, updated_at in legacy_assigned:
                connection.execute(
                    """
                    UPDATE owner_briefing_tasks
                    SET assigned_at = ?, due_at = ?, next_reminder_at = ?
                    WHERE task_id = ?
                    """,
                    (
                        updated_at,
                        _after_hours(str(updated_at), OWNER_ESCALATION_AFTER_HOURS),
                        _after_hours(str(updated_at), OWNER_REMINDER_INTERVAL_HOURS),
                        task_id,
                    ),
                )
            connection.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_owner_briefing_tasks_reminder
                ON owner_briefing_tasks(status, next_reminder_at, due_at)
                """
            )

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=5)
        connection.execute("PRAGMA foreign_keys = ON")
        return connection


def _insert_assistant_response(
    connection: sqlite3.Connection,
    conversation_id: str,
    assistant: str,
    *,
    turn_id: str | None,
    serialized_artifacts: list[dict[str, Any]],
    serialized_resources: list[dict[str, str | None]],
    serialized_trace: list[str],
    duration_ms: int | None,
    context: dict[str, Any] | None,
    now: str,
) -> int:
    updated = connection.execute(
        "UPDATE conversations SET updated_at = ? WHERE conversation_id = ?",
        (now, conversation_id),
    ).rowcount
    if updated != 1:
        raise ConversationNotFoundError(conversation_id)
    cursor = connection.execute(
        """
        INSERT INTO messages (
            conversation_id, role, content, status, turn_id, created_at
        ) VALUES (?, 'assistant', ?, 'completed', ?, ?)
        """,
        (conversation_id, assistant, turn_id, now),
    )
    assistant_message_id = int(cursor.lastrowid)
    if serialized_trace or duration_ms is not None or context is not None:
        connection.execute(
            """
            INSERT INTO message_execution_details (
                message_id, trace_json, duration_ms, context_json
            ) VALUES (?, ?, ?, ?)
            """,
            (
                assistant_message_id,
                json.dumps(
                    serialized_trace,
                    ensure_ascii=False,
                    separators=(",", ":"),
                ),
                duration_ms,
                json.dumps(
                    context,
                    ensure_ascii=False,
                    separators=(",", ":"),
                )
                if context is not None
                else None,
            ),
        )
    synchronized_artifacts = [
        _synchronize_owner_briefing_artifact(
            connection,
            conversation_id,
            item,
            now,
        )
        for item in serialized_artifacts
    ]
    connection.executemany(
        """
        INSERT INTO generation_ui_artifacts (
            artifact_id, conversation_id, message_id, scenario,
            text, spec_json, data_json, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            (
                item["artifact_id"],
                conversation_id,
                assistant_message_id,
                item["scenario"],
                item["text"],
                item["spec_json"],
                item["data_json"],
                now,
            )
            for item in synchronized_artifacts
        ),
    )
    connection.executemany(
        """
        INSERT INTO conversation_resources (
            resource_id, conversation_id, message_id, kind, title,
            subtitle, url, path, media_type, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            (
                item["resource_id"],
                conversation_id,
                assistant_message_id,
                item["kind"],
                item["title"],
                item["subtitle"],
                item["url"],
                item["path"],
                item["media_type"],
                now,
            )
            for item in serialized_resources
        ),
    )
    return assistant_message_id


def _validate_conversation_id(conversation_id: str) -> None:
    try:
        UUID(conversation_id)
    except ValueError as exc:
        raise ConversationNotFoundError(conversation_id) from exc


def _conversation_title(content: str, *, max_chars: int = 36) -> str:
    normalized = " ".join(content.split())
    if not normalized:
        return "新对话"
    if len(normalized) <= max_chars:
        return normalized
    return normalized[: max_chars - 1] + "…"


def _validate_attachment_ids(conversation_id: str, attachment_id: str) -> None:
    _validate_conversation_id(conversation_id)
    try:
        UUID(attachment_id)
    except ValueError as exc:
        raise AttachmentNotFoundError(attachment_id) from exc


def _validate_uuid(value: str, field: str) -> None:
    try:
        UUID(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field} must be a UUID") from exc


def _conversation_ui_artifact(
    connection: sqlite3.Connection, row: Sequence[Any]
) -> ConversationUiArtifact:
    scenario = str(row[3])
    spec = _json_object(row[5], "spec_json")
    data = _json_object(row[6], "data_json")
    if scenario == "owner_daily_briefing":
        data = _hydrate_owner_briefing_data(connection, data)
        spec = _upgrade_owner_briefing_spec(spec, data)
    return ConversationUiArtifact(
        artifact_id=str(row[0]),
        conversation_id=str(row[1]),
        message_id=int(row[2]),
        scenario=scenario,
        text=str(row[4]),
        spec=spec,
        data=data,
    )


def _upgrade_owner_briefing_spec(
    spec: dict[str, Any], data: dict[str, Any]
) -> dict[str, Any]:
    actionable_options = _owner_action_options(data.get("anomalies"))
    root = spec.get("root")
    if isinstance(root, dict) and isinstance(root.get("children"), list):
        has_notice = any(
            isinstance(card, dict)
            and any(
                isinstance(child, dict) and child.get("id") == "owner-live-state-notice"
                for child in card.get("children", [])
            )
            for card in root["children"]
        )
        if not has_notice:
            root["children"].insert(
                2,
                {
                    "id": "card-owner-live-state-notice",
                    "type": "Card",
                    "props": {"title": "状态说明"},
                    "children": [
                        {
                            "id": "owner-live-state-notice",
                            "type": "Alert",
                            "props": {
                                "path": "live_state_notice",
                                "severity": "info",
                            },
                            "children": [],
                        }
                    ],
                },
            )
        if not actionable_options:
            root["children"] = [
                card
                for card in root["children"]
                if not (
                    isinstance(card, dict)
                    and any(
                        isinstance(child, dict) and child.get("id") == "task-form"
                        for child in card.get("children", [])
                    )
                )
            ]
    stack = [spec.get("root")]
    while stack:
        node = stack.pop()
        if not isinstance(node, dict):
            continue
        props = node.get("props")
        if node.get("type") == "DataTable" and isinstance(props, dict):
            path = props.get("path")
            columns = props.get("columns")
            if path == "anomalies":
                props["columns"] = [
                    {"field": "title", "header": "异常事项"},
                    {"field": "severity_label", "header": "优先级"},
                    {"field": "status_label", "header": "状态"},
                    {"field": "owner", "header": "责任人"},
                    {"field": "assignee", "header": "承接人"},
                    {
                        "field": "due_at_local",
                        "header": "截止时间（北京时间）",
                    },
                    {"field": "reminder_count", "header": "提醒次数"},
                    {
                        "field": "escalation_pending_label",
                        "header": "待升级确认",
                    },
                    {"field": "evidence_ref", "header": "证据"},
                ]
            elif isinstance(columns, list):
                replacements = (
                    {
                        "previous_status": (
                            "previous_status_label",
                            "昨日状态",
                        )
                    }
                    if path == "previous_day_review.recurring_issues"
                    else {}
                )
                for column in columns:
                    if not isinstance(column, dict):
                        continue
                    replacement = replacements.get(column.get("field"))
                    if replacement is not None:
                        column["field"], column["header"] = replacement
        if node.get("id") == "task-form" and isinstance(props, dict):
            props["title"] = (
                "请选择异常事件和处理动作；系统会自动关联编号。"
                "所有动作仅更新本地状态，不会发送外部消息。"
            )
        if (
            node.get("type") == "Select"
            and isinstance(props, dict)
            and props.get("model") == "anomaly_key"
        ):
            props.update(
                {
                    "label": "处理异常",
                    "options": actionable_options,
                    "span": "full",
                }
            )
        children = node.get("children")
        if isinstance(children, list):
            stack.extend(children)
    if actionable_options:
        defaults = spec.setdefault("form_defaults", {})
        actionable_keys = {option["value"] for option in actionable_options}
        if defaults.get("anomaly_key") not in actionable_keys:
            defaults["anomaly_key"] = actionable_options[0]["value"]
        defaults["action"] = _owner_default_action(
            data.get("anomalies"),
            str(defaults["anomaly_key"]),
        )
    else:
        spec["interaction_mode"] = "read_only"
        spec["actions"] = []
        defaults = spec.setdefault("form_defaults", {})
        defaults.update({"anomaly_key": "", "action": ""})
    return spec


def _hydrate_owner_briefing_data(
    connection: sqlite3.Connection, data: dict[str, Any]
) -> dict[str, Any]:
    report_date = data.get("report_date")
    anomalies = data.get("anomalies")
    if not isinstance(report_date, str) or not isinstance(anomalies, list):
        return data
    data["live_state_notice"] = (
        "卡片数据已按当前任务库实时同步；Agent 对话正文保留生成时快照，"
        "请以卡片状态为准。"
    )
    current_issue_keys: list[str] = []
    for anomaly in anomalies:
        if not isinstance(anomaly, dict):
            continue
        anomaly["severity_label"] = _owner_severity_label(anomaly.get("severity"))
        issue_key = anomaly.get("issue_key")
        if isinstance(issue_key, str):
            current_issue_keys.append(issue_key)
        task_id = anomaly.get("task_id")
        row = (
            connection.execute(
                """
                SELECT status, assignee, note, assigned_at, due_at,
                       next_reminder_at, last_reminded_at, reminder_count,
                       escalation_pending
                FROM owner_briefing_tasks
                WHERE task_id = ?
                """,
                (task_id,),
            ).fetchone()
            if isinstance(task_id, str)
            else None
        )
        if row is None:
            status = "detected"
            due_at = ""
            escalation_pending = False
            anomaly.update(
                {
                    "status": status,
                    "assignee": "",
                    "note": "",
                    "assigned_at": "",
                    "due_at": due_at,
                    "next_reminder_at": "",
                    "last_reminded_at": "",
                    "reminder_count": 0,
                    "escalation_pending": escalation_pending,
                }
            )
        else:
            status = str(row[0])
            due_at = str(row[4] or "")
            escalation_pending = bool(row[8])
            anomaly.update(
                {
                    "status": status,
                    "assignee": str(row[1] or ""),
                    "note": str(row[2] or ""),
                    "assigned_at": str(row[3] or ""),
                    "due_at": due_at,
                    "next_reminder_at": str(row[5] or ""),
                    "last_reminded_at": str(row[6] or ""),
                    "reminder_count": int(row[7] or 0),
                    "escalation_pending": escalation_pending,
                }
            )
        anomaly.update(
            _owner_display_fields(
                status,
                due_at=due_at,
                escalation_pending=escalation_pending,
            )
        )
    data["previous_day_review"] = _owner_next_day_review(
        connection,
        report_date,
        current_issue_keys=current_issue_keys,
    )
    return data


def _owner_next_day_review(
    connection: sqlite3.Connection,
    report_date: str,
    *,
    current_issue_keys: Sequence[str] = (),
) -> dict[str, Any]:
    try:
        current_date = date.fromisoformat(report_date)
    except (TypeError, ValueError) as exc:
        raise ValueError("report_date must use YYYY-MM-DD") from exc
    previous_date = (current_date - timedelta(days=1)).isoformat()
    rows = connection.execute(
        """
        SELECT issue_key, title, status, assignee, note,
               reminder_count, escalation_pending
        FROM owner_briefing_tasks
        WHERE report_date = ?
        ORDER BY issue_key
        """,
        (previous_date,),
    ).fetchall()
    current_keys = set(current_issue_keys)
    recurring = [
        {
            "issue_key": str(row[0]),
            "title": str(row[1]),
            "previous_status": str(row[2]),
            "previous_status_label": _owner_status_label(str(row[2])),
            "assignee": str(row[3] or ""),
        }
        for row in rows
        if row[0] in current_keys
    ]
    unresolved = [
        {
            "issue_key": str(row[0]),
            "title": str(row[1]),
            "status": str(row[2]),
            "status_label": _owner_status_label(str(row[2])),
            "assignee": str(row[3] or ""),
            "note": str(row[4] or ""),
            "reminder_count": int(row[5] or 0),
            "escalation_pending": bool(row[6]),
            "escalation_pending_label": "是" if bool(row[6]) else "否",
        }
        for row in rows
        if row[2] in {"detected", "assigned"}
    ]
    return {
        "review_date": previous_date,
        "generated_for": report_date,
        "metrics": {
            "new_count": len(rows),
            "completed_count": sum(row[2] == "completed" for row in rows),
            "unresolved_count": len(unresolved),
            "escalated_count": sum(row[2] == "escalated" for row in rows),
            "recurring_count": len(recurring),
        },
        "unresolved_tasks": unresolved,
        "recurring_issues": recurring,
    }


def _owner_status_label(status: str) -> str:
    return OWNER_STATUS_LABELS.get(status, status)


def _owner_local_time(value: str) -> str:
    if not value:
        return ""
    return _utc_datetime(value).astimezone(CHINA_TIMEZONE).strftime("%Y-%m-%d %H:%M")


def _generation_action_receipt(
    connection: sqlite3.Connection,
    idempotency_key: str,
    conversation_id: str,
    artifact_id: str,
    action_id: str,
    arguments: dict[str, Any],
) -> dict[str, Any] | None:
    row = connection.execute(
        """
        SELECT conversation_id, artifact_id, action_id,
               arguments_json, response_json
        FROM generation_ui_actions
        WHERE idempotency_key = ?
        """,
        (idempotency_key,),
    ).fetchone()
    if row is None:
        return None
    if tuple(row[:3]) != (conversation_id, artifact_id, action_id):
        raise GenerationUiActionConflictError(
            "idempotency_key 已被其他 Action 使用。"
        )
    if _json_object(row[3], "arguments_json") != arguments:
        raise GenerationUiActionConflictError(
            "同一 idempotency_key 不能提交不同参数。"
        )
    return _json_object(row[4], "response_json")


def _insert_generation_action(
    connection: sqlite3.Connection,
    idempotency_key: str,
    conversation_id: str,
    artifact_id: str,
    action_id: str,
    arguments: dict[str, Any],
    response: dict[str, Any],
    created_at: str,
) -> None:
    connection.execute(
        """
        INSERT INTO generation_ui_actions (
            idempotency_key, conversation_id, artifact_id,
            action_id, arguments_json, response_json, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (
            idempotency_key,
            conversation_id,
            artifact_id,
            action_id,
            json.dumps(arguments, ensure_ascii=False, separators=(",", ":")),
            json.dumps(response, ensure_ascii=False, separators=(",", ":")),
            created_at,
        ),
    )


def _refresh_content_metrics(data: dict[str, Any]) -> None:
    contents = data.get("contents")
    if not isinstance(contents, list):
        raise GenerationUiActionConflictError("内容计划数据不完整。")
    valid_contents = [item for item in contents if isinstance(item, dict)]
    data["metrics"] = {
        "content_count": len(valid_contents),
        "pending_review_count": sum(
            item.get("status") in {"pending_review", "revision_requested"}
            for item in valid_contents
        ),
        "approved_count": sum(
            item.get("status") == "approved" for item in valid_contents
        ),
        "draft_saved_count": sum(
            item.get("rpa_status") == "draft_saved" for item in valid_contents
        ),
    }


def _owner_display_fields(
    status: str, *, due_at: str, escalation_pending: bool
) -> dict[str, str]:
    return {
        "status_label": _owner_status_label(status),
        "due_at_local": _owner_local_time(due_at),
        "escalation_pending_label": "是" if escalation_pending else "否",
    }


def _owner_severity_label(value: Any) -> str:
    text = str(value or "").strip()
    return OWNER_SEVERITY_LABELS.get(text, text or "未分级")


def _owner_action_options(value: Any) -> list[dict[str, str]]:
    if not isinstance(value, list):
        return []
    options: list[dict[str, str]] = []
    for anomaly in value:
        if not isinstance(anomaly, dict):
            continue
        if anomaly.get("status") not in OWNER_ACTIONABLE_STATUSES:
            continue
        anomaly_key = anomaly.get("anomaly_key")
        title = anomaly.get("title")
        if not isinstance(anomaly_key, str) or not isinstance(title, str):
            continue
        severity = _owner_severity_label(anomaly.get("severity"))
        status = (
            "待升级"
            if anomaly.get("escalation_pending")
            else str(anomaly.get("status_label") or "").strip()
        )
        prefix = f"{severity} · {status}" if status else severity
        options.append(
            {
                "label": f"【{prefix}】{title.strip() or '未命名异常'}",
                "value": anomaly_key,
            }
        )
    return options


def _owner_default_action(value: Any, anomaly_key: str) -> str:
    if not isinstance(value, list):
        return ""
    anomaly = next(
        (
            item
            for item in value
            if isinstance(item, dict) and item.get("anomaly_key") == anomaly_key
        ),
        None,
    )
    if not isinstance(anomaly, dict):
        return ""
    if anomaly.get("status") == "detected":
        return "分派"
    if anomaly.get("status") == "assigned":
        return "升级" if anomaly.get("escalation_pending") else "完成"
    return ""


def _serialize_ui_artifact(artifact: dict[str, Any]) -> dict[str, str]:
    if not isinstance(artifact, dict):
        raise ValueError("Generation UI artifact must be an object")
    if artifact.get("type") != "generation_ui":
        raise ValueError("Generation UI artifact type must be generation_ui")
    artifact_id = artifact.get("artifact_id")
    try:
        UUID(str(artifact_id))
    except (TypeError, ValueError) as exc:
        raise ValueError("Generation UI artifact_id must be a UUID") from exc
    scenario = artifact.get("scenario")
    text = artifact.get("text")
    spec = artifact.get("spec")
    data = artifact.get("data")
    if not isinstance(scenario, str) or not scenario:
        raise ValueError("Generation UI scenario must be a non-empty string")
    if not isinstance(text, str) or not text:
        raise ValueError("Generation UI text must be a non-empty string")
    if not isinstance(spec, dict) or not isinstance(data, dict):
        raise ValueError("Generation UI spec and data must be objects")
    return {
        "artifact_id": str(artifact_id),
        "scenario": scenario,
        "text": text,
        "spec_json": json.dumps(spec, ensure_ascii=False, separators=(",", ":")),
        "data_json": json.dumps(data, ensure_ascii=False, separators=(",", ":")),
    }


def _synchronize_owner_briefing_artifact(
    connection: sqlite3.Connection,
    conversation_id: str,
    artifact: dict[str, str],
    now: str,
) -> dict[str, str]:
    """用稳定 task_id 去重任务，并把已有状态覆盖到重复生成的早报。"""

    if artifact["scenario"] != "owner_daily_briefing":
        return artifact
    data = _json_object(artifact["data_json"], "data_json")
    report_date = data.get("report_date")
    anomalies = data.get("anomalies")
    if not isinstance(report_date, str) or not isinstance(anomalies, list):
        raise ValueError("Owner briefing artifact data is incomplete")
    for anomaly in anomalies:
        if not isinstance(anomaly, dict):
            raise ValueError("Owner briefing anomaly must be an object")
        task_id = anomaly.get("task_id")
        anomaly_key = anomaly.get("anomaly_key")
        issue_key = anomaly.get("issue_key")
        title = anomaly.get("title")
        owner = anomaly.get("owner")
        if not all(
            isinstance(value, str) and value
            for value in (task_id, anomaly_key, issue_key, title, owner)
        ):
            raise ValueError("Owner briefing task data is incomplete")
        _validate_uuid(task_id, "task_id")
        connection.execute(
            """
            INSERT OR IGNORE INTO owner_briefing_tasks (
                task_id, anomaly_key, issue_key, report_date, title, owner,
                status, source_artifact_id, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, 'detected', ?, ?, ?)
            """,
            (
                task_id,
                anomaly_key,
                issue_key,
                report_date,
                title,
                owner,
                artifact["artifact_id"],
                now,
                now,
            ),
        )
        connection.execute(
            """
            UPDATE owner_briefing_tasks
            SET issue_key = ?, title = ?, owner = ?
            WHERE task_id = ?
            """,
            (issue_key, title, owner, task_id),
        )
        row = connection.execute(
            """
            SELECT status, assignee, note, assigned_at, due_at,
                   next_reminder_at, last_reminded_at, reminder_count,
                   escalation_pending
            FROM owner_briefing_tasks
            WHERE task_id = ?
            """,
            (task_id,),
        ).fetchone()
        if row is None:
            raise ValueError("Owner briefing task synchronization failed")
        anomaly.update(
            {
                "status": str(row[0]),
                "assignee": str(row[1] or ""),
                "note": str(row[2] or ""),
                "assigned_at": str(row[3] or ""),
                "due_at": str(row[4] or ""),
                "next_reminder_at": str(row[5] or ""),
                "last_reminded_at": str(row[6] or ""),
                "reminder_count": int(row[7] or 0),
                "escalation_pending": bool(row[8]),
            }
        )
        anomaly.update(
            _owner_display_fields(
                str(row[0]),
                due_at=str(row[4] or ""),
                escalation_pending=bool(row[8]),
            )
        )
    synchronized = dict(artifact)
    synchronized["data_json"] = json.dumps(
        data,
        ensure_ascii=False,
        separators=(",", ":"),
    )
    return synchronized


def _serialize_trace(trace: Sequence[str]) -> list[str]:
    if isinstance(trace, (str, bytes)) or not isinstance(trace, Sequence):
        raise ValueError("Conversation execution trace must be a sequence")
    if any(not isinstance(item, str) for item in trace):
        raise ValueError("Conversation execution trace entries must be strings")
    return list(trace)


def _serialize_resource(resource: dict[str, Any]) -> dict[str, str | None]:
    if not isinstance(resource, dict):
        raise ValueError("Conversation resource must be an object")
    resource_id = str(resource.get("resource_id") or uuid4())
    _validate_uuid(resource_id, "resource_id")
    kind = resource.get("kind")
    if kind not in {"artifact", "source", "evidence"}:
        raise ValueError("Conversation resource kind is invalid")
    title = resource.get("title")
    if not isinstance(title, str) or not title.strip() or len(title) > 240:
        raise ValueError("Conversation resource title is invalid")
    subtitle = resource.get("subtitle")
    if subtitle is not None and (
        not isinstance(subtitle, str) or len(subtitle) > 1000
    ):
        raise ValueError("Conversation resource subtitle is invalid")
    url = resource.get("url")
    if url is not None:
        if not isinstance(url, str):
            raise ValueError("Conversation resource URL is invalid")
        parsed = urlsplit(url)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise ValueError("Conversation resource URL must use HTTP or HTTPS")
    path = resource.get("path")
    if path is not None:
        if not isinstance(path, str) or "\\" in path:
            raise ValueError("Conversation resource path is invalid")
        relative = PurePosixPath(path)
        if relative.is_absolute() or ".." in relative.parts:
            raise ValueError("Conversation resource path is invalid")
        if not relative.parts or relative.parts[0] != "artifacts":
            raise ValueError("Conversation resource path must be inside artifacts")
        path = relative.as_posix()
    if kind == "artifact" and path is None:
        raise ValueError("Artifact resource requires a path")
    if kind == "source" and url is None:
        raise ValueError("Source resource requires a URL")
    media_type = resource.get("media_type")
    if media_type is not None and (
        not isinstance(media_type, str) or len(media_type) > 160
    ):
        raise ValueError("Conversation resource media type is invalid")
    return {
        "resource_id": resource_id,
        "kind": kind,
        "title": title.strip(),
        "subtitle": subtitle.strip() if isinstance(subtitle, str) else None,
        "url": url,
        "path": path,
        "media_type": media_type,
    }


def _json_object(value: str, field: str) -> dict[str, Any]:
    decoded = json.loads(value)
    if not isinstance(decoded, dict):
        raise ValueError(f"Stored {field} must decode to an object")
    return decoded


def _json_string_list(value: str, field: str) -> list[str]:
    decoded = json.loads(value)
    if not isinstance(decoded, list) or any(
        not isinstance(item, str) for item in decoded
    ):
        raise ValueError(f"Stored {field} must decode to a string list")
    return decoded


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _utc_datetime(value: str | datetime | None) -> datetime:
    if value is None:
        return datetime.now(UTC)
    parsed = datetime.fromisoformat(value) if isinstance(value, str) else value
    if parsed.tzinfo is None:
        raise ValueError("Reminder timestamp must include a timezone")
    return parsed.astimezone(UTC)


def _optional_utc_datetime(value: Any) -> datetime | None:
    if value in {None, ""}:
        return None
    if not isinstance(value, str):
        raise ValueError("Stored reminder timestamp must be text")
    return _utc_datetime(value)


def _after_hours(value: str, hours: int) -> str:
    return (_utc_datetime(value) + timedelta(hours=hours)).isoformat()


__all__ = [
    "AttachmentNotFoundError",
    "ConversationAttachment",
    "ConversationNotFoundError",
    "ConversationResource",
    "ConversationResourceNotFoundError",
    "SQLiteConversationStore",
]
