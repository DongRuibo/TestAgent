"""本地网页对话：复用 AgentLoop，返回回答和可展示的执行轨迹。"""

from __future__ import annotations

import os
import time
from collections.abc import Callable
from pathlib import Path
from typing import Literal, Protocol

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from dotenv import load_dotenv
from pydantic import BaseModel, ConfigDict, Field, field_validator

from agent.loop import AgentLoop, TOOL_DEFINITIONS
from llm.client import LLMClient


STATIC_DIR = Path(__file__).resolve().parent / "static"
PROJECT_ROOT = Path(__file__).resolve().parents[1]
load_dotenv(PROJECT_ROOT / ".env")


class RunnableAgent(Protocol):
    def run(self, task: str, *, history: list[dict[str, str]] | None = None) -> str: ...


AgentFactory = Callable[[Callable[[str], None]], RunnableAgent]


class ChatHistoryMessage(BaseModel):
    model_config = ConfigDict(extra="forbid")

    role: Literal["user", "assistant"]
    content: str = Field(min_length=1, max_length=20_000)

    @field_validator("content")
    @classmethod
    def reject_blank_content(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("message content cannot be blank")
        return value


class ChatRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    message: str = Field(min_length=1, max_length=20_000)
    history: list[ChatHistoryMessage] = Field(default_factory=list, max_length=20)

    @field_validator("message")
    @classmethod
    def reject_blank_message(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("message cannot be blank")
        return value


class ChatResponse(BaseModel):
    answer: str
    trace: list[str]
    duration_ms: int


def _default_agent_factory(trace: Callable[[str], None]) -> AgentLoop:
    return AgentLoop(LLMClient.from_env(), trace=trace)


def create_app(agent_factory: AgentFactory | None = None) -> FastAPI:
    factory = agent_factory or _default_agent_factory
    app = FastAPI(
        title="Mini Agent Web",
        version="0.1.0",
        description="Local chat interface for the TestAgent demo.",
    )
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

    @app.get("/", include_in_schema=False)
    def index() -> FileResponse:
        return FileResponse(STATIC_DIR / "index.html")

    @app.get("/api/health")
    def health() -> dict[str, object]:
        gateway_configured = _gateway_configured()
        return {
            "status": "ready" if _model_configured() and gateway_configured else "configuration_required",
            "model_configured": _model_configured(),
            "gateway_configured": gateway_configured,
            "code_sandbox_configured": gateway_configured,
            "browser_sandbox_configured": gateway_configured,
            "tools": list(TOOL_DEFINITIONS),
        }

    @app.post("/api/chat", response_model=ChatResponse)
    def chat(request: ChatRequest) -> ChatResponse:
        trace: list[str] = []
        started = time.perf_counter()
        try:
            agent = factory(trace.append)
            answer = agent.run(
                request.message,
                history=[item.model_dump() for item in request.history],
            )
        except RuntimeError as exc:
            raise HTTPException(
                status_code=502,
                detail={"error_type": "AGENT_EXECUTION_ERROR", "message": str(exc)},
            ) from exc
        except Exception as exc:
            raise HTTPException(
                status_code=500,
                detail={"error_type": "WEB_AGENT_ERROR", "message": str(exc)},
            ) from exc
        return ChatResponse(
            answer=answer,
            trace=trace,
            duration_ms=round((time.perf_counter() - started) * 1000),
        )

    return app


def _model_configured() -> bool:
    return bool(os.getenv("OPENAI_API_KEY", "").strip() and os.getenv("OPENAI_MODEL", "").strip())


def _gateway_configured() -> bool:
    return all(
        os.getenv(name, "").strip()
        for name in ("SANDBOX_GATEWAY_URL", "SANDBOX_GATEWAY_API_KEY")
    )


app = create_app()
