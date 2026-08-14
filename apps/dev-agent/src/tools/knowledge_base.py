"""Agent 工具：只读检索固定的 LazyTT RAG 知识库。"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, BinaryIO
from uuid import UUID, uuid4

import httpx
from dotenv import load_dotenv

from testagent_dev_agent.paths import REPOSITORY_ROOT


MAX_QUERY_CHARS = 2_000
MAX_CANDIDATES = 5
MAX_CONTENT_CHARS = 2_000
MAX_RESPONSE_BYTES = 2 * 1024 * 1024
_ATTACHMENT_KNOWLEDGE_BASE_ENV = {
    "pdf": "RAG_ATTACHMENT_PDF_KNOWLEDGE_BASE_ID",
    "office": "RAG_ATTACHMENT_OFFICE_KNOWLEDGE_BASE_ID",
    "image": "RAG_ATTACHMENT_IMAGE_KNOWLEDGE_BASE_ID",
    "media": "RAG_ATTACHMENT_MEDIA_KNOWLEDGE_BASE_ID",
}


@dataclass(frozen=True)
class AttachmentKnowledgeBases:
    """按文件族保存四个独立附件知识库的 Gateway UUID。"""

    pdf: str
    office: str
    image: str
    media: str

    def for_kind(self, kind: str) -> str:
        if kind not in {"pdf", "office", "image", "media"}:
            raise RuntimeError(f"Unsupported attachment knowledge base kind: {kind}")
        return str(getattr(self, kind))


@dataclass(frozen=True)
class KnowledgeBaseConfig:
    """保存 TestAgent 固定知识库的只读连接配置。"""

    base_url: str
    knowledge_base_id: str
    bearer_token: str = field(repr=False)
    attachment_knowledge_bases: AttachmentKnowledgeBases | None = None
    retrieval_profile: str = "hybrid-default@1"
    timeout_seconds: float = 30.0

    @classmethod
    def from_env(cls) -> KnowledgeBaseConfig:
        """从本地 `.env` 和 Git 忽略的 Token 文件读取配置。"""
        load_dotenv(REPOSITORY_ROOT / ".env")
        if not _truthy(os.getenv("RAG_ENABLED", "false")):
            raise RuntimeError("RAG integration is disabled")
        base_url = os.getenv("RAG_BASE_URL", "").strip()
        knowledge_base_id = os.getenv("RAG_KNOWLEDGE_BASE_ID", "").strip()
        attachment_values = {
            kind: os.getenv(env_name, "").strip()
            for kind, env_name in _ATTACHMENT_KNOWLEDGE_BASE_ENV.items()
        }
        token_file_raw = os.getenv("RAG_BEARER_TOKEN_FILE", "").strip()
        if not base_url:
            raise RuntimeError("Missing required environment variable: RAG_BASE_URL")
        try:
            UUID(knowledge_base_id)
        except ValueError as exc:
            raise RuntimeError("RAG_KNOWLEDGE_BASE_ID must be a UUID") from exc
        configured_attachment_values = {
            kind: value for kind, value in attachment_values.items() if value
        }
        if configured_attachment_values and len(configured_attachment_values) != len(
            _ATTACHMENT_KNOWLEDGE_BASE_ENV
        ):
            missing = [
                env_name
                for kind, env_name in _ATTACHMENT_KNOWLEDGE_BASE_ENV.items()
                if not attachment_values[kind]
            ]
            raise RuntimeError(
                "Missing required environment variables: " + ", ".join(missing)
            )
        for kind, value in configured_attachment_values.items():
            try:
                UUID(value)
            except ValueError as exc:
                raise RuntimeError(
                    f"{_ATTACHMENT_KNOWLEDGE_BASE_ENV[kind]} must be a UUID"
                ) from exc
        if configured_attachment_values:
            attachment_ids = set(configured_attachment_values.values())
            if len(attachment_ids) != len(_ATTACHMENT_KNOWLEDGE_BASE_ENV):
                raise RuntimeError("Attachment knowledge base UUIDs must be distinct")
            if knowledge_base_id in attachment_ids:
                raise RuntimeError(
                    "Attachment knowledge bases must not reuse RAG_KNOWLEDGE_BASE_ID"
                )
        if not token_file_raw:
            raise RuntimeError("Missing required environment variable: RAG_BEARER_TOKEN_FILE")
        token_file = Path(token_file_raw).expanduser()
        if not token_file.is_absolute():
            token_file = REPOSITORY_ROOT / token_file
        try:
            bearer_token = token_file.read_text(encoding="utf-8").strip()
        except OSError as exc:
            raise RuntimeError("Cannot read RAG_BEARER_TOKEN_FILE") from exc
        if not bearer_token:
            raise RuntimeError("RAG_BEARER_TOKEN_FILE is empty")
        retrieval_profile = os.getenv(
            "RAG_RETRIEVAL_PROFILE", "hybrid-default@1"
        ).strip()
        timeout_raw = os.getenv("RAG_TIMEOUT_SECONDS", "30").strip()
        try:
            timeout_seconds = float(timeout_raw)
        except ValueError as exc:
            raise RuntimeError("RAG_TIMEOUT_SECONDS must be numeric") from exc
        if not retrieval_profile or timeout_seconds <= 0:
            raise RuntimeError("RAG retrieval profile and timeout must be valid")
        attachment_knowledge_bases = (
            AttachmentKnowledgeBases(**attachment_values)
            if configured_attachment_values
            else None
        )
        return cls(
            base_url=base_url,
            knowledge_base_id=knowledge_base_id,
            bearer_token=bearer_token,
            attachment_knowledge_bases=attachment_knowledge_bases,
            retrieval_profile=retrieval_profile,
            timeout_seconds=timeout_seconds,
        )

    def for_knowledge_base(self, knowledge_base_id: str) -> KnowledgeBaseConfig:
        """复用同一 Gateway/JWT，但把检索和写入限定到另一个知识库。"""
        try:
            UUID(knowledge_base_id)
        except ValueError as exc:
            raise RuntimeError("knowledge_base_id must be a UUID") from exc
        return KnowledgeBaseConfig(
            base_url=self.base_url,
            knowledge_base_id=knowledge_base_id,
            bearer_token=self.bearer_token,
            attachment_knowledge_bases=self.attachment_knowledge_bases,
            retrieval_profile=self.retrieval_profile,
            timeout_seconds=self.timeout_seconds,
        )


class KnowledgeBaseClient:
    """调用 Gateway `/v1/retrieval` 并返回受限的引用字段。"""

    def __init__(
        self,
        config: KnowledgeBaseConfig,
        *,
        http_client: httpx.Client | None = None,
    ) -> None:
        self.config = config
        self._owns_client = http_client is None
        self._client = http_client or httpx.Client(
            base_url=config.base_url.rstrip("/") + "/",
            timeout=httpx.Timeout(config.timeout_seconds, connect=3.0),
            follow_redirects=False,
            trust_env=False,
            headers={"Accept": "application/json", "User-Agent": "TestAgent-RAG/0.1"},
        )

    @property
    def knowledge_base_id(self) -> str:
        return self.config.knowledge_base_id

    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    def search(
        self,
        query: str,
        *,
        knowledge_base_ids: tuple[str, ...] = (),
        document_ids: tuple[str, ...] = (),
    ) -> dict[str, Any]:
        if not isinstance(query, str) or not query.strip():
            return _error("INVALID_ARGUMENTS", "query 必须是非空字符串。")
        if len(document_ids) > 100:
            return _error("INVALID_ARGUMENTS", "document_ids 最多允许 100 个。")
        if len(knowledge_base_ids) > 20:
            return _error("INVALID_ARGUMENTS", "knowledge_base_ids 最多允许 20 个。")
        try:
            normalized_document_ids = [str(UUID(value)) for value in document_ids]
            normalized_knowledge_base_ids = [
                str(UUID(value))
                for value in (
                    knowledge_base_ids or (self.config.knowledge_base_id,)
                )
            ]
        except (TypeError, ValueError) as exc:
            return _error(
                "INVALID_ARGUMENTS",
                f"knowledge_base_ids 和 document_ids 必须是 UUID：{type(exc).__name__}",
            )
        normalized_query = " ".join(query.split())
        if len(normalized_query) > MAX_QUERY_CHARS:
            return _error(
                "INVALID_ARGUMENTS",
                f"query 长度超过 {MAX_QUERY_CHARS} 字符。",
            )
        try:
            response = self._client.post(
                "v1/retrieval",
                headers=self._headers(),
                json={
                    "knowledge_base_ids": normalized_knowledge_base_ids,
                    "document_ids": normalized_document_ids,
                    "query": normalized_query,
                    "retrieval_profile": self.config.retrieval_profile,
                    "options": {
                        "page_size": MAX_CANDIDATES,
                        "include_content": True,
                        "include_positions": True,
                    },
                },
            )
        except httpx.TimeoutException:
            return _error("RAG_TIMEOUT", "RAG 检索请求超时。", retryable=True)
        except httpx.RequestError as exc:
            return _error(
                "RAG_UNAVAILABLE",
                f"RAG 检索请求失败：{type(exc).__name__}",
                retryable=True,
            )
        payload, response_error = self._payload(response)
        if response_error is not None:
            return response_error
        data = payload.get("data") if isinstance(payload, dict) else None
        raw_candidates = data.get("candidates") if isinstance(data, dict) else None
        if not isinstance(raw_candidates, list):
            return _error("INVALID_RAG_RESPONSE", "RAG Gateway 响应结构无效。")
        candidates = [
            normalized
            for item in raw_candidates[:MAX_CANDIDATES]
            if (normalized := _normalize_candidate(item)) is not None
        ]
        warnings = data.get("warnings")
        return {
            "success": True,
            "query": normalized_query,
            "knowledge_base_id": self.config.knowledge_base_id,
            "result_count": len(candidates),
            "candidates": candidates,
            "warnings": warnings if isinstance(warnings, list) else [],
            "request_id": payload.get("request_id"),
            "effective_query": data.get("effective_query"),
            "query_plan": data.get("query_plan"),
            "trust": "untrusted_knowledge_data",
            "instructions_executable": False,
        }

    def upload_document(
        self,
        file: BinaryIO,
        *,
        filename: str,
        content_type: str,
        attachment_id: str,
        conversation_id: str,
    ) -> dict[str, Any]:
        """把一次用户主动上传转发到当前配置的附件知识库。"""
        try:
            UUID(attachment_id)
            UUID(conversation_id)
        except ValueError:
            return _error("INVALID_ARGUMENTS", "attachment_id 和 conversation_id 必须是 UUID。")
        try:
            response = self._client.post(
                f"v1/knowledge-bases/{self.config.knowledge_base_id}/documents",
                headers=self._headers(idempotency_key=f"attachment-upload-{attachment_id}"),
                data={
                    "external_id": attachment_id,
                    "metadata": json.dumps(
                        {
                            "source": "testagent_chat_attachment",
                            "conversation_id": conversation_id,
                        },
                        ensure_ascii=False,
                        separators=(",", ":"),
                    ),
                    "auto_parse": "true",
                },
                files={"file": (filename, file, content_type)},
            )
        except httpx.TimeoutException:
            return _error("RAG_TIMEOUT", "RAG 上传请求超时。", retryable=True)
        except httpx.RequestError as exc:
            return _error(
                "RAG_UNAVAILABLE",
                f"RAG 上传请求失败：{type(exc).__name__}",
                retryable=True,
            )
        payload, response_error = self._payload(response)
        if response_error is not None:
            return response_error
        data = payload.get("data") if isinstance(payload, dict) else None
        if not isinstance(data, dict) or not all(
            isinstance(data.get(name), str) for name in ("document_id", "job_id", "status")
        ):
            return _error("INVALID_RAG_RESPONSE", "RAG 上传响应结构无效。")
        return {
            "success": True,
            "request_id": payload.get("request_id"),
            "document_id": data["document_id"],
            "job_id": data["job_id"],
            "status": data["status"],
        }

    def get_job(self, job_id: str) -> dict[str, Any]:
        """读取附件解析 Job 的安全状态字段。"""
        return self._get_resource(f"v1/jobs/{_uuid(job_id, 'job_id')}", "job")

    def get_document(self, document_id: str) -> dict[str, Any]:
        """读取附件文档的安全状态字段。"""
        return self._get_resource(
            f"v1/documents/{_uuid(document_id, 'document_id')}", "document"
        )

    def delete_document(self, document_id: str, *, attachment_id: str) -> dict[str, Any]:
        """响应用户移除附件，异步删除没有共享引用的 RAG 文档。"""
        try:
            response = self._client.delete(
                f"v1/documents/{_uuid(document_id, 'document_id')}",
                params={"cancel_active": "true"},
                headers=self._headers(idempotency_key=f"attachment-delete-{_uuid(attachment_id, 'attachment_id')}"),
            )
        except httpx.TimeoutException:
            return _error("RAG_TIMEOUT", "RAG 删除请求超时。", retryable=True)
        except httpx.RequestError as exc:
            return _error(
                "RAG_UNAVAILABLE",
                f"RAG 删除请求失败：{type(exc).__name__}",
                retryable=True,
            )
        if response.status_code == 404:
            return {"success": True, "already_deleted": True}
        payload, response_error = self._payload(response)
        if response_error is not None:
            return response_error
        data = payload.get("data") if isinstance(payload, dict) else None
        if not isinstance(data, dict) or not isinstance(data.get("job_id"), str):
            return _error("INVALID_RAG_RESPONSE", "RAG 删除响应结构无效。")
        return {
            "success": True,
            "request_id": payload.get("request_id"),
            "job_id": data["job_id"],
            "status": data.get("status"),
        }

    def _get_resource(self, path: str, resource: str) -> dict[str, Any]:
        try:
            response = self._client.get(path, headers=self._headers())
        except httpx.TimeoutException:
            return _error("RAG_TIMEOUT", f"RAG {resource} 查询超时。", retryable=True)
        except httpx.RequestError as exc:
            return _error(
                "RAG_UNAVAILABLE",
                f"RAG {resource} 查询失败：{type(exc).__name__}",
                retryable=True,
            )
        payload, response_error = self._payload(response)
        if response_error is not None:
            return response_error
        data = payload.get("data") if isinstance(payload, dict) else None
        if not isinstance(data, dict) or not isinstance(data.get("status"), str):
            return _error("INVALID_RAG_RESPONSE", f"RAG {resource} 响应结构无效。")
        return {
            "success": True,
            "request_id": payload.get("request_id"),
            "data": data,
        }

    def _headers(self, *, idempotency_key: str | None = None) -> dict[str, str]:
        headers = {
            "Authorization": f"Bearer {self.config.bearer_token}",
            "X-Request-ID": str(uuid4()),
        }
        if idempotency_key is not None:
            headers["Idempotency-Key"] = idempotency_key
        return headers

    def _payload(
        self, response: httpx.Response
    ) -> tuple[dict[str, Any], dict[str, Any] | None]:
        if len(response.content) > MAX_RESPONSE_BYTES:
            return {}, _error("INVALID_RAG_RESPONSE", "RAG 响应超过 2 MiB 限制。")
        try:
            payload = response.json()
        except ValueError:
            payload = {}
        if not response.is_success:
            api_error = payload.get("error") if isinstance(payload, dict) else None
            stable_code = api_error.get("code") if isinstance(api_error, dict) else None
            retryable = (
                api_error.get("retryable")
                if isinstance(api_error, dict) and isinstance(api_error.get("retryable"), bool)
                else response.status_code == 429 or response.status_code >= 500
            )
            error_type = stable_code or {
                401: "RAG_AUTHENTICATION_ERROR",
                403: "RAG_AUTHORIZATION_ERROR",
                404: "RAG_RESOURCE_NOT_FOUND",
            }.get(response.status_code, "RAG_UPSTREAM_ERROR")
            return {}, _error(
                error_type,
                f"RAG Gateway 返回 HTTP {response.status_code}。",
                retryable=retryable,
            )
        if not isinstance(payload, dict):
            return {}, _error("INVALID_RAG_RESPONSE", "RAG Gateway 返回了非对象 JSON。")
        return payload, None


def search_knowledge_base(
    query: str,
    *,
    client: KnowledgeBaseClient | None = None,
    knowledge_base_ids: tuple[str, ...] = (),
    document_ids: tuple[str, ...] = (),
) -> dict[str, Any]:
    """只读检索长期知识库或调用方限定的附件知识库，不暴露管理能力。"""
    owns_client = client is None
    try:
        capability = client or KnowledgeBaseClient(KnowledgeBaseConfig.from_env())
    except RuntimeError as exc:
        return _error("CONFIGURATION_ERROR", str(exc))
    try:
        return capability.search(
            query,
            knowledge_base_ids=knowledge_base_ids,
            document_ids=document_ids,
        )
    finally:
        if owns_client:
            capability.close()


def knowledge_base_configuration_status() -> tuple[bool, bool]:
    """返回 `(enabled, configured)`；关闭时不阻塞 Agent 就绪。"""
    load_dotenv(REPOSITORY_ROOT / ".env")
    enabled = _truthy(os.getenv("RAG_ENABLED", "false"))
    if not enabled:
        return False, True
    try:
        KnowledgeBaseConfig.from_env()
    except RuntimeError:
        return True, False
    return True, True


def attachment_knowledge_base_configuration_status() -> bool:
    """四个附件库必须全部显式配置，绝不回退到长期知识库。"""
    try:
        return KnowledgeBaseConfig.from_env().attachment_knowledge_bases is not None
    except RuntimeError:
        return False


def attachment_knowledge_base_config(kind: str) -> KnowledgeBaseConfig:
    """返回指定文件族的附件库配置；缺失时拒绝上传。"""
    config = KnowledgeBaseConfig.from_env()
    if config.attachment_knowledge_bases is None:
        raise RuntimeError(
            "Missing required attachment knowledge base environment variables"
        )
    return config.for_knowledge_base(config.attachment_knowledge_bases.for_kind(kind))


def _normalize_candidate(item: Any) -> dict[str, Any] | None:
    if not isinstance(item, dict):
        return None
    content = item.get("content")
    document_name = item.get("document_name")
    chunk_id = item.get("chunk_id")
    if not all(isinstance(value, str) for value in (content, document_name, chunk_id)):
        return None
    pages = sorted(
        {
            position["page"]
            for position in item.get("positions", [])
            if isinstance(position, dict)
            and isinstance(position.get("page"), int)
            and not isinstance(position.get("page"), bool)
        }
    )
    scores = item.get("scores")
    score = scores.get("score") if isinstance(scores, dict) else None
    return {
        "chunk_id": chunk_id,
        "document_id": item.get("document_id"),
        "document_name": document_name,
        "pages": pages,
        "content": content[:MAX_CONTENT_CHARS],
        "score": score if isinstance(score, int | float) and not isinstance(score, bool) else None,
    }


def _truthy(value: str) -> bool:
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _uuid(value: str, name: str) -> str:
    try:
        return str(UUID(value))
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name} must be a UUID") from exc


def _error(error_type: str, message: str, retryable: bool = False) -> dict[str, Any]:
    return {
        "success": False,
        "error_type": error_type,
        "message": message,
        "retryable": retryable,
    }


__all__ = [
    "AttachmentKnowledgeBases",
    "KnowledgeBaseClient",
    "KnowledgeBaseConfig",
    "attachment_knowledge_base_config",
    "attachment_knowledge_base_configuration_status",
    "knowledge_base_configuration_status",
    "search_knowledge_base",
]
