"""Gateway REST API 的同步和异步 Python SDK。"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Mapping
from typing import Any

import httpx

from sandbox_gateway.models import (
    ExecutionCreateRequest,
    ExecutionStatus,
    ExecutionView,
    FileUploadRequest,
    FileUploadView,
)


class SandboxClientError(RuntimeError):
    def __init__(self, status_code: int, message: str, error_type: str | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.error_type = error_type


class SandboxClient:
    def __init__(self, base_url: str, api_key: str, *, timeout: float = 30) -> None:
        self._client = httpx.Client(
            base_url=base_url.rstrip("/"),
            timeout=timeout,
            headers={"Authorization": f"Bearer {api_key}"},
        )

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> "SandboxClient":
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def upload_file(self, name: str, content: bytes, *, media_type: str = "application/octet-stream") -> FileUploadView:
        request = FileUploadRequest(
            name=name,
            media_type=media_type,
            content_base64=_encode(content),
        )
        response = self._client.post("/v1/files", json=request.model_dump(mode="json"))
        return _decode(response, FileUploadView)

    def create_execution(self, request: ExecutionCreateRequest, *, idempotency_key: str | None = None) -> ExecutionView:
        headers = {"Idempotency-Key": idempotency_key} if idempotency_key else None
        response = self._client.post(
            "/v1/executions",
            json=request.model_dump(mode="json"),
            headers=headers,
        )
        return _decode(response, ExecutionView)

    def execute_code(
        self,
        source: str,
        *,
        language: str = "python",
        input_files: list[dict[str, str]] | None = None,
        output_paths: list[str] | None = None,
        provider_preference: str = "auto",
        network_profile: str = "provider_default",
        idempotency_key: str | None = None,
        wait: bool = True,
        timeout: float = 60,
    ) -> ExecutionView:
        request = ExecutionCreateRequest(
            task_type="code",
            source=source,
            language=language,
            input_files=input_files or [],
            output_paths=output_paths or [],
            provider_preference=provider_preference,
            network_profile=network_profile,
        )
        result = self.create_execution(
            request,
            idempotency_key=idempotency_key or f"sdk-{time.time_ns()}",
        )
        return self.wait(result.execution_id, timeout=timeout) if wait else result

    def analyze_sales_file(
        self,
        name: str,
        content: bytes,
        *,
        media_type: str = "text/csv",
        provider_preference: str = "auto",
        network_profile: str = "provider_default",
        wait: bool = True,
        timeout: float = 120,
    ) -> ExecutionView:
        uploaded = self.upload_file(name, content, media_type=media_type)
        request = ExecutionCreateRequest(
            task_type="sales_analysis",
            input_files=[
                {
                    "file_id": uploaded.file_id,
                    "remote_path": f"/tmp/lazyx/input/{uploaded.name}",
                }
            ],
            provider_preference=provider_preference,
            network_profile=network_profile,
        )
        result = self.create_execution(request, idempotency_key=f"sdk-{time.time_ns()}")
        return self.wait(result.execution_id, timeout=timeout) if wait else result

    def analyze_spreadsheet_file(
        self,
        name: str,
        content: bytes,
        *,
        media_type: str = "application/octet-stream",
        generate_briefing: bool = False,
        provider_preference: str = "auto",
        network_profile: str = "provider_default",
        wait: bool = True,
        timeout: float = 120,
    ) -> ExecutionView:
        uploaded = self.upload_file(name, content, media_type=media_type)
        request = ExecutionCreateRequest(
            task_type="spreadsheet_analysis",
            input_files=[{
                "file_id": uploaded.file_id,
                "remote_path": f"/tmp/lazyx/input/{uploaded.name}",
            }],
            generate_briefing=generate_briefing,
            provider_preference=provider_preference,
            network_profile=network_profile,
        )
        result = self.create_execution(request, idempotency_key=f"sdk-{time.time_ns()}")
        return self.wait(result.execution_id, timeout=timeout) if wait else result

    def browse_public_page(
        self,
        url: str,
        *,
        provider_preference: str = "auto",
        max_text_chars: int = 12_000,
        wait: bool = True,
        timeout: float = 120,
    ) -> ExecutionView:
        request = ExecutionCreateRequest(
            task_type="browser_public",
            url=url,
            max_text_chars=max_text_chars,
            network_profile="public_readonly",
            provider_preference=provider_preference,
        )
        result = self.create_execution(request, idempotency_key=f"sdk-{time.time_ns()}")
        return self.wait(result.execution_id, timeout=timeout) if wait else result

    def get_execution(self, execution_id: str) -> ExecutionView:
        response = self._client.get(f"/v1/executions/{execution_id}")
        return _decode(response, ExecutionView)

    def wait(self, execution_id: str, *, timeout: float = 60, poll_interval: float = 0.1) -> ExecutionView:
        deadline = time.monotonic() + timeout
        while True:
            result = self.get_execution(execution_id)
            if result.status in {
                ExecutionStatus.SUCCEEDED,
                ExecutionStatus.FAILED,
                ExecutionStatus.CANCELLED,
            }:
                return result
            if time.monotonic() >= deadline:
                raise TimeoutError(f"Execution {execution_id} did not finish in {timeout}s.")
            time.sleep(poll_interval)

    def cancel(self, execution_id: str) -> ExecutionView:
        response = self._client.post(f"/v1/executions/{execution_id}/cancel")
        return _decode(response, ExecutionView)

    def download_artifact(self, execution_id: str, artifact_name: str) -> bytes:
        response = self._client.get(f"/v1/executions/{execution_id}/artifacts/{artifact_name}")
        _raise_for_error(response)
        return response.content


class AsyncSandboxClient:
    def __init__(self, base_url: str, api_key: str, *, timeout: float = 30) -> None:
        self._client = httpx.AsyncClient(
            base_url=base_url.rstrip("/"),
            timeout=timeout,
            headers={"Authorization": f"Bearer {api_key}"},
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    async def upload_file(self, name: str, content: bytes, *, media_type: str = "application/octet-stream") -> FileUploadView:
        request = FileUploadRequest(
            name=name,
            media_type=media_type,
            content_base64=_encode(content),
        )
        response = await self._client.post("/v1/files", json=request.model_dump(mode="json"))
        return _decode(response, FileUploadView)

    async def create_execution(self, request: ExecutionCreateRequest, *, idempotency_key: str | None = None) -> ExecutionView:
        headers = {"Idempotency-Key": idempotency_key} if idempotency_key else None
        response = await self._client.post(
            "/v1/executions",
            json=request.model_dump(mode="json"),
            headers=headers,
        )
        return _decode(response, ExecutionView)

    async def execute_code(
        self,
        source: str,
        *,
        language: str = "python",
        input_files: list[dict[str, str]] | None = None,
        output_paths: list[str] | None = None,
        provider_preference: str = "auto",
        network_profile: str = "provider_default",
        idempotency_key: str | None = None,
        wait: bool = True,
        timeout: float = 60,
    ) -> ExecutionView:
        result = await self.create_execution(
            ExecutionCreateRequest(
                task_type="code",
                source=source,
                language=language,
                input_files=input_files or [],
                output_paths=output_paths or [],
                provider_preference=provider_preference,
                network_profile=network_profile,
            ),
            idempotency_key=idempotency_key or f"sdk-{time.time_ns()}",
        )
        return await self.wait(result.execution_id, timeout=timeout) if wait else result

    async def analyze_sales_file(
        self,
        name: str,
        content: bytes,
        *,
        media_type: str = "text/csv",
        provider_preference: str = "auto",
        network_profile: str = "provider_default",
        wait: bool = True,
        timeout: float = 120,
    ) -> ExecutionView:
        uploaded = await self.upload_file(name, content, media_type=media_type)
        result = await self.create_execution(
            ExecutionCreateRequest(
                task_type="sales_analysis",
                input_files=[
                    {
                        "file_id": uploaded.file_id,
                        "remote_path": f"/tmp/lazyx/input/{uploaded.name}",
                    }
                ],
                provider_preference=provider_preference,
                network_profile=network_profile,
            ),
            idempotency_key=f"sdk-{time.time_ns()}",
        )
        return await self.wait(result.execution_id, timeout=timeout) if wait else result

    async def analyze_spreadsheet_file(
        self,
        name: str,
        content: bytes,
        *,
        media_type: str = "application/octet-stream",
        generate_briefing: bool = False,
        provider_preference: str = "auto",
        network_profile: str = "provider_default",
        wait: bool = True,
        timeout: float = 120,
    ) -> ExecutionView:
        uploaded = await self.upload_file(name, content, media_type=media_type)
        result = await self.create_execution(
            ExecutionCreateRequest(
                task_type="spreadsheet_analysis",
                input_files=[{
                    "file_id": uploaded.file_id,
                    "remote_path": f"/tmp/lazyx/input/{uploaded.name}",
                }],
                generate_briefing=generate_briefing,
                provider_preference=provider_preference,
                network_profile=network_profile,
            ),
            idempotency_key=f"sdk-{time.time_ns()}",
        )
        return await self.wait(result.execution_id, timeout=timeout) if wait else result

    async def browse_public_page(
        self,
        url: str,
        *,
        provider_preference: str = "auto",
        max_text_chars: int = 12_000,
        wait: bool = True,
        timeout: float = 120,
    ) -> ExecutionView:
        result = await self.create_execution(
            ExecutionCreateRequest(
                task_type="browser_public",
                url=url,
                max_text_chars=max_text_chars,
                network_profile="public_readonly",
                provider_preference=provider_preference,
            ),
            idempotency_key=f"sdk-{time.time_ns()}",
        )
        return await self.wait(result.execution_id, timeout=timeout) if wait else result

    async def get_execution(self, execution_id: str) -> ExecutionView:
        response = await self._client.get(f"/v1/executions/{execution_id}")
        return _decode(response, ExecutionView)

    async def wait(self, execution_id: str, *, timeout: float = 60, poll_interval: float = 0.1) -> ExecutionView:
        deadline = time.monotonic() + timeout
        while True:
            result = await self.get_execution(execution_id)
            if result.status in {
                ExecutionStatus.SUCCEEDED,
                ExecutionStatus.FAILED,
                ExecutionStatus.CANCELLED,
            }:
                return result
            if time.monotonic() >= deadline:
                raise TimeoutError(f"Execution {execution_id} did not finish in {timeout}s.")
            await asyncio.sleep(poll_interval)

    async def cancel(self, execution_id: str) -> ExecutionView:
        response = await self._client.post(f"/v1/executions/{execution_id}/cancel")
        return _decode(response, ExecutionView)

    async def download_artifact(self, execution_id: str, artifact_name: str) -> bytes:
        response = await self._client.get(f"/v1/executions/{execution_id}/artifacts/{artifact_name}")
        _raise_for_error(response)
        return response.content


def _encode(content: bytes) -> str:
    import base64

    return base64.b64encode(content).decode("ascii")


def _decode(response: httpx.Response, model: type[Any]) -> Any:
    _raise_for_error(response)
    return model.model_validate(response.json())


def _raise_for_error(response: httpx.Response) -> None:
    if response.is_success:
        return
    try:
        payload = response.json()
        detail = payload.get("detail", payload)
        if isinstance(detail, dict):
            message = str(detail.get("message", detail))
            error_type = detail.get("error_type")
        else:
            message = str(detail)
            error_type = None
    except ValueError:
        message = response.text or response.reason_phrase
        error_type = None
    raise SandboxClientError(response.status_code, message, error_type)
