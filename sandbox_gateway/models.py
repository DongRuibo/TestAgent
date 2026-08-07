"""Gateway V0 的公开请求、状态和响应模型。"""

from __future__ import annotations

from enum import StrEnum
from typing import Any
from urllib.parse import urlparse

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class TaskType(StrEnum):
    CODE = "code"
    SALES_ANALYSIS = "sales_analysis"
    SPREADSHEET_ANALYSIS = "spreadsheet_analysis"
    BROWSER_PUBLIC = "browser_public"


class NetworkProfile(StrEnum):
    PROVIDER_DEFAULT = "provider_default"
    OFFLINE = "offline"
    PUBLIC_READONLY = "public_readonly"
    VPC_RESTRICTED = "vpc_restricted"


class ExecutionStatus(StrEnum):
    QUEUED = "queued"
    POLICY_CHECKED = "policy_checked"
    CREATING = "creating"
    RUNNING = "running"
    COLLECTING = "collecting"
    TERMINATING = "terminating"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"


class ExecutionLimits(BaseModel):
    model_config = ConfigDict(extra="forbid")

    ttl_seconds: int = Field(default=300, ge=1, le=86_400)
    timeout_seconds: float = Field(default=30, gt=0, le=3_600)
    max_output_bytes: int = Field(default=1_048_576, ge=1, le=10_485_760)


class InputFileRef(BaseModel):
    model_config = ConfigDict(extra="forbid")

    file_id: str = Field(min_length=1, max_length=128, pattern=r"^file-[A-Za-z0-9-]+$")
    remote_path: str = Field(min_length=1, max_length=512)

    @field_validator("remote_path")
    @classmethod
    def validate_remote_path(cls, value: str) -> str:
        if not value.startswith("/") or ".." in value.split("/"):
            raise ValueError("remote_path must be absolute and cannot contain '..'.")
        return value


class FileUploadRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=128)
    media_type: str = Field(default="application/octet-stream", max_length=128)
    content_base64: str = Field(min_length=1)

    @field_validator("name")
    @classmethod
    def validate_name(cls, value: str) -> str:
        if value in {".", ".."} or "/" in value or "\\" in value:
            raise ValueError("name must be a single file name.")
        return value


class FileUploadView(BaseModel):
    file_id: str
    name: str
    media_type: str
    size_bytes: int
    sha256: str


class ExecutionCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    task_type: TaskType
    source: str | None = Field(default=None, max_length=20_000)
    language: str = Field(default="python", max_length=32)
    input_files: list[InputFileRef] = Field(default_factory=list, max_length=20)
    output_paths: list[str] = Field(default_factory=list, max_length=20)
    url: str | None = Field(default=None, max_length=2_048)
    max_text_chars: int = Field(default=12_000, ge=1, le=100_000)
    generate_briefing: bool = False
    network_profile: NetworkProfile = NetworkProfile.PROVIDER_DEFAULT
    provider_preference: str = Field(default="auto", max_length=64)
    limits: ExecutionLimits = Field(default_factory=ExecutionLimits)

    @field_validator("language")
    @classmethod
    def normalize_language(cls, value: str) -> str:
        value = value.strip().lower()
        if value not in {"python", "javascript", "typescript", "js", "ts"}:
            raise ValueError("language must be python, javascript, typescript, js or ts.")
        return value

    @field_validator("output_paths")
    @classmethod
    def validate_output_paths(cls, values: list[str]) -> list[str]:
        for value in values:
            if not value.startswith("/") or ".." in value.split("/"):
                raise ValueError("output_paths must be absolute and cannot contain '..'.")
        return values

    @model_validator(mode="after")
    def validate_task_shape(self) -> "ExecutionCreateRequest":
        if self.task_type is TaskType.CODE:
            if not self.source or not self.source.strip():
                raise ValueError("code tasks require a non-empty source.")
        elif self.task_type in {TaskType.SALES_ANALYSIS, TaskType.SPREADSHEET_ANALYSIS}:
            if len(self.input_files) != 1:
                raise ValueError(f"{self.task_type.value} requires exactly one input file.")
        elif self.task_type is TaskType.BROWSER_PUBLIC:
            if not self.url:
                raise ValueError("browser_public requires url.")
            parsed = urlparse(self.url)
            if parsed.scheme not in {"http", "https"} or not parsed.netloc:
                raise ValueError("browser_public url must be an absolute HTTP(S) URL.")
            if self.input_files or self.output_paths:
                raise ValueError("browser_public does not accept input_files or output_paths.")
        return self


class ArtifactView(BaseModel):
    name: str
    relative_path: str
    sha256: str
    size_bytes: int
    media_type: str


class ExecutionView(BaseModel):
    execution_id: str
    tenant_id: str
    agent_id: str
    task_type: TaskType
    status: ExecutionStatus
    provider: str | None = None
    sandbox_id: str | None = None
    created_at: str
    updated_at: str
    started_at: str | None = None
    finished_at: str | None = None
    result: dict[str, Any] | None = None
    artifacts: list[ArtifactView] = Field(default_factory=list)
    receipt: dict[str, Any] | None = None
    error_type: str | None = None
    error_message: str | None = None
    cancellation_requested: bool = False
