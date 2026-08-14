"""Gateway V0 的执行编排：策略、Provider、工作流、Receipt 和清理。"""

from __future__ import annotations

import base64
import binascii
import mimetypes
import re
import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Mapping
from uuid import uuid4

from sandbox_runtime.artifact_store import ArtifactStore
from sandbox_runtime.browser_workflow import PublicBrowserWorkflow
from sandbox_runtime.models import (
    ArtifactRecord,
    CleanupResult,
    ExecutionReceipt,
    RequestContext,
    SandboxKind,
    SandboxSession,
    utc_now,
)
from sandbox_runtime.provider import SandboxProvider, provider_name
from sandbox_runtime.report_renderer import render_pdf, render_pptx
from sandbox_runtime.sales_workflow import SalesAnalysisWorkflow
from sandbox_runtime.spreadsheet_workflow import SpreadsheetAnalysisWorkflow

from sandbox_contracts import (
    ArtifactView,
    ExecutionCreateRequest,
    ExecutionStatus,
    ExecutionView,
    FileUploadRequest,
    FileUploadView,
    NetworkProfile,
    TaskType,
)
from sandbox_gateway.repository import SQLiteRepository, StoredExecution, StoredFile


class GatewayError(RuntimeError):
    """可直接映射为 HTTP 错误的 Gateway 异常。"""

    def __init__(self, message: str, *, status_code: int = 400, error_type: str = "GATEWAY_ERROR") -> None:
        super().__init__(message)
        self.status_code = status_code
        self.error_type = error_type


@dataclass(frozen=True)
class GatewayConfig:
    max_upload_bytes: int = 10 * 1024 * 1024
    max_workers: int = 4


BrowserWorkflowFactory = Callable[[SandboxProvider, ArtifactStore], PublicBrowserWorkflow]
SalesWorkflowFactory = Callable[[SandboxProvider, ArtifactStore], SalesAnalysisWorkflow]
SpreadsheetWorkflowFactory = Callable[[SandboxProvider, ArtifactStore], SpreadsheetAnalysisWorkflow]


