"""本地网页对话：复用 AgentLoop，返回回答和可展示的执行轨迹。"""

from __future__ import annotations

import asyncio
import inspect
import json
import os
import tempfile
import threading
import time
from collections import Counter
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import date
from functools import partial
from pathlib import Path, PurePosixPath
from typing import Any, BinaryIO, Literal, Protocol
from urllib.parse import urlsplit
from uuid import UUID

from fastapi import FastAPI, HTTPException, Query, Request, Response
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from dotenv import load_dotenv
from pydantic import BaseModel, ConfigDict, Field, field_validator

from agent.loop import AgentLoop, DEFAULT_TOOL_HANDLERS, TOOL_DEFINITIONS
from llm.client import LLMClient, LLMConnectionError, LLMStreamCancelled
from memory.client import build_memory_client_from_env, memory_configuration_status
from testagent_dev_agent.paths import REPOSITORY_ROOT
from tools.content_operations import (
    CONTENT_PERFORMANCE_SCENARIO,
    CONTENT_PERFORMANCE_TOOL_NAME,
    CONTENT_WEEKLY_PLAN_SCENARIO,
    CONTENT_WEEKLY_PLAN_TOOL_NAME,
    ContentOperationTools,
    compact_content_operation_result,
)
from tools.knowledge_base import (
    KnowledgeBaseClient,
    attachment_knowledge_base_config,
    attachment_knowledge_base_configuration_status,
    knowledge_base_configuration_status,
    search_knowledge_base,
)
from tools.lazytt_mcp import (
    LazyTtMcpConfig,
    LazyTtMcpError,
    LazyTtMcpTools,
    build_lazytt_mcp_tools_from_env,
)
from tools.mcp_generation_ui import (
    PREPARE_MATERIAL_ESTIMATE_INPUT_TOOL,
    PREPARE_MATERIAL_ESTIMATE_INPUT_TOOL_NAME,
    McpMaterialGenerationUiAdapter,
    prepare_material_estimate_input,
    supports_material_estimate_input,
)
from tools.owner_briefing import (
    OWNER_BRIEFING_SCENARIO,
    OWNER_BRIEFING_TOOL_NAME,
    attach_owner_briefing_presentation,
    attach_owner_next_day_review,
    compact_owner_briefing_result,
)
from tools.present_generation_ui import (
    GenerationUiPresenter,
    TrustedPresentationResultRegistry,
    prepare_generation_ui_input,
)
from tools.renovation_scenarios import RenovationScenarioTools
from tools.rpa_xiaohongshu import (
    CONTENT_IMAGE_MAX_BYTES,
    save_xiaohongshu_draft,
    store_content_image,
    validate_content_image_paths,
)
from web_agent.conversations import (
    AttachmentNotFoundError,
    ConversationAttachment,
    ConversationNotFoundError,
    ConversationResource,
    ConversationResourceNotFoundError,
    ConversationSummary,
    ConversationUiArtifact,
    GenerationUiActionConflictError,
    GenerationUiArtifactNotFoundError,
    SQLiteConversationStore,
)


STATIC_DIR = Path(__file__).resolve().parent / "static"
PROJECT_ROOT = REPOSITORY_ROOT
DEFAULT_ATTACHMENT_MAX_BYTES = 100 * 1024 * 1024
PDF_EXTENSIONS = {".pdf"}
OFFICE_EXTENSIONS = {
    ".csv",
    ".doc",
    ".docx",
    ".htm",
    ".html",
    ".md",
    ".ppt",
    ".pptx",
    ".txt",
    ".xls",
    ".xlsx",
}
IMAGE_EXTENSIONS = {".jpeg", ".jpg", ".png", ".webp"}
MEDIA_EXTENSIONS = {
    ".aac",
    ".avi",
    ".flac",
    ".m4a",
    ".mkv",
    ".mov",
    ".mp3",
    ".mp4",
    ".ogg",
    ".wav",
    ".webm",
}
AttachmentKind = Literal["pdf", "office", "image", "media"]
load_dotenv(PROJECT_ROOT / ".env")


class RunnableAgent(Protocol):
    last_context_receipt: object | None
    generation_ui_artifacts: list[dict[str, Any]]
    last_tool_results: list[dict[str, Any]]

    def run(
        self,
        task: str,
        *,
        history: list[dict[str, str]] | None = None,
        episode_id: str | None = None,
        generation_ui_event: Mapping[str, Any] | None = None,
        on_answer_delta: Callable[[str], None] | None = None,
        on_answer_reset: Callable[[], None] | None = None,
        is_cancelled: Callable[[], bool] | None = None,
    ) -> str: ...


class AttachmentGateway(Protocol):
    knowledge_base_id: str

    def close(self) -> None: ...

    def upload_document(
        self,
        file: BinaryIO,
        *,
        filename: str,
        content_type: str,
        attachment_id: str,
        conversation_id: str,
    ) -> dict[str, Any]: ...

    def get_job(self, job_id: str) -> dict[str, Any]: ...

    def get_document(self, document_id: str) -> dict[str, Any]: ...

    def delete_document(
        self, document_id: str, *, attachment_id: str
    ) -> dict[str, Any]: ...


@dataclass(frozen=True)
class AttachmentRetrievalScope:
    knowledge_base_ids: tuple[str, ...] = ()
    document_ids: tuple[str, ...] = ()
    filenames: tuple[str, ...] = ()


AgentFactory = Callable[
    [Callable[[str], None], AttachmentRetrievalScope], RunnableAgent
]
AttachmentGatewayFactory = Callable[[AttachmentKind], AttachmentGateway]
_MCP_TOOLS_UNSET = object()
LOCAL_AREA_TOOL_NAME = "calculate_material_quantities"
MCP_AREA_TOOL_SUFFIX = "__area_calculate"


class ChatRunCancelled(RuntimeError):
    pass


class _LazyTtMcpToolCache:
    """在一个 Web 应用实例内缓存已发现的 MCP 工具。"""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._tools: LazyTtMcpTools | None = None
        self._config_error = False
        try:
            self._config = LazyTtMcpConfig.from_env()
        except LazyTtMcpError:
            self._config = None
            self._config_error = True

    def get(self) -> LazyTtMcpTools | None:
        if self._config_error:
            raise LazyTtMcpError("LazyTT MCP 配置无效。")
        if self._config is None:
            return None
        if self._tools is not None:
            return self._tools
        with self._lock:
            if self._tools is None:
                self._tools = LazyTtMcpTools.from_config(self._config)
        return self._tools

    def status(self) -> dict[str, object]:
        if self._config_error:
            return {
                "enabled": True,
                "configured": False,
                "connected": False,
                "tools": [],
            }
        if self._config is None:
            return {
                "enabled": False,
                "configured": False,
                "connected": False,
                "tools": [],
            }
        try:
            tools = self.get()
        except LazyTtMcpError:
            return {
                "enabled": True,
                "configured": True,
                "connected": False,
                "tools": [],
            }
        assert tools is not None
        return {
            "enabled": True,
            "configured": True,
            "connected": True,
            "tools": list(tools.tool_definitions),
        }


class ChatRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    message: str = Field(min_length=1, max_length=20_000)
    conversation_id: UUID | None = None

    @field_validator("message")
    @classmethod
    def reject_blank_message(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("message cannot be blank")
        return value


class ChatStreamRequest(ChatRequest):
    request_id: UUID


class ChatCancellationResponse(BaseModel):
    status: Literal["cancellation_requested"]


class GenerationUiArtifactResponse(BaseModel):
    type: Literal["generation_ui"] = "generation_ui"
    conversation_id: UUID
    message_id: str
    artifact_id: UUID
    scenario: str
    text: str
    spec: dict[str, Any]
    data: dict[str, Any]


class MessageResourceResponse(BaseModel):
    resource_id: UUID
    kind: Literal["artifact", "source", "evidence"]
    title: str
    subtitle: str | None = None
    href: str | None = None
    media_type: str | None = None


class ChatResponse(BaseModel):
    conversation_id: UUID
    answer: str
    artifacts: list[GenerationUiArtifactResponse] = Field(default_factory=list)
    resources: list[MessageResourceResponse] = Field(default_factory=list)
    trace: list[str]
    duration_ms: int
    context: dict[str, object] | None


class GenerationUiEventRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    conversation_id: UUID
    message_id: int
    artifact_id: UUID
    scenario: str = Field(min_length=1, max_length=80)
    event_type: Literal["action", "submit", "confirm"] | None = None
    action_id: str = Field(min_length=1, max_length=80)
    arguments: dict[str, Any]
    idempotency_key: UUID


class GenerationUiEventResponse(BaseModel):
    success: bool
    message: str
    status: str
    data_patch: dict[str, Any]
    form_patch: dict[str, Any] = Field(default_factory=dict)
    spec_patch: dict[str, Any] = Field(default_factory=dict)
    agent_message: str
    continuation: ChatResponse | None = None


class ConversationMessage(BaseModel):
    role: Literal["user", "assistant"]
    content: str
    status: Literal["pending", "stopped", "failed"] | None = None
    artifacts: list[GenerationUiArtifactResponse] | None = None
    resources: list[MessageResourceResponse] | None = None
    trace: list[str] | None = None
    duration_ms: int | None = None
    context: dict[str, object] | None = None


class AttachmentResponse(BaseModel):
    attachment_id: UUID
    filename: str
    content_type: str
    size_bytes: int
    status: Literal["UPLOADING", "PROCESSING", "READY", "FAILED"]
    progress: float
    error_message: str | None = None


class ContentImageUploadResponse(BaseModel):
    filename: str
    path: str
    size_bytes: int


class ConversationResponse(BaseModel):
    conversation_id: UUID
    messages: list[ConversationMessage]
    attachments: list[AttachmentResponse]


class ConversationCreatedResponse(BaseModel):
    conversation_id: UUID


class ConversationSummaryResponse(BaseModel):
    conversation_id: UUID
    title: str
    updated_at: str
    message_count: int


class ConversationListResponse(BaseModel):
    conversations: list[ConversationSummaryResponse]


class ConversationRenameRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: str = Field(min_length=1, max_length=80)

    @field_validator("title")
    @classmethod
    def normalize_title(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("title must not be blank")
        return normalized


class ConversationRenameResponse(BaseModel):
    conversation_id: UUID
    title: str


class AttachmentListResponse(BaseModel):
    conversation_id: UUID
    attachments: list[AttachmentResponse]


def _default_agent_factory(
    trace: Callable[[str], None],
    attachment_scope: AttachmentRetrievalScope,
    *,
    mcp_tools: LazyTtMcpTools | None | object = _MCP_TOOLS_UNSET,
    owner_store: SQLiteConversationStore | None = None,
) -> AgentLoop:
    definitions = dict(TOOL_DEFINITIONS)
    handlers = dict(DEFAULT_TOOL_HANDLERS)
    trusted_results = TrustedPresentationResultRegistry()
    presenter = GenerationUiPresenter(trusted_results)
    analyze_handler = handlers["analyze_spreadsheet"]
    briefing_handler = handlers["generate_business_briefing"]
    owner_briefing_handler = handlers[OWNER_BRIEFING_TOOL_NAME]
    handlers["analyze_spreadsheet"] = lambda path: trusted_results.attach_analysis_result(
        path, analyze_handler(path)
    )
    handlers["generate_business_briefing"] = (
        lambda path: trusted_results.attach_analysis_result(path, briefing_handler(path))
    )
    def owner_briefing_with_review(report_date: str) -> dict[str, Any]:
        result = owner_briefing_handler(report_date)
        briefing = result.get("briefing")
        if owner_store is not None and isinstance(briefing, dict):
            issue_keys = [
                item["issue_key"]
                for item in briefing.get("anomalies", [])
                if isinstance(item, dict) and isinstance(item.get("issue_key"), str)
            ]
            result = attach_owner_next_day_review(
                result,
                owner_store.owner_next_day_review(
                    report_date, current_issue_keys=issue_keys
                ),
            )
        result = attach_owner_briefing_presentation(trusted_results, result)
        presentation = result.get("presentation")
        if not isinstance(presentation, dict):
            return result
        ui_result = presenter.present(
            OWNER_BRIEFING_SCENARIO,
            f"{result.get('company_name', '')} {report_date} 老板早报".strip(),
            presentation.get("result_ref", ""),
        )
        return compact_owner_briefing_result(result, ui_result=ui_result)

    handlers[OWNER_BRIEFING_TOOL_NAME] = owner_briefing_with_review
    content_tools = ContentOperationTools(trusted_results)

    def auto_present_content(
        handler: Callable[..., dict[str, Any]],
        scenario: str,
        title: str,
    ) -> Callable[..., dict[str, Any]]:
        def wrapped(*args: Any, **kwargs: Any) -> dict[str, Any]:
            result = handler(*args, **kwargs)
            presentation = result.get("presentation")
            if not isinstance(presentation, dict):
                return result
            ui_result = presenter.present(
                scenario,
                title,
                presentation.get("result_ref", ""),
            )
            return compact_content_operation_result(result, ui_result=ui_result)

        return wrapped

    raw_content_handlers = content_tools.handlers()
    handlers[CONTENT_WEEKLY_PLAN_TOOL_NAME] = auto_present_content(
        raw_content_handlers[CONTENT_WEEKLY_PLAN_TOOL_NAME],
        CONTENT_WEEKLY_PLAN_SCENARIO,
        "柒昱美学设计公司获客内容周计划",
    )
    handlers[CONTENT_PERFORMANCE_TOOL_NAME] = auto_present_content(
        raw_content_handlers[CONTENT_PERFORMANCE_TOOL_NAME],
        CONTENT_PERFORMANCE_SCENARIO,
        "柒昱美学设计公司内容表现复盘",
    )
    contract_search = None
    if attachment_scope.document_ids:
        contract_search = partial(
            search_knowledge_base,
            knowledge_base_ids=attachment_scope.knowledge_base_ids,
            document_ids=attachment_scope.document_ids,
        )
        handlers["search_knowledge_base"] = contract_search
    renovation_tools = RenovationScenarioTools(
        trusted_results,
        contract_search=contract_search,
    )
    handlers.update(renovation_tools.handlers())
    handlers["prepare_generation_ui_input"] = partial(
        prepare_generation_ui_input,
        trusted_results,
    )
    handlers["present_generation_ui"] = presenter.present
    if mcp_tools is _MCP_TOOLS_UNSET:
        mcp_tools = build_lazytt_mcp_tools_from_env()
    mcp_budgets = None
    if mcp_tools is not None:
        if any(
            name.startswith("mcp__") and name.endswith(MCP_AREA_TOOL_SUFFIX)
            for name in mcp_tools.tool_definitions
        ):
            definitions.pop(LOCAL_AREA_TOOL_NAME, None)
            handlers.pop(LOCAL_AREA_TOOL_NAME, None)
        collisions = set(definitions) & set(mcp_tools.tool_definitions)
        if collisions:
            names = ", ".join(sorted(collisions))
            raise RuntimeError(f"MCP tool name collision: {names}")
        definitions.update(mcp_tools.tool_definitions)
        handlers.update(
            McpMaterialGenerationUiAdapter(trusted_results).wrap_handlers(
                mcp_tools.tool_handlers
            )
        )
        if supports_material_estimate_input(mcp_tools.tool_definitions):
            definitions[PREPARE_MATERIAL_ESTIMATE_INPUT_TOOL_NAME] = (
                PREPARE_MATERIAL_ESTIMATE_INPUT_TOOL
            )
            handlers[PREPARE_MATERIAL_ESTIMATE_INPUT_TOOL_NAME] = partial(
                prepare_material_estimate_input,
                trusted_results,
            )
        mcp_budgets = mcp_tools.tool_budgets
    agent = AgentLoop(
        LLMClient.from_env(),
        trace=trace,
        memory=build_memory_client_from_env(),
        tool_definitions=definitions,
        tool_handlers=handlers,
        tool_budgets=mcp_budgets,
        attachment_filenames=attachment_scope.filenames,
    )
    agent.generation_ui_artifacts = presenter.artifacts
    return agent


def _default_attachment_gateway_factory(kind: AttachmentKind) -> KnowledgeBaseClient:
    return KnowledgeBaseClient(attachment_knowledge_base_config(kind))


def create_app(
    agent_factory: AgentFactory | None = None,
    *,
    conversation_store: SQLiteConversationStore | None = None,
    attachment_gateway_factory: AttachmentGatewayFactory | None = None,
    owner_reminder_scheduler_enabled: bool = False,
    owner_reminder_interval_seconds: float = 60.0,
) -> FastAPI:
    mcp_cache = _LazyTtMcpToolCache() if agent_factory is None else None
    if agent_factory is None:
        assert mcp_cache is not None

        def factory(
            trace: Callable[[str], None],
            attachment_scope: AttachmentRetrievalScope,
        ) -> RunnableAgent:
            try:
                discovered_mcp_tools = mcp_cache.get()
            except LazyTtMcpError:
                trace("LazyTT MCP：当前不可用，已降级为本地工具。")
                discovered_mcp_tools = None
            return _default_agent_factory(
                trace,
                attachment_scope,
                mcp_tools=discovered_mcp_tools,
                owner_store=get_store(),
            )

    else:
        factory = agent_factory
    gateway_factory = (
        attachment_gateway_factory or _default_attachment_gateway_factory
    )
    store = conversation_store
    active_chat_cancellations: dict[str, threading.Event] = {}
    active_chat_lock = threading.Lock()
    owner_reminder_stop = threading.Event()
    owner_reminder_state: dict[str, str | None] = {
        "last_scan_at": None,
        "last_error": None,
    }

    def get_store() -> SQLiteConversationStore:
        nonlocal store
        if store is None:
            raw_path = os.getenv(
                "CONVERSATION_DB_PATH",
                ".local/conversations.sqlite3",
            ).strip()
            path = Path(raw_path).expanduser()
            if not path.is_absolute():
                path = PROJECT_ROOT / path
            store = SQLiteConversationStore(path)
        return store

    app = FastAPI(
        title="Mini Agent Web",
        version="0.1.0",
        description="Local chat interface for the TestAgent demo.",
    )
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

    if owner_reminder_scheduler_enabled:
        if owner_reminder_interval_seconds <= 0:
            raise ValueError("owner_reminder_interval_seconds must be positive")

        def scan_owner_reminders() -> None:
            while not owner_reminder_stop.is_set():
                try:
                    result = get_store().scan_owner_briefing_reminders()
                    owner_reminder_state["last_scan_at"] = str(result["scanned_at"])
                    owner_reminder_state["last_error"] = None
                except Exception as exc:  # pragma: no cover - defensive service loop
                    owner_reminder_state["last_error"] = type(exc).__name__
                owner_reminder_stop.wait(owner_reminder_interval_seconds)

        reminder_thread: threading.Thread | None = None

        def start_owner_reminders() -> None:
            nonlocal reminder_thread
            owner_reminder_stop.clear()
            reminder_thread = threading.Thread(
                target=scan_owner_reminders,
                name="owner-briefing-reminders",
                daemon=True,
            )
            reminder_thread.start()

        def stop_owner_reminders() -> None:
            owner_reminder_stop.set()
            if reminder_thread is not None:
                reminder_thread.join(timeout=1)

        app.router.add_event_handler("startup", start_owner_reminders)
        app.router.add_event_handler("shutdown", stop_owner_reminders)

    @app.get("/", include_in_schema=False)
    def index() -> FileResponse:
        return FileResponse(STATIC_DIR / "index.html")

    @app.get("/api/health")
    def health() -> dict[str, object]:
        model_configured = _model_configured()
        code_configured = _agsx_code_configured()
        browser_configured = code_configured and _configured("AGSX_BROWSER_TEMPLATE")
        search_configured = _configured("TAVILY_API_KEY")
        memory_enabled, memory_configured = memory_configuration_status()
        rag_enabled, rag_configured = knowledge_base_configuration_status()
        rag_attachments_configured = attachment_knowledge_base_configuration_status()
        mcp_status = (
            mcp_cache.status()
            if mcp_cache is not None
            else {
                "enabled": False,
                "configured": False,
                "connected": False,
                "tools": [],
            }
        )
        core_ready = all(
            (
                model_configured,
                code_configured,
                browser_configured,
                search_configured,
                memory_configured,
                rag_configured,
            )
        )
        mcp_ready = not mcp_status["enabled"] or mcp_status["connected"]
        return {
            "status": (
                "ready" if core_ready and mcp_ready else "configuration_required"
            ),
            "model_configured": model_configured,
            "code_sandbox_configured": code_configured,
            "browser_sandbox_configured": browser_configured,
            "search_configured": search_configured,
            "memory_enabled": memory_enabled,
            "memory_configured": memory_configured,
            "rag_enabled": rag_enabled,
            "rag_configured": rag_configured,
            "rag_attachments_configured": rag_attachments_configured,
            "mcp_enabled": mcp_status["enabled"],
            "mcp_configured": mcp_status["configured"],
            "mcp_connected": mcp_status["connected"],
            "mcp_tools": mcp_status["tools"],
            "conversation_store": "sqlite",
            "owner_reminder_scheduler_enabled": owner_reminder_scheduler_enabled,
            "owner_reminder_last_scan_at": owner_reminder_state["last_scan_at"],
            "owner_reminder_last_error": owner_reminder_state["last_error"],
            "tools": [
                *(
                    name
                    for name in TOOL_DEFINITIONS
                    if not (
                        name == LOCAL_AREA_TOOL_NAME
                        and any(
                            tool_name.startswith("mcp__")
                            and tool_name.endswith(MCP_AREA_TOOL_SUFFIX)
                            for tool_name in mcp_status["tools"]
                        )
                    )
                ),
                *(
                    [PREPARE_MATERIAL_ESTIMATE_INPUT_TOOL_NAME]
                    if supports_material_estimate_input(mcp_status["tools"])
                    else []
                ),
                *mcp_status["tools"],
            ],
        }

    @app.post("/api/conversations", response_model=ConversationCreatedResponse)
    def create_conversation() -> ConversationCreatedResponse:
        return ConversationCreatedResponse(conversation_id=UUID(get_store().create()))

    @app.get("/api/owner/reminders")
    def owner_reminders(
        limit: int = Query(default=50, ge=1, le=100),
    ) -> dict[str, Any]:
        return {"reminders": get_store().owner_briefing_reminders(limit=limit)}

    @app.post("/api/owner/demo-reset")
    def reset_owner_demo() -> dict[str, int | bool]:
        return get_store().reset_owner_briefing_demo()

    @app.get("/api/conversations", response_model=ConversationListResponse)
    def list_conversations(
        limit: int = Query(default=50, ge=1, le=100),
    ) -> ConversationListResponse:
        return ConversationListResponse(
            conversations=[
                _conversation_summary_response(item)
                for item in get_store().conversation_summaries(limit=limit)
            ]
        )

    @app.patch(
        "/api/conversations/{conversation_id}",
        response_model=ConversationRenameResponse,
    )
    def rename_conversation(
        conversation_id: UUID,
        request: ConversationRenameRequest,
    ) -> ConversationRenameResponse:
        identifier = str(conversation_id)
        try:
            get_store().rename(identifier, request.title)
        except ConversationNotFoundError as exc:
            raise HTTPException(
                status_code=404,
                detail={"error_type": "CONVERSATION_NOT_FOUND"},
            ) from exc
        return ConversationRenameResponse(
            conversation_id=conversation_id,
            title=request.title,
        )

    @app.post(
        "/api/generation/event",
        response_model=GenerationUiEventResponse,
    )
    @app.post(
        "/api/generation/action",
        response_model=GenerationUiEventResponse,
        include_in_schema=False,
    )
    def execute_generation_ui_event(
        request: GenerationUiEventRequest,
    ) -> GenerationUiEventResponse:
        conversation_id = str(request.conversation_id)
        artifact_id = str(request.artifact_id)
        try:
            artifact = get_store().generation_ui_artifact(
                conversation_id, artifact_id
            )
        except GenerationUiArtifactNotFoundError as exc:
            raise HTTPException(
                status_code=404,
                detail={
                    "error_type": "GENERATION_UI_ARTIFACT_NOT_FOUND",
                    "message": "当前会话中不存在这个 Generation UI。",
                },
            ) from exc
        if artifact.message_id != request.message_id or artifact.scenario != request.scenario:
            raise HTTPException(
                status_code=409,
                detail={
                    "error_type": "GENERATION_UI_ACTION_CONTEXT_MISMATCH",
                    "message": "Action 与当前 UI 消息不匹配。",
                },
            )
        action = _generation_ui_action(artifact.spec, request.action_id)
        if action is None:
            raise HTTPException(
                status_code=400,
                detail={
                    "error_type": "UNKNOWN_GENERATION_UI_ACTION",
                    "message": "该 UI 不支持请求的 Action。",
                },
            )
        declared_event_type = action.get("event_type")
        if declared_event_type not in {"action", "submit", "confirm"}:
            declared_event_type = "confirm" if action.get("confirm") is True else "action"
        if request.event_type is not None and request.event_type != declared_event_type:
            raise HTTPException(
                status_code=409,
                detail={
                    "error_type": "GENERATION_UI_EVENT_TYPE_MISMATCH",
                    "message": "事件类型与当前 UI Action 不匹配。",
                },
            )
        try:
            if (
                artifact.scenario == "renovation_project_create"
                and request.action_id == "confirm_create_project"
            ):
                action_schema = action.get("arguments_schema") or {
                    "type": "object",
                    "properties": {
                        "address": {
                            "type": "string",
                            "minLength": 5,
                            "maxLength": 200,
                        }
                    },
                    "required": ["address"],
                    "additionalProperties": False,
                }
                normalized_arguments = _validate_action_arguments(
                    action_schema,
                    request.arguments,
                )
                normalized_address = normalized_arguments["address"]
                city = artifact.data.get("city")
                if not isinstance(city, str) or not any(
                    value and value in normalized_address
                    for value in (city, city.removesuffix("市"))
                ):
                    raise HTTPException(
                        status_code=422,
                        detail={
                            "error_type": "PROJECT_ADDRESS_CITY_MISMATCH",
                            "message": "施工地址必须与项目城市一致。",
                        },
                    )
                response = get_store().confirm_renovation_project(
                    conversation_id,
                    artifact_id,
                    address=normalized_address,
                    idempotency_key=str(request.idempotency_key),
                )
            elif (
                artifact.scenario == OWNER_BRIEFING_SCENARIO
                and request.action_id == "transition_owner_briefing_task"
            ):
                normalized_arguments = _validate_action_arguments(
                    action.get("arguments_schema"),
                    request.arguments,
                )
                response = get_store().transition_owner_briefing_task(
                    conversation_id,
                    artifact_id,
                    arguments=normalized_arguments,
                    idempotency_key=str(request.idempotency_key),
                )
            elif (
                artifact.scenario == CONTENT_WEEKLY_PLAN_SCENARIO
                and request.action_id == "review_content_plan"
            ):
                normalized_arguments = _validate_action_arguments(
                    action.get("arguments_schema"),
                    request.arguments,
                )
                try:
                    image_paths = (
                        validate_content_image_paths(
                            normalized_arguments.get("image_paths_text", "")
                        )
                        if normalized_arguments["decision"] == "确认内容"
                        else []
                    )
                except ValueError as exc:
                    raise HTTPException(
                        status_code=422,
                        detail={
                            "error_type": "INVALID_CONTENT_IMAGES",
                            "message": str(exc),
                        },
                    ) from exc
                response = get_store().review_content_plan(
                    conversation_id,
                    artifact_id,
                    arguments={
                        **normalized_arguments,
                        "image_paths": image_paths,
                    },
                    idempotency_key=str(request.idempotency_key),
                )
            elif (
                artifact.scenario == CONTENT_WEEKLY_PLAN_SCENARIO
                and request.action_id == "save_xiaohongshu_draft"
            ):
                normalized_arguments = _validate_action_arguments(
                    action.get("arguments_schema"),
                    request.arguments,
                )
                rpa_arguments = {"content_id": normalized_arguments["content_id"]}
                prepared = get_store().prepare_content_xiaohongshu_rpa(
                    conversation_id,
                    artifact_id,
                    arguments=rpa_arguments,
                    idempotency_key=str(request.idempotency_key),
                )
                existing_response = prepared.get("existing_response")
                if isinstance(existing_response, dict):
                    response = existing_response
                else:
                    rpa_result = save_xiaohongshu_draft(
                        title=prepared["title"],
                        body=prepared["body"],
                        tags=prepared["tags"],
                        image_paths=prepared["image_paths"],
                    )
                    response = get_store().finish_content_xiaohongshu_rpa(
                        conversation_id,
                        artifact_id,
                        arguments=rpa_arguments,
                        idempotency_key=str(request.idempotency_key),
                        rpa_result=rpa_result,
                    )
            elif (
                artifact.scenario == "structured_input"
                and request.action_id == "submit_structured_input"
            ):
                normalized_arguments = _validate_action_arguments(
                    action.get("arguments_schema"),
                    request.arguments,
                )
                response, should_continue = get_store().submit_generation_ui_input(
                    conversation_id,
                    artifact_id,
                    arguments=normalized_arguments,
                    idempotency_key=str(request.idempotency_key),
                )
                if not should_continue and response.get("status") == "processing":
                    raise HTTPException(
                        status_code=409,
                        detail={
                            "error_type": "GENERATION_UI_CONTINUATION_IN_PROGRESS",
                            "message": "这次表单提交仍在处理中。",
                        },
                    )
                if should_continue:
                    try:
                        continuation = execute_chat(
                            ChatRequest(
                                message="继续完成此前由 Generation UI 收集参数的任务。",
                                conversation_id=request.conversation_id,
                            ),
                            generation_ui_event={
                                "type": "generation_ui_submission",
                                "scenario": artifact.scenario,
                                "action_id": request.action_id,
                                "artifact_id": artifact_id,
                                "arguments": normalized_arguments,
                            },
                        )
                    except Exception:
                        get_store().finish_generation_ui_input(
                            conversation_id,
                            artifact_id,
                            idempotency_key=str(request.idempotency_key),
                            response={
                                **response,
                                "success": False,
                                "status": "failed",
                                "message": "参数已提交，但 Agent 续跑失败。",
                                "agent_message": "Agent 续跑失败，请重试。",
                            },
                        )
                        raise
                    response = {
                        **response,
                        "status": "completed",
                        "message": f"已提交 {len(normalized_arguments)} 项参数，计算完成。",
                        "agent_message": "Agent 已根据表单参数完成处理。",
                        "continuation": continuation.model_dump(
                            mode="json", exclude_none=True
                        ),
                    }
                    get_store().finish_generation_ui_input(
                        conversation_id,
                        artifact_id,
                        idempotency_key=str(request.idempotency_key),
                        response=response,
                    )
            else:
                raise HTTPException(
                    status_code=400,
                    detail={
                        "error_type": "UNKNOWN_GENERATION_UI_ACTION",
                        "message": "该 UI Action 没有注册后端处理器。",
                    },
                )
        except GenerationUiActionConflictError as exc:
            raise HTTPException(
                status_code=409,
                detail={
                    "error_type": "GENERATION_UI_ACTION_CONFLICT",
                    "message": str(exc),
                },
            ) from exc
        return GenerationUiEventResponse(**response)

    @app.get(
        "/api/conversations/{conversation_id}",
        response_model=ConversationResponse,
        response_model_exclude_none=True,
    )
    def get_conversation(conversation_id: UUID) -> ConversationResponse:
        try:
            identifier = str(conversation_id)
            message_records = get_store().message_records(identifier)
            artifacts = get_store().generation_ui_artifacts(identifier)
            resources = get_store().conversation_resources(identifier)
        except ConversationNotFoundError as exc:
            raise HTTPException(
                status_code=404,
                detail={"error_type": "CONVERSATION_NOT_FOUND"},
            ) from exc
        artifacts_by_message: dict[int, list[GenerationUiArtifactResponse]] = {}
        for artifact in artifacts:
            artifacts_by_message.setdefault(artifact.message_id, []).append(
                _generation_ui_artifact_response(artifact)
            )
        resources_by_message: dict[int, list[MessageResourceResponse]] = {}
        for resource in resources:
            resources_by_message.setdefault(resource.message_id, []).append(
                _message_resource_response(resource)
            )
        return ConversationResponse(
            conversation_id=conversation_id,
            messages=[
                ConversationMessage(
                    role=item.role,
                    content=item.content,
                    status=item.status if item.status != "completed" else None,
                    artifacts=artifacts_by_message.get(item.message_id),
                    resources=resources_by_message.get(item.message_id),
                    trace=list(item.trace) or None,
                    duration_ms=item.duration_ms,
                    context=item.context,
                )
                for item in message_records
            ],
            attachments=[
                _attachment_response(item)
                for item in get_store().attachments(str(conversation_id))
            ],
        )

    @app.get("/api/resources/{resource_id}", include_in_schema=False)
    def download_resource(resource_id: UUID) -> FileResponse:
        try:
            resource = get_store().resource(str(resource_id))
        except ConversationResourceNotFoundError as exc:
            raise HTTPException(
                status_code=404,
                detail={"error_type": "RESOURCE_NOT_FOUND"},
            ) from exc
        if resource.path is None:
            raise HTTPException(
                status_code=404,
                detail={"error_type": "RESOURCE_FILE_NOT_FOUND"},
            )
        path = _resolve_artifact_path(resource.path)
        if path is None:
            raise HTTPException(
                status_code=404,
                detail={"error_type": "RESOURCE_FILE_NOT_FOUND"},
            )
        return FileResponse(
            path,
            media_type=resource.media_type,
            filename=path.name,
        )

    @app.delete("/api/conversations/{conversation_id}", status_code=204)
    def delete_conversation(conversation_id: UUID) -> Response:
        identifier = str(conversation_id)
        try:
            attachments = get_store().attachments(identifier)
        except ConversationNotFoundError as exc:
            raise HTTPException(
                status_code=404,
                detail={"error_type": "CONVERSATION_NOT_FOUND"},
            ) from exc
        _delete_unshared_documents(
            get_store(),
            attachments,
            gateway_factory,
        )
        get_store().delete(identifier)
        return Response(status_code=204)

    @app.post(
        "/api/content-assets/images",
        response_model=ContentImageUploadResponse,
        status_code=201,
    )
    async def upload_content_image(
        request: Request,
        filename: str = Query(min_length=1, max_length=255),
    ) -> ContentImageUploadResponse:
        safe_filename = _validate_filename(filename)
        if Path(safe_filename).suffix.lower() not in IMAGE_EXTENSIONS:
            raise HTTPException(
                status_code=415,
                detail={
                    "error_type": "UNSUPPORTED_CONTENT_IMAGE_TYPE",
                    "message": "图片仅支持 JPEG、PNG 或 WebP。",
                },
            )
        content_length = request.headers.get("content-length")
        if content_length is not None:
            try:
                declared_size = int(content_length)
            except ValueError as exc:
                raise HTTPException(
                    status_code=400,
                    detail={"error_type": "INVALID_CONTENT_LENGTH"},
                ) from exc
            if declared_size > CONTENT_IMAGE_MAX_BYTES:
                raise HTTPException(
                    status_code=413,
                    detail={
                        "error_type": "CONTENT_IMAGE_TOO_LARGE",
                        "message": "单张图片不能超过 32MB。",
                    },
                )
        content = bytearray()
        async for chunk in request.stream():
            content.extend(chunk)
            if len(content) > CONTENT_IMAGE_MAX_BYTES:
                raise HTTPException(
                    status_code=413,
                    detail={
                        "error_type": "CONTENT_IMAGE_TOO_LARGE",
                        "message": "单张图片不能超过 32MB。",
                    },
                )
        try:
            path = await run_in_threadpool(store_content_image, bytes(content))
        except ValueError as exc:
            raise HTTPException(
                status_code=422,
                detail={
                    "error_type": "INVALID_CONTENT_IMAGE",
                    "message": str(exc),
                },
            ) from exc
        return ContentImageUploadResponse(
            filename=safe_filename,
            path=path,
            size_bytes=len(content),
        )

    @app.post(
        "/api/conversations/{conversation_id}/attachments",
        response_model=AttachmentResponse,
        status_code=202,
    )
    async def upload_attachment(
        conversation_id: UUID,
        request: Request,
        filename: str = Query(min_length=1, max_length=255),
    ) -> AttachmentResponse:
        safe_filename = _validate_filename(filename)
        attachment_kind = _attachment_kind(safe_filename)
        try:
            existing_attachments = get_store().attachments(str(conversation_id))
        except ConversationNotFoundError as exc:
            raise HTTPException(
                status_code=404,
                detail={"error_type": "CONVERSATION_NOT_FOUND"},
            ) from exc
        if len(existing_attachments) >= 100:
            raise HTTPException(
                status_code=409,
                detail={"error_type": "ATTACHMENT_LIMIT_REACHED"},
            )
        max_bytes = _attachment_max_bytes()
        content_length = request.headers.get("content-length")
        if content_length is not None:
            try:
                declared_size = int(content_length)
            except ValueError as exc:
                raise HTTPException(status_code=400, detail={"error_type": "INVALID_CONTENT_LENGTH"}) from exc
            if declared_size > max_bytes:
                raise HTTPException(status_code=413, detail={"error_type": "ATTACHMENT_TOO_LARGE"})
        if attachment_kind == "media" and request.headers.get(
            "x-user-media-consent", ""
        ).lower() != "true":
            raise HTTPException(
                status_code=400,
                detail={"error_type": "MEDIA_CONSENT_REQUIRED"},
            )
        content_type = _validate_content_type(
            request.headers.get("content-type", "application/octet-stream")
        )
        size_bytes = 0
        with tempfile.SpooledTemporaryFile(max_size=2 * 1024 * 1024, mode="w+b") as upload:
            async for chunk in request.stream():
                size_bytes += len(chunk)
                if size_bytes > max_bytes:
                    raise HTTPException(
                        status_code=413,
                        detail={"error_type": "ATTACHMENT_TOO_LARGE"},
                    )
                upload.write(chunk)
            if size_bytes == 0:
                raise HTTPException(status_code=400, detail={"error_type": "EMPTY_ATTACHMENT"})
            upload.seek(0)
            gateway = _open_attachment_gateway(gateway_factory, attachment_kind)
            try:
                try:
                    attachment = get_store().create_attachment(
                        str(conversation_id),
                        knowledge_base_kind=attachment_kind,
                        knowledge_base_id=gateway.knowledge_base_id,
                        filename=safe_filename,
                        content_type=content_type,
                        size_bytes=size_bytes,
                    )
                except ConversationNotFoundError as exc:
                    raise HTTPException(
                        status_code=404,
                        detail={"error_type": "CONVERSATION_NOT_FOUND"},
                    ) from exc
                result = await run_in_threadpool(
                    gateway.upload_document,
                    upload,
                    filename=safe_filename,
                    content_type=content_type,
                    attachment_id=attachment.attachment_id,
                    conversation_id=str(conversation_id),
                )
            finally:
                gateway.close()
        if not result.get("success"):
            failed = get_store().update_attachment_status(
                str(conversation_id),
                attachment.attachment_id,
                status="FAILED",
                progress=0,
                error_message=str(result.get("error_type") or "RAG_UPLOAD_FAILED"),
            )
            return _attachment_response(failed)
        uploaded = get_store().mark_attachment_uploaded(
            str(conversation_id),
            attachment.attachment_id,
            document_id=str(result["document_id"]),
            job_id=str(result["job_id"]),
        )
        return _attachment_response(uploaded)

    @app.get(
        "/api/conversations/{conversation_id}/attachments",
        response_model=AttachmentListResponse,
    )
    def list_attachments(conversation_id: UUID) -> AttachmentListResponse:
        identifier = str(conversation_id)
        try:
            attachments = get_store().attachments(identifier)
        except ConversationNotFoundError as exc:
            raise HTTPException(
                status_code=404,
                detail={"error_type": "CONVERSATION_NOT_FOUND"},
            ) from exc
        refreshed = _refresh_attachments(
            get_store(),
            identifier,
            attachments,
            gateway_factory,
        )
        return AttachmentListResponse(
            conversation_id=conversation_id,
            attachments=[_attachment_response(item) for item in refreshed],
        )

    @app.delete(
        "/api/conversations/{conversation_id}/attachments/{attachment_id}",
        status_code=204,
    )
    def delete_attachment(conversation_id: UUID, attachment_id: UUID) -> Response:
        identifier = str(conversation_id)
        try:
            attachment = get_store().attachment(identifier, str(attachment_id))
        except AttachmentNotFoundError as exc:
            raise HTTPException(
                status_code=404,
                detail={"error_type": "ATTACHMENT_NOT_FOUND"},
            ) from exc
        _delete_unshared_documents(
            get_store(),
            [attachment],
            gateway_factory,
        )
        get_store().remove_attachment(identifier, str(attachment_id))
        return Response(status_code=204)

    def execute_chat(
        request: ChatRequest,
        on_trace: Callable[[str], None] | None = None,
        is_cancelled: Callable[[], bool] | None = None,
        on_answer_delta: Callable[[str], None] | None = None,
        on_answer_reset: Callable[[], None] | None = None,
        on_turn_started: Callable[[str], None] | None = None,
        generation_ui_event: Mapping[str, Any] | None = None,
    ) -> ChatResponse:
        trace: list[str] = []
        answer_parts: list[str] = []
        started = time.perf_counter()

        def ensure_running() -> None:
            if is_cancelled is not None and is_cancelled():
                raise ChatRunCancelled("用户已停止本次任务。")

        def record_trace(message: str) -> None:
            ensure_running()
            trace.append(message)
            if on_trace is not None:
                on_trace(message)

        conversation_id = str(request.conversation_id) if request.conversation_id else None
        if conversation_id is None:
            conversation_id = get_store().create()
        try:
            history = get_store().messages(conversation_id)
        except ConversationNotFoundError as exc:
            raise HTTPException(
                status_code=404,
                detail={"error_type": "CONVERSATION_NOT_FOUND"},
            ) from exc
        attachments = get_store().attachments(conversation_id)
        if any(item.status in {"UPLOADING", "PROCESSING"} for item in attachments):
            attachments = _refresh_attachments(
                get_store(),
                conversation_id,
                attachments,
                gateway_factory,
            )
        if any(item.status in {"UPLOADING", "PROCESSING"} for item in attachments):
            raise HTTPException(
                status_code=409,
                detail={
                    "error_type": "ATTACHMENTS_PROCESSING",
                    "message": "附件仍在解析，请等待状态变为已可用。",
                },
            )
        ready_attachments = [item for item in attachments if item.status == "READY"]
        if any(
            item.knowledge_base_id is None or item.document_id is None
            for item in ready_attachments
        ):
            raise HTTPException(
                status_code=409,
                detail={
                    "error_type": "ATTACHMENT_ROUTE_MIGRATION_REQUIRED",
                    "message": "旧附件缺少知识库路由信息，请移除后重新上传。",
                },
            )
        attachment_scope = AttachmentRetrievalScope(
            knowledge_base_ids=tuple(
                dict.fromkeys(
                    item.knowledge_base_id
                    for item in ready_attachments
                    if item.knowledge_base_id is not None
                )
            ),
            document_ids=tuple(
                item.document_id
                for item in ready_attachments
                if item.document_id is not None
            ),
            filenames=tuple(item.filename for item in ready_attachments),
        )
        turn_id = (
            get_store().begin_turn(conversation_id, request.message)
            if generation_ui_event is None
            else None
        )
        if on_turn_started is not None:
            on_turn_started(conversation_id)

        def record_answer_delta(delta: str) -> None:
            ensure_running()
            answer_parts.append(delta)
            if on_answer_delta is not None:
                on_answer_delta(delta)

        def reset_answer() -> None:
            answer_parts.clear()
            if on_answer_reset is not None:
                on_answer_reset()

        try:
            ensure_running()
            agent = factory(record_trace, attachment_scope)
            run_parameters = inspect.signature(agent.run).parameters
            run_options: dict[str, Any] = {
                "history": history,
                "episode_id": conversation_id,
            }
            if generation_ui_event is not None:
                if "generation_ui_event" not in run_parameters:
                    raise RuntimeError("当前 Agent 不支持 Generation UI 结构化续跑。")
                run_options["generation_ui_event"] = generation_ui_event
            if "on_answer_delta" in run_parameters:
                run_options.update(
                    on_answer_delta=record_answer_delta,
                    on_answer_reset=reset_answer,
                    is_cancelled=is_cancelled,
                )
            answer = agent.run(request.message, **run_options)
            ensure_running()
        except (ChatRunCancelled, LLMStreamCancelled) as exc:
            if turn_id is not None:
                get_store().stop_turn(
                    conversation_id,
                    turn_id,
                    partial_answer="".join(answer_parts),
                    trace=trace,
                    duration_ms=round((time.perf_counter() - started) * 1000),
                )
            raise ChatRunCancelled("用户已停止本次任务。") from exc
        except HTTPException:
            if turn_id is not None:
                get_store().stop_turn(conversation_id, turn_id, status="failed")
            raise
        except LLMConnectionError as exc:
            if turn_id is not None:
                get_store().stop_turn(
                    conversation_id,
                    turn_id,
                    partial_answer="".join(answer_parts),
                    status="failed",
                    trace=trace,
                    duration_ms=round((time.perf_counter() - started) * 1000),
                )
            raise HTTPException(
                status_code=503,
                detail={
                    "error_type": "MODEL_CONNECTION_ERROR",
                    "message": str(exc),
                    "retryable": True,
                },
            ) from exc
        except RuntimeError as exc:
            if turn_id is not None:
                get_store().stop_turn(
                    conversation_id,
                    turn_id,
                    partial_answer="".join(answer_parts),
                    status="failed",
                    trace=trace,
                    duration_ms=round((time.perf_counter() - started) * 1000),
                )
            raise HTTPException(
                status_code=502,
                detail={"error_type": "AGENT_EXECUTION_ERROR", "message": str(exc)},
            ) from exc
        except Exception as exc:
            if turn_id is not None:
                get_store().stop_turn(
                    conversation_id,
                    turn_id,
                    partial_answer="".join(answer_parts),
                    status="failed",
                    trace=trace,
                    duration_ms=round((time.perf_counter() - started) * 1000),
                )
            raise HTTPException(
                status_code=500,
                detail={"error_type": "WEB_AGENT_ERROR", "message": str(exc)},
            ) from exc
        pending_artifacts = getattr(agent, "generation_ui_artifacts", [])
        if not isinstance(pending_artifacts, list):
            pending_artifacts = []
        pending_resources = _extract_message_resources(
            getattr(agent, "last_tool_results", [])
        )
        receipt = getattr(agent, "last_context_receipt", None)
        context = receipt.to_dict() if hasattr(receipt, "to_dict") else None
        duration_ms = round((time.perf_counter() - started) * 1000)
        try:
            with active_chat_lock:
                ensure_running()
                if turn_id is None:
                    assistant_message_id = get_store().append_assistant_response(
                        conversation_id,
                        answer,
                        ui_artifacts=pending_artifacts,
                        resources=pending_resources,
                        trace=trace,
                        duration_ms=duration_ms,
                        context=context,
                    )
                else:
                    assistant_message_id = get_store().complete_turn(
                        conversation_id,
                        turn_id,
                        answer,
                        ui_artifacts=pending_artifacts,
                        resources=pending_resources,
                        trace=trace,
                        duration_ms=duration_ms,
                        context=context,
                    )
        except ChatRunCancelled:
            if turn_id is not None:
                get_store().stop_turn(
                    conversation_id,
                    turn_id,
                    partial_answer="".join(answer_parts),
                    trace=trace,
                    duration_ms=duration_ms,
                )
            raise
        stored_artifacts = [
            item
            for item in get_store().generation_ui_artifacts(conversation_id)
            if item.message_id == assistant_message_id
        ]
        stored_resources = [
            item
            for item in get_store().conversation_resources(conversation_id)
            if item.message_id == assistant_message_id
        ]
        return ChatResponse(
            conversation_id=UUID(conversation_id),
            answer=answer,
            artifacts=[
                _generation_ui_artifact_response(item) for item in stored_artifacts
            ],
            resources=[
                _message_resource_response(item) for item in stored_resources
            ],
            trace=trace,
            duration_ms=duration_ms,
            context=context,
        )

    @app.post(
        "/api/chat",
        response_model=ChatResponse,
        response_model_exclude_none=True,
    )
    def chat(request: ChatRequest) -> ChatResponse:
        return execute_chat(request)

    @app.post("/api/chat/stream")
    async def chat_stream(request: ChatStreamRequest) -> StreamingResponse:
        request_id = str(request.request_id)
        cancellation = threading.Event()
        with active_chat_lock:
            if request_id in active_chat_cancellations:
                raise HTTPException(
                    status_code=409,
                    detail={"error_type": "CHAT_REQUEST_ALREADY_ACTIVE"},
                )
            active_chat_cancellations[request_id] = cancellation

        queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        event_loop = asyncio.get_running_loop()
        chat_request = ChatRequest(
            message=request.message,
            conversation_id=request.conversation_id,
        )
        started_conversation_id: str | None = None

        def emit(event: dict[str, Any]) -> None:
            event_loop.call_soon_threadsafe(queue.put_nowait, event)

        def emit_trace(message: str) -> None:
            emit({"type": "status", "message": message})

        def emit_answer_delta(delta: str) -> None:
            emit({"type": "answer_delta", "delta": delta})

        def emit_answer_reset() -> None:
            emit({"type": "answer_reset"})

        def emit_turn_started(conversation_id: str) -> None:
            nonlocal started_conversation_id
            started_conversation_id = conversation_id
            emit(
                {
                    "type": "accepted",
                    "request_id": request_id,
                    "conversation_id": conversation_id,
                }
            )

        async def run_chat() -> None:
            try:
                result = await run_in_threadpool(
                    partial(
                        execute_chat,
                        chat_request,
                        emit_trace,
                        cancellation.is_set,
                        emit_answer_delta,
                        emit_answer_reset,
                        emit_turn_started,
                    )
                )
                emit(
                    {
                        "type": "result",
                        "data": result.model_dump(mode="json", exclude_none=True),
                    }
                )
            except ChatRunCancelled:
                emit(
                    {
                        "type": "cancelled",
                        "message": "已停止生成。",
                        "conversation_id": started_conversation_id,
                    }
                )
            except HTTPException as exc:
                emit(
                    {
                        "type": "error",
                        "status": exc.status_code,
                        "detail": exc.detail,
                    }
                )
            except Exception as exc:
                emit(
                    {
                        "type": "error",
                        "status": 500,
                        "detail": {
                            "error_type": "WEB_AGENT_ERROR",
                            "message": str(exc),
                        },
                    }
                )
            finally:
                with active_chat_lock:
                    active_chat_cancellations.pop(request_id, None)

        async def stream_events():
            task = asyncio.create_task(run_chat())
            try:
                while True:
                    event = await queue.get()
                    yield json.dumps(event, ensure_ascii=False) + "\n"
                    if event.get("type") in {"result", "error", "cancelled"}:
                        break
            finally:
                if not task.done():
                    cancellation.set()

        return StreamingResponse(
            stream_events(),
            media_type="application/x-ndjson",
            headers={"Cache-Control": "no-store"},
        )

    @app.delete(
        "/api/chat/stream/{request_id}",
        response_model=ChatCancellationResponse,
        status_code=202,
    )
    def cancel_chat(request_id: UUID) -> ChatCancellationResponse:
        with active_chat_lock:
            cancellation = active_chat_cancellations.get(str(request_id))
            if cancellation is not None:
                cancellation.set()
        if cancellation is None:
            raise HTTPException(
                status_code=404,
                detail={"error_type": "CHAT_REQUEST_NOT_ACTIVE"},
            )
        return ChatCancellationResponse(status="cancellation_requested")

    return app


def _generation_ui_action(
    spec: dict[str, Any],
    action_id: str,
) -> dict[str, Any] | None:
    actions = spec.get("actions")
    if not isinstance(actions, list):
        return None
    return next(
        (
            action
            for action in actions
            if isinstance(action, dict) and action.get("id") == action_id
        ),
        None,
    )


def _validate_action_arguments(
    schema: Any,
    arguments: dict[str, Any],
) -> dict[str, Any]:
    if not isinstance(schema, dict) or schema.get("type") != "object":
        raise HTTPException(
            status_code=400,
            detail={
                "error_type": "INVALID_GENERATION_UI_ACTION_SCHEMA",
                "message": "该 UI Action 缺少受控参数定义。",
            },
        )
    properties = schema.get("properties")
    required = schema.get("required", [])
    if not isinstance(properties, dict) or not isinstance(required, list):
        raise HTTPException(
            status_code=400,
            detail={"error_type": "INVALID_GENERATION_UI_ACTION_SCHEMA"},
        )
    if schema.get("additionalProperties") is False and set(arguments) - set(properties):
        raise HTTPException(
            status_code=422,
            detail={
                "error_type": "INVALID_GENERATION_UI_ACTION_ARGUMENTS",
                "message": "提交内容包含界面未声明的字段。",
            },
        )
    missing = [name for name in required if name not in arguments]
    if missing:
        raise HTTPException(
            status_code=422,
            detail={
                "error_type": "INVALID_GENERATION_UI_ACTION_ARGUMENTS",
                "message": f"请填写：{', '.join(missing)}。",
            },
        )
    normalized: dict[str, Any] = {}
    for name, value in arguments.items():
        field_schema = properties.get(name)
        if not isinstance(field_schema, dict):
            continue
        field_type = field_schema.get("type")
        if field_type == "string":
            if not isinstance(value, str):
                _invalid_generation_ui_field(name)
            safe_value = (
                "\n".join(line.strip() for line in value.splitlines() if line.strip())
                if field_schema.get("format") == "multiline"
                else " ".join(value.split())
            )
            minimum = int(field_schema.get("minLength", 1 if name in required else 0))
            maximum = int(field_schema.get("maxLength", 500))
            if not minimum <= len(safe_value) <= maximum:
                _invalid_generation_ui_field(name)
            allowed_values = field_schema.get("enum")
            if isinstance(allowed_values, list) and safe_value not in allowed_values:
                _invalid_generation_ui_field(name)
            if field_schema.get("format") == "date":
                try:
                    date.fromisoformat(safe_value)
                except ValueError:
                    _invalid_generation_ui_field(name)
            normalized[name] = safe_value
        elif field_type == "number":
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                _invalid_generation_ui_field(name)
            if "minimum" in field_schema and value < field_schema["minimum"]:
                _invalid_generation_ui_field(name)
            if "maximum" in field_schema and value > field_schema["maximum"]:
                _invalid_generation_ui_field(name)
            normalized[name] = value
        elif field_type == "boolean":
            if not isinstance(value, bool):
                _invalid_generation_ui_field(name)
            normalized[name] = value
        else:
            _invalid_generation_ui_field(name)
    return normalized


def _invalid_generation_ui_field(name: str) -> None:
    raise HTTPException(
        status_code=422,
        detail={
            "error_type": "INVALID_GENERATION_UI_ACTION_ARGUMENTS",
            "message": f"字段 {name} 的值无效。",
        },
    )


def _extract_message_resources(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    resources: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()

    def add(resource: dict[str, Any], identity: tuple[str, str]) -> None:
        if identity in seen or len(resources) >= 12:
            return
        seen.add(identity)
        resources.append(resource)

    for entry in value:
        if not isinstance(entry, dict):
            continue
        tool_name = entry.get("tool_name")
        result = entry.get("result")
        if not isinstance(tool_name, str) or not isinstance(result, dict):
            continue

        if tool_name == "web_search":
            for item in result.get("results", []):
                if not isinstance(item, dict):
                    continue
                url = _safe_http_url(item.get("url"))
                if url is None:
                    continue
                title = _short_resource_text(item.get("title")) or urlsplit(url).hostname
                if title is None:
                    continue
                add(
                    {
                        "kind": "source",
                        "title": title,
                        "subtitle": _short_resource_text(item.get("snippet"), limit=260),
                        "url": url,
                    },
                    ("source", url),
                )

        source_url = _safe_http_url(result.get("source_url"))
        if source_url is not None:
            title = _short_resource_text(result.get("title"))
            add(
                {
                    "kind": "source",
                    "title": title or urlsplit(source_url).hostname or "来源网页",
                    "subtitle": "Agent 已读取的公开网页",
                    "url": source_url,
                },
                ("source", source_url),
            )

        if tool_name == "search_knowledge_base":
            for item in result.get("candidates", []):
                if not isinstance(item, dict):
                    continue
                document_name = _short_resource_text(item.get("document_name"))
                if document_name is None:
                    continue
                pages = item.get("pages")
                page_text = ""
                if isinstance(pages, list):
                    valid_pages = [str(page) for page in pages if isinstance(page, int)]
                    if valid_pages:
                        page_text = f" · 第 {', '.join(valid_pages)} 页"
                subtitle = _short_resource_text(item.get("content"), limit=260)
                identity = str(item.get("chunk_id") or f"{document_name}:{page_text}")
                add(
                    {
                        "kind": "evidence",
                        "title": document_name + page_text,
                        "subtitle": subtitle,
                    },
                    ("evidence", identity),
                )

        artifact_candidates = result.get("artifacts")
        if not isinstance(artifact_candidates, list):
            artifact_candidates = []
        receipt_path = result.get("receipt_path")
        if isinstance(receipt_path, str):
            artifact_candidates = [*artifact_candidates, {"path": receipt_path}]
        for item in artifact_candidates:
            if not isinstance(item, dict):
                continue
            path = _stored_artifact_path(item.get("path"))
            if path is None:
                continue
            media_type = item.get("media_type")
            add(
                {
                    "kind": "artifact",
                    "title": PurePosixPath(path).name,
                    "subtitle": _short_resource_text(item.get("kind"))
                    or "Agent 生成的文件",
                    "path": path,
                    "media_type": media_type if isinstance(media_type, str) else None,
                },
                ("artifact", path),
            )
    return resources


def _short_resource_text(value: Any, *, limit: int = 180) -> str | None:
    if not isinstance(value, str):
        return None
    normalized = " ".join(value.split())
    if not normalized:
        return None
    if len(normalized) <= limit:
        return normalized
    return normalized[: limit - 1] + "…"


def _safe_http_url(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    parsed = urlsplit(value)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        return None
    return value


def _stored_artifact_path(value: Any) -> str | None:
    if not isinstance(value, str) or "\\" in value:
        return None
    relative = PurePosixPath(value)
    if (
        relative.is_absolute()
        or ".." in relative.parts
        or not relative.parts
        or relative.parts[0] != "artifacts"
    ):
        return None
    return relative.as_posix() if _resolve_artifact_path(relative.as_posix()) else None


def _resolve_artifact_path(value: str) -> Path | None:
    artifact_root = (PROJECT_ROOT / "artifacts").resolve()
    candidate = (PROJECT_ROOT / value).resolve()
    if not candidate.is_relative_to(artifact_root) or not candidate.is_file():
        return None
    return candidate


def _attachment_response(item: ConversationAttachment) -> AttachmentResponse:
    return AttachmentResponse(
        attachment_id=UUID(item.attachment_id),
        filename=item.filename,
        content_type=item.content_type,
        size_bytes=item.size_bytes,
        status=item.status,
        progress=item.progress,
        error_message=item.error_message,
    )


def _message_resource_response(
    item: ConversationResource,
) -> MessageResourceResponse:
    href = item.url
    if item.path is not None:
        href = f"/api/resources/{item.resource_id}"
    return MessageResourceResponse(
        resource_id=UUID(item.resource_id),
        kind=item.kind,
        title=item.title,
        subtitle=item.subtitle,
        href=href,
        media_type=item.media_type,
    )


def _conversation_summary_response(
    item: ConversationSummary,
) -> ConversationSummaryResponse:
    return ConversationSummaryResponse(
        conversation_id=UUID(item.conversation_id),
        title=item.title,
        updated_at=item.updated_at,
        message_count=item.message_count,
    )


def _generation_ui_artifact_response(
    item: ConversationUiArtifact,
) -> GenerationUiArtifactResponse:
    return GenerationUiArtifactResponse(
        conversation_id=UUID(item.conversation_id),
        message_id=str(item.message_id),
        artifact_id=UUID(item.artifact_id),
        scenario=item.scenario,
        text=item.text,
        spec=item.spec,
        data=item.data,
    )


def _open_attachment_gateway(
    gateway_factory: AttachmentGatewayFactory,
    kind: AttachmentKind,
) -> AttachmentGateway:
    try:
        return gateway_factory(kind)
    except RuntimeError as exc:
        raise HTTPException(
            status_code=503,
            detail={
                "error_type": "RAG_ATTACHMENT_CONFIGURATION_ERROR",
                "message": str(exc),
            },
        ) from exc


def _refresh_attachments(
    store: SQLiteConversationStore,
    conversation_id: str,
    attachments: list[ConversationAttachment],
    gateway_factory: AttachmentGatewayFactory,
) -> list[ConversationAttachment]:
    pending = [
        item
        for item in attachments
        if item.status == "PROCESSING" and item.job_id is not None
    ]
    if not pending:
        return attachments
    gateway = _open_attachment_gateway(
        gateway_factory,
        _stored_attachment_kind(pending[0]),
    )
    try:
        for item in pending:
            job = gateway.get_job(item.job_id or "")
            if not job.get("success"):
                continue
            job_data = job.get("data")
            if not isinstance(job_data, dict):
                continue
            job_status = job_data.get("status")
            progress = job_data.get("progress")
            normalized_progress = (
                float(progress)
                if isinstance(progress, int | float) and not isinstance(progress, bool)
                else item.progress
            )
            if job_status in {"FAILED", "CANCELLED"}:
                error = job_data.get("error")
                error_code = error.get("code") if isinstance(error, dict) else None
                store.update_attachment_status(
                    conversation_id,
                    item.attachment_id,
                    status="FAILED",
                    progress=normalized_progress,
                    error_message=str(error_code or f"RAG_JOB_{job_status}"),
                )
                continue
            if job_status != "SUCCEEDED" or item.document_id is None:
                store.update_attachment_status(
                    conversation_id,
                    item.attachment_id,
                    status="PROCESSING",
                    progress=normalized_progress,
                )
                continue
            document = gateway.get_document(item.document_id)
            if not document.get("success"):
                continue
            document_data = document.get("data")
            if not isinstance(document_data, dict):
                continue
            document_status = document_data.get("status")
            document_progress = document_data.get("progress")
            normalized_document_progress = (
                float(document_progress)
                if isinstance(document_progress, int | float)
                and not isinstance(document_progress, bool)
                else normalized_progress
            )
            if document_status == "READY":
                store.update_attachment_status(
                    conversation_id,
                    item.attachment_id,
                    status="READY",
                    progress=1,
                )
            elif document_status in {"FAILED", "DELETE_FAILED", "DELETED"}:
                last_error = document_data.get("last_error")
                error_code = (
                    last_error.get("code") if isinstance(last_error, dict) else None
                )
                store.update_attachment_status(
                    conversation_id,
                    item.attachment_id,
                    status="FAILED",
                    progress=normalized_document_progress,
                    error_message=str(error_code or f"RAG_DOCUMENT_{document_status}"),
                )
            else:
                store.update_attachment_status(
                    conversation_id,
                    item.attachment_id,
                    status="PROCESSING",
                    progress=normalized_document_progress,
                )
    finally:
        gateway.close()
    return store.attachments(conversation_id)


def _delete_unshared_documents(
    store: SQLiteConversationStore,
    attachments: list[ConversationAttachment],
    gateway_factory: AttachmentGatewayFactory,
) -> None:
    document_counts = Counter(
        item.document_id for item in attachments if item.document_id is not None
    )
    deletions: list[tuple[str, str]] = []
    for document_id, selected_count in document_counts.items():
        if document_id is None:
            continue
        if store.document_reference_count(document_id) <= selected_count:
            attachment_id = next(
                item.attachment_id
                for item in attachments
                if item.document_id == document_id
            )
            deletions.append((document_id, attachment_id))
    if not deletions:
        return
    selected_attachment = next(
        item
        for item in attachments
        if item.document_id == deletions[0][0]
    )
    gateway = _open_attachment_gateway(
        gateway_factory,
        _stored_attachment_kind(selected_attachment),
    )
    try:
        for document_id, attachment_id in deletions:
            result = gateway.delete_document(
                document_id,
                attachment_id=attachment_id,
            )
            if not result.get("success"):
                raise HTTPException(
                    status_code=502,
                    detail={
                        "error_type": "RAG_ATTACHMENT_DELETE_FAILED",
                        "message": "附件暂时无法从 RAG 删除，请稍后重试。",
                    },
                )
    finally:
        gateway.close()


def _validate_filename(filename: str) -> str:
    normalized = filename.strip()
    if (
        not normalized
        or normalized in {".", ".."}
        or "/" in normalized
        or "\\" in normalized
        or any(ord(character) < 32 for character in normalized)
    ):
        raise HTTPException(
            status_code=400,
            detail={"error_type": "INVALID_ATTACHMENT_FILENAME"},
        )
    return normalized


def _validate_content_type(content_type: str) -> str:
    normalized = content_type.split(";", 1)[0].strip().lower()
    if (
        not normalized
        or len(normalized) > 128
        or any(ord(character) < 32 for character in normalized)
    ):
        raise HTTPException(
            status_code=400,
            detail={"error_type": "INVALID_ATTACHMENT_CONTENT_TYPE"},
        )
    return normalized


def _attachment_max_bytes() -> int:
    raw_value = os.getenv(
        "RAG_ATTACHMENT_MAX_BYTES", str(DEFAULT_ATTACHMENT_MAX_BYTES)
    ).strip()
    try:
        value = int(raw_value)
    except ValueError as exc:
        raise RuntimeError("RAG_ATTACHMENT_MAX_BYTES must be an integer") from exc
    if value <= 0:
        raise RuntimeError("RAG_ATTACHMENT_MAX_BYTES must be positive")
    return value


def _attachment_kind(filename: str) -> AttachmentKind:
    extension = Path(filename).suffix.lower()
    if extension in PDF_EXTENSIONS:
        return "pdf"
    if extension in OFFICE_EXTENSIONS:
        return "office"
    if extension in IMAGE_EXTENSIONS:
        return "image"
    if extension in MEDIA_EXTENSIONS:
        return "media"
    raise HTTPException(
        status_code=415,
        detail={"error_type": "UNSUPPORTED_ATTACHMENT_TYPE"},
    )


def _stored_attachment_kind(item: ConversationAttachment) -> AttachmentKind:
    if item.knowledge_base_kind == "pdf":
        return "pdf"
    if item.knowledge_base_kind == "office":
        return "office"
    if item.knowledge_base_kind == "image":
        return "image"
    if item.knowledge_base_kind == "media":
        return "media"
    return _attachment_kind(item.filename)


def _model_configured() -> bool:
    return bool(os.getenv("OPENAI_API_KEY", "").strip() and os.getenv("OPENAI_MODEL", "").strip())


def _agsx_code_configured() -> bool:
    return all(_configured(name) for name in ("E2B_API_KEY", "E2B_DOMAIN", "AGSX_TEMPLATE"))


def _configured(name: str) -> bool:
    return bool(os.getenv(name, "").strip())


app = create_app(owner_reminder_scheduler_enabled=True)