class GatewayService:
    """面向多个 Agent 的一次性执行服务。

    V0 使用 SQLite 和本地 Artifact Store，但 Provider 密钥仍只由服务端持有。
    生产环境可以在不改变 API 的情况下替换仓储和对象存储实现。
    """

    def __init__(
        self,
        providers: Mapping[str, SandboxProvider],
        *,
        database_path: Path | str,
        artifact_root: Path | str,
        config: GatewayConfig | None = None,
        browser_workflow_factory: BrowserWorkflowFactory | None = None,
        sales_workflow_factory: SalesWorkflowFactory | None = None,
        spreadsheet_workflow_factory: SpreadsheetWorkflowFactory | None = None,
    ) -> None:
        self.providers = dict(providers)
        self.config = config or GatewayConfig()
        self.repository = SQLiteRepository(database_path)
        self.artifact_store = ArtifactStore(artifact_root)
        self.upload_root = Path(artifact_root).resolve() / "uploads"
        self._executor = ThreadPoolExecutor(max_workers=self.config.max_workers)
        self._active_sessions: dict[str, tuple[SandboxProvider, SandboxSession]] = {}
        self._cleanup_results: dict[str, Any] = {}
        self._active_lock = threading.RLock()
        self._browser_workflow_factory = browser_workflow_factory or (
            lambda provider, store: PublicBrowserWorkflow(provider, store)
        )
        self._sales_workflow_factory = sales_workflow_factory or (
            lambda provider, store: SalesAnalysisWorkflow(provider, store)
        )
        self._spreadsheet_workflow_factory = spreadsheet_workflow_factory or (
            lambda provider, store: SpreadsheetAnalysisWorkflow(provider, store)
        )

    def upload_file(
        self,
        tenant_id: str,
        agent_id: str,
        request: FileUploadRequest,
    ) -> FileUploadView:
        try:
            data = base64.b64decode(request.content_base64, validate=True)
        except (binascii.Error, ValueError) as exc:
            raise GatewayError(
                "content_base64 is invalid.",
                error_type="INVALID_FILE_CONTENT",
            ) from exc
        if len(data) > self.config.max_upload_bytes:
            raise GatewayError(
                f"file exceeds {self.config.max_upload_bytes} bytes.",
                error_type="FILE_TOO_LARGE",
            )
        stored = self.repository.create_file(
            tenant_id,
            agent_id,
            request.name,
            request.media_type,
            data,
            self.upload_root,
        )
        return FileUploadView(
            file_id=stored.file_id,
            name=stored.name,
            media_type=stored.media_type,
            size_bytes=stored.size_bytes,
            sha256=stored.sha256,
        )

    def create_execution(
        self,
        tenant_id: str,
        agent_id: str,
        request: ExecutionCreateRequest,
        idempotency_key: str | None = None,
    ) -> ExecutionView:
        if idempotency_key:
            existing = self.repository.find_by_idempotency(
                tenant_id,
                agent_id,
                idempotency_key,
            )
            if existing is not None:
                if existing.request.model_dump(mode="json") != request.model_dump(mode="json"):
                    raise GatewayError(
                        "Idempotency-Key was already used for a different request.",
                        status_code=409,
                        error_type="IDEMPOTENCY_CONFLICT",
                    )
                return self._to_view(existing)
        provider_key, provider = self._select_provider(request)
        self._check_task_permission(request, agent_id)
        self._check_input_files(request, tenant_id, agent_id)
        execution_id = f"exec-{uuid4().hex}"
        try:
            record = self.repository.create_execution(
                execution_id,
                tenant_id,
                agent_id,
                request,
                utc_now(),
                idempotency_key,
            )
        except sqlite3.IntegrityError as exc:
            # 两个并发请求可能同时通过前置查询；让唯一索引兜底，随后返回
            # 与第一次提交相同的记录，而不是把数据库错误泄漏给 Agent。
            if idempotency_key:
                existing = self.repository.find_by_idempotency(
                    tenant_id,
                    agent_id,
                    idempotency_key,
                )
                if existing is not None:
                    if existing.request.model_dump(mode="json") != request.model_dump(mode="json"):
                        raise GatewayError(
                            "Idempotency-Key was already used for a different request.",
                            status_code=409,
                            error_type="IDEMPOTENCY_CONFLICT",
                        ) from exc
                    return self._to_view(existing)
            raise GatewayError(
                "Could not persist execution request.",
                status_code=503,
                error_type="EXECUTION_STORE_UNAVAILABLE",
            ) from exc
        record.provider = provider_key
        record.updated_at = utc_now()
        self.repository.save_execution(record)
        self._executor.submit(self._run_execution, execution_id, provider_key, provider)
        return self._to_view(record)

    def get_execution(
        self,
        execution_id: str,
        tenant_id: str,
        agent_id: str,
    ) -> ExecutionView:
        record = self._owned_execution(execution_id, tenant_id, agent_id)
        return self._to_view(record)

    def cancel_execution(
        self,
        execution_id: str,
        tenant_id: str,
        agent_id: str,
    ) -> ExecutionView:
        record = self._owned_execution(execution_id, tenant_id, agent_id)
        if record.status in {
            ExecutionStatus.SUCCEEDED,
            ExecutionStatus.FAILED,
            ExecutionStatus.CANCELLED,
        }:
            return self._to_view(record)
        now = utc_now()
        if record.status is ExecutionStatus.QUEUED:
            record.status = ExecutionStatus.CANCELLED
            record.finished_at = now
        else:
            record.cancellation_requested = True
            with self._active_lock:
                active = self._active_sessions.get(execution_id)
            if active is not None:
                provider, session = active
                self._terminate_once(execution_id, provider, session)
        record.updated_at = now
        self.repository.save_execution(record)
        return self._to_view(record)

    def download_artifact(
        self,
        execution_id: str,
        artifact_name: str,
        tenant_id: str,
        agent_id: str,
    ) -> tuple[bytes, str, str]:
        record = self._owned_execution(execution_id, tenant_id, agent_id)
        for artifact in record.artifacts:
            if artifact.get("name") == artifact_name:
                path = (self.artifact_store.root / artifact["relative_path"]).resolve()
                if not path.is_relative_to(self.artifact_store.root):
                    break
                try:
                    data = path.read_bytes()
                except FileNotFoundError as exc:
                    raise GatewayError(
                        "Artifact no longer exists.",
                        status_code=404,
                        error_type="ARTIFACT_NOT_FOUND",
                    ) from exc
                return data, str(artifact.get("media_type", "application/octet-stream")), artifact_name
        raise GatewayError("Artifact not found.", status_code=404, error_type="ARTIFACT_NOT_FOUND")

    def shutdown(self) -> None:
        self._executor.shutdown(wait=False, cancel_futures=True)

    def recover_after_restart(self) -> list[str]:
        """把进程重启前未完成的本地任务标为失败。

        V0 不会假装可以从 SQLite 恢复远端 Python 进程或 Browser 状态；
        先明确结束本地记录，随后由 `cleanup_orphans()` 扫描云端带项目标签的实例。
        """
        recovered: list[str] = []
        for record in self.repository.get_active_executions():
            self._fail(
                record,
                "GATEWAY_RESTARTED",
                "Gateway restarted before this execution reached a terminal state.",
            )
            recovered.append(record.execution_id)
        return recovered

    def cleanup_orphans(self, *, max_age_seconds: int = 600) -> dict[str, list[dict[str, Any]]]:
        """调用 Provider 的受控标签扫描，回收本项目的过期远端实例。

        Provider 没有实现扫描能力时返回 `unsupported`，不会尝试猜测或删除未知实例。
        """
        if max_age_seconds < 60:
            raise GatewayError(
                "max_age_seconds must be at least 60.",
                error_type="INVALID_CLEANUP_AGE",
            )
        report: dict[str, list[dict[str, Any]]] = {}
        for key, provider in self.providers.items():
            cleanup = getattr(provider, "cleanup_expired", None)
            if not callable(cleanup):
                report[key] = [{"status": "unsupported"}]
                continue
            try:
                report[key] = [item.to_dict() for item in cleanup(max_age_seconds)]
            except Exception as exc:
                report[key] = [
                    {
                        "status": "error",
                        "error_type": type(exc).__name__,
                        "error": str(exc),
                    }
                ]
        return report

    def _run_execution(
        self,
        execution_id: str,
        provider_key: str,
        provider: SandboxProvider,
    ) -> None:
        record = self.repository.get_execution(execution_id)
        if record is None:
            return
        if record.status is ExecutionStatus.CANCELLED:
            return
        record.status = ExecutionStatus.POLICY_CHECKED
        record.started_at = utc_now()
        record.updated_at = utc_now()
        self.repository.save_execution(record)
        try:
            if record.request.task_type is TaskType.CODE:
                self._run_code(record, provider_key, provider)
            elif record.request.task_type is TaskType.SALES_ANALYSIS:
                self._run_sales(record, provider_key, provider)
            elif record.request.task_type is TaskType.SPREADSHEET_ANALYSIS:
                self._run_spreadsheet(record, provider_key, provider)
            else:
                self._run_browser(record, provider_key, provider)
        except GatewayError as exc:
            self._fail(record, exc.error_type, str(exc))
        except Exception as exc:
            self._fail(record, "GATEWAY_EXECUTION_ERROR", f"{type(exc).__name__}: {exc}")

    def _run_code(
        self,
        record: StoredExecution,
        provider_key: str,
        provider: SandboxProvider,
    ) -> None:
        request = record.request
        context = self._context(record)
        started = utc_now()
        receipt = ExecutionReceipt(
            receipt_id=f"receipt-{uuid4().hex}",
            context=context,
            provider=provider_key,
            action="gateway_execute_code",
            started_at=started,
        )
        session: SandboxSession | None = None
        output_records: list[ArtifactRecord] = []
        payload: dict[str, Any] = {}
        try:
            self._set_status(record, ExecutionStatus.CREATING)
            session = provider.create_code(context, ttl_seconds=request.limits.ttl_seconds)
            receipt.sandbox_id = session.sandbox_id
            record.sandbox_id = session.sandbox_id
            record.status = ExecutionStatus.RUNNING
            self.repository.save_execution(record)
            with self._active_lock:
                self._active_sessions[record.execution_id] = (provider, session)

            for input_file in request.input_files:
                stored = self._owned_file(input_file.file_id, record.tenant_id, record.agent_id)
                provider.put_file(session, input_file.remote_path, self._read_stored_file(stored))

            execution = provider.execute_code(
                session,
                request.source or "",
                language=request.language,
                timeout_seconds=request.limits.timeout_seconds,
            )
            payload = execution.to_dict()
            for index, remote_path in enumerate(request.output_paths):
                data = provider.get_file(session, remote_path)
                if len(data) > request.limits.max_output_bytes:
                    raise GatewayError(
                        f"output file exceeds {request.limits.max_output_bytes} bytes.",
                        status_code=413,
                        error_type="OUTPUT_TOO_LARGE",
                    )
                name = _safe_output_name(remote_path, index)
                output_records.append(
                    self.artifact_store.put_bytes(
                        record.execution_id,
                        name,
                        data,
                        mimetypes.guess_type(name)[0] or "application/octet-stream",
                    )
                )
            receipt.outputs.extend(output_records)
            receipt.status = "passed" if execution.success else "failed"
            receipt.error = execution.error_message
        except GatewayError as exc:
            receipt.status = "failed"
            receipt.error = str(exc)
            payload = {
                "success": False,
                "error_type": exc.error_type,
                "error_message": str(exc),
            }
        except Exception as exc:
            receipt.status = "failed"
            receipt.error = f"{type(exc).__name__}: {exc}"
            payload = {
                "success": False,
                "error_type": "GATEWAY_EXECUTION_ERROR",
                "error_message": receipt.error,
            }
        finally:
            if session is not None:
                self._set_status(record, ExecutionStatus.TERMINATING)
                cleanup = self._terminate_once(record.execution_id, provider, session)
                receipt.cleanup = cleanup
                with self._active_lock:
                    self._active_sessions.pop(record.execution_id, None)
                if not cleanup.destroyed:
                    receipt.status = "failed"
                    receipt.error = receipt.error or cleanup.error or "sandbox was not destroyed"
            receipt.finished_at = utc_now()

        receipt_record = self.artifact_store.put_json(
            record.execution_id,
            "execution-receipt.json",
            receipt.to_dict(),
        )
        all_artifacts = [item.to_dict() for item in output_records]
        all_artifacts.append(receipt_record.to_dict())
        if record.cancellation_requested:
            payload.update({"success": False, "error_type": "CANCELLED"})
            final_status = ExecutionStatus.CANCELLED
        else:
            final_status = (
                ExecutionStatus.SUCCEEDED
                if receipt.status == "passed"
                else ExecutionStatus.FAILED
            )
        self._complete(record, final_status, payload, all_artifacts, receipt.to_dict())

    def _run_sales(
        self,
        record: StoredExecution,
        provider_key: str,
        provider: SandboxProvider,
    ) -> None:
        stored = self._owned_file(
            record.request.input_files[0].file_id,
            record.tenant_id,
            record.agent_id,
        )
        self._set_status(record, ExecutionStatus.RUNNING)
        context = self._context(record)
        workflow = self._sales_workflow_factory(provider, self.artifact_store)
        result = workflow.run(Path(stored.path), context)
        receipt_payload = _read_artifact_json(self.artifact_store, result.receipt)
        record.sandbox_id = receipt_payload.get("sandbox_id")
        artifacts = [item.to_dict() for item in result.artifacts]
        artifacts.append(result.receipt.to_dict())
        payload = {
            "success": result.success,
            "summary": result.summary,
            "error": result.error,
        }
        if record.cancellation_requested:
            payload.update({"success": False, "error_type": "CANCELLED"})
            final_status = ExecutionStatus.CANCELLED
        else:
            final_status = ExecutionStatus.SUCCEEDED if result.success else ExecutionStatus.FAILED
        self._complete(record, final_status, payload, artifacts, receipt_payload)

    def _run_browser(
        self,
        record: StoredExecution,
        provider_key: str,
        provider: SandboxProvider,
    ) -> None:
        self._set_status(record, ExecutionStatus.RUNNING)
        context = self._context(record)
        workflow = self._browser_workflow_factory(provider, self.artifact_store)
        result = workflow.run(
            record.request.url or "",
            context,
            max_text_chars=record.request.max_text_chars,
        )
        receipt_payload = _read_artifact_json(self.artifact_store, result.receipt)
        record.sandbox_id = receipt_payload.get("sandbox_id")
        artifacts = [item.to_dict() for item in result.artifacts]
        artifacts.append(result.receipt.to_dict())
        payload = {
            "success": result.success,
            "source_url": result.source_url,
            "final_url": result.final_url,
            "status_code": result.status_code,
            "title": result.title,
            "text_preview": result.text_preview,
            "text_total_chars": result.text_total_chars,
            "trust": result.trust,
            "instructions_executable": result.instructions_executable,
            "error": result.error,
        }
        if record.cancellation_requested:
            payload.update({"success": False, "error_type": "CANCELLED"})
            final_status = ExecutionStatus.CANCELLED
        else:
            final_status = ExecutionStatus.SUCCEEDED if result.success else ExecutionStatus.FAILED
        self._complete(record, final_status, payload, artifacts, receipt_payload)

    def _run_spreadsheet(
        self,
        record: StoredExecution,
        provider_key: str,
        provider: SandboxProvider,
    ) -> None:
        stored = self._owned_file(
            record.request.input_files[0].file_id,
            record.tenant_id,
            record.agent_id,
        )
        self._set_status(record, ExecutionStatus.RUNNING)
        workflow = self._spreadsheet_workflow_factory(provider, self.artifact_store)
        result = workflow.run(
            Path(stored.path),
            self._context(record),
            source_name=stored.name,
        )
        receipt_payload = _read_artifact_json(self.artifact_store, result.receipt)
        record.sandbox_id = receipt_payload.get("sandbox_id")
        output_records = list(result.artifacts)
        render_error: str | None = None
        if result.success and record.request.generate_briefing and result.analysis is not None:
            try:
                output_records.extend([
                    self.artifact_store.put_bytes(
                        record.execution_id,
                        "briefing.pdf",
                        render_pdf(result.analysis),
                        "application/pdf",
                    ),
                    self.artifact_store.put_bytes(
                        record.execution_id,
                        "briefing.pptx",
                        render_pptx(result.analysis),
                        "application/vnd.openxmlformats-officedocument.presentationml.presentation",
                    ),
                ])
            except Exception as exc:
                render_error = f"{type(exc).__name__}: {exc}"
        if output_records:
            receipt_payload["outputs"] = [item.to_dict() for item in output_records]
        if render_error:
            receipt_payload["status"] = "failed"
            receipt_payload["error"] = render_error
        receipt_record = self.artifact_store.put_json(
            record.execution_id,
            "execution-receipt.json",
            receipt_payload,
        )
        artifacts = [item.to_dict() for item in output_records]
        artifacts.append(receipt_record.to_dict())
        payload = {
            "success": result.success and render_error is None,
            "analysis": result.analysis,
            "briefing_requested": record.request.generate_briefing,
            "error": render_error or result.error,
        }
        if record.cancellation_requested:
            payload.update({"success": False, "error_type": "CANCELLED"})
            final_status = ExecutionStatus.CANCELLED
        else:
            final_status = ExecutionStatus.SUCCEEDED if payload["success"] else ExecutionStatus.FAILED
        self._complete(record, final_status, payload, artifacts, receipt_payload)

    def _select_provider(
        self,
        request: ExecutionCreateRequest,
    ) -> tuple[str, SandboxProvider]:
        kind = SandboxKind.CODE if request.task_type is not TaskType.BROWSER_PUBLIC else SandboxKind.BROWSER
        preference = request.provider_preference
        if preference != "auto":
            candidates = [preference]
        elif request.task_type is TaskType.SPREADSHEET_ANALYSIS:
            # 经营表格默认走国内企业侧已配置的 AGSX Tool；调用方仍可显式指定
            # Provider 做对照测试，但不会在失败后跨 Provider 自动重放任务。
            candidates = ["tencent-agsx"]
        elif request.network_profile in {NetworkProfile.OFFLINE, NetworkProfile.VPC_RESTRICTED}:
            candidates = ["tencent-agsx"]
        else:
            candidates = ["ppio-sandbox", "tencent-agsx"]

        for key in candidates:
            provider = self.providers.get(key)
            if provider is None:
                continue
            capabilities = provider.capabilities()
            if kind not in capabilities.kinds:
                continue
            if request.task_type is TaskType.CODE:
                normalized_language = {
                    "js": "javascript",
                    "ts": "typescript",
                }.get(request.language, request.language)
                if normalized_language not in capabilities.languages:
                    continue
            if (
                request.input_files or request.output_paths
            ) and not capabilities.supports_file_transfer:
                continue
            if request.network_profile in {
                NetworkProfile.OFFLINE,
                NetworkProfile.VPC_RESTRICTED,
            } and not capabilities.supports_domain_egress_policy:
                continue
            return key, provider
        raise GatewayError(
            "No configured Provider satisfies this task and network profile.",
            status_code=503,
            error_type="NO_PROVIDER",
        )

    def _terminate_once(
        self,
        execution_id: str,
        provider: SandboxProvider,
        session: SandboxSession,
    ) -> CleanupResult:
        """保证取消路径和后台 finally 不会重复 kill 同一实例。"""
        with self._active_lock:
            previous = self._cleanup_results.get(execution_id)
            if previous is not None:
                return previous
            try:
                cleanup = provider.terminate(session)
            except Exception as exc:
                cleanup = CleanupResult(
                    session.sandbox_id,
                    False,
                    f"{type(exc).__name__}: {exc}",
                )
            self._cleanup_results[execution_id] = cleanup
            return cleanup

    @staticmethod
    def _check_task_permission(request: ExecutionCreateRequest, agent_id: str) -> None:
        # V0 的权限映射由 GatewayAuth 控制；这里保留任务级策略扩展点。
        if not agent_id.strip():
            raise GatewayError("agent identity is empty.", error_type="INVALID_IDENTITY")

    def _check_input_files(
        self,
        request: ExecutionCreateRequest,
        tenant_id: str,
        agent_id: str,
    ) -> None:
        for input_file in request.input_files:
            self._owned_file(input_file.file_id, tenant_id, agent_id)

    def _owned_file(self, file_id: str, tenant_id: str, agent_id: str) -> StoredFile:
        stored = self.repository.get_file(file_id)
        if stored is None or stored.tenant_id != tenant_id or stored.agent_id != agent_id:
            raise GatewayError("File not found.", status_code=404, error_type="FILE_NOT_FOUND")
        return stored

    def _read_stored_file(self, stored: StoredFile) -> bytes:
        data = Path(stored.path).read_bytes()
        if len(data) != stored.size_bytes:
            raise GatewayError("Uploaded file size changed.", error_type="FILE_INTEGRITY_ERROR")
        if _sha256(data) != stored.sha256:
            raise GatewayError("Uploaded file hash changed.", error_type="FILE_INTEGRITY_ERROR")
        return data

    def _owned_execution(self, execution_id: str, tenant_id: str, agent_id: str) -> StoredExecution:
        record = self.repository.get_execution(execution_id)
        if record is None or record.tenant_id != tenant_id or record.agent_id != agent_id:
            raise GatewayError("Execution not found.", status_code=404, error_type="EXECUTION_NOT_FOUND")
        return record

    def _context(self, record: StoredExecution) -> RequestContext:
        return RequestContext(
            tenant_id=record.tenant_id,
            user_id=record.agent_id,
            task_id=record.execution_id,
            attempt_id=record.execution_id,
        )

    def _set_status(self, record: StoredExecution, status: ExecutionStatus) -> None:
        record.status = status
        record.updated_at = utc_now()
        self.repository.save_execution(record)

    def _complete(
        self,
        record: StoredExecution,
        status: ExecutionStatus,
        result: dict[str, Any],
        artifacts: list[dict],
        receipt: dict,
    ) -> None:
        record.status = status
        record.result = result
        record.artifacts = artifacts
        record.receipt = receipt
        record.error_type = result.get("error_type")
        record.error_message = result.get("error") or result.get("error_message")
        record.finished_at = utc_now()
        record.updated_at = utc_now()
        self.repository.save_execution(record)
        with self._active_lock:
            self._cleanup_results.pop(record.execution_id, None)

    def _fail(self, record: StoredExecution, error_type: str, message: str) -> None:
        record.status = ExecutionStatus.FAILED
        record.error_type = error_type
        record.error_message = message
        record.result = {
            "success": False,
            "error_type": error_type,
            "error_message": message,
        }
        record.finished_at = utc_now()
        record.updated_at = utc_now()
        self.repository.save_execution(record)
        with self._active_lock:
            self._cleanup_results.pop(record.execution_id, None)

    @staticmethod
    def _to_view(record: StoredExecution) -> ExecutionView:
        return ExecutionView(
            execution_id=record.execution_id,
            tenant_id=record.tenant_id,
            agent_id=record.agent_id,
            task_type=record.request.task_type,
            status=record.status,
            provider=record.provider,
            sandbox_id=record.sandbox_id,
            created_at=record.created_at.isoformat(),
            updated_at=record.updated_at.isoformat(),
            started_at=record.started_at.isoformat() if record.started_at else None,
            finished_at=record.finished_at.isoformat() if record.finished_at else None,
            result=record.result,
            artifacts=[ArtifactView.model_validate(item) for item in record.artifacts],
            receipt=record.receipt,
            error_type=record.error_type,
            error_message=record.error_message,
            cancellation_requested=record.cancellation_requested,
        )


def _read_artifact_json(store: ArtifactStore, record: ArtifactRecord) -> dict[str, Any]:
    import json

    value = json.loads(store.read_bytes(record))
    return value if isinstance(value, dict) else {}


def _safe_output_name(remote_path: str, index: int) -> str:
    name = Path(remote_path).name or f"output-{index}.bin"
    safe = re.sub(r"[^A-Za-z0-9._-]", "_", name)
    return f"output-{index}-{safe}"


def _sha256(data: bytes) -> str:
    import hashlib

    return hashlib.sha256(data).hexdigest()
