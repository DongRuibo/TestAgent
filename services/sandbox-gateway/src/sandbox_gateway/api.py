"""Gateway V0 的 FastAPI HTTP 边界。"""

from __future__ import annotations

from typing import Callable

from fastapi import Depends, FastAPI, Header, HTTPException, Response, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from sandbox_gateway.auth import GatewayAuth, GatewayIdentity
from sandbox_contracts import (
    ExecutionCreateRequest,
    ExecutionView,
    FileUploadRequest,
    FileUploadView,
)
from sandbox_gateway.service import GatewayError, GatewayService


def create_app(service: GatewayService, auth: GatewayAuth) -> FastAPI:
    app = FastAPI(
        title="Lazyx Sandbox Gateway",
        version="0.1.0",
        description="Provider-neutral execution API for Agent sandboxes.",
    )
    bearer = HTTPBearer(auto_error=False)

    def identity_dependency(
        credentials: HTTPAuthorizationCredentials | None = Depends(bearer),
    ) -> GatewayIdentity:
        token = credentials.credentials if credentials else None
        try:
            return auth.authenticate(token)
        except PermissionError as exc:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail={"error_type": "UNAUTHORIZED", "message": str(exc)},
                headers={"WWW-Authenticate": "Bearer"},
            ) from exc

    @app.get("/health")
    def health() -> dict[str, object]:
        return {
            "status": "ok",
            "providers": sorted(service.providers),
        }

    @app.post("/v1/files", response_model=FileUploadView, status_code=status.HTTP_201_CREATED)
    def upload_file(
        request: FileUploadRequest,
        identity: GatewayIdentity = Depends(identity_dependency),
    ) -> FileUploadView:
        return _call(lambda: service.upload_file(identity.tenant_id, identity.agent_id, request))

    @app.post("/v1/executions", response_model=ExecutionView, status_code=status.HTTP_202_ACCEPTED)
    def create_execution(
        request: ExecutionCreateRequest,
        identity: GatewayIdentity = Depends(identity_dependency),
        idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    ) -> ExecutionView:
        # V0 先要求调用方显式传幂等键；Repository 的唯一约束将在下一步扩展为去重。
        if idempotency_key is not None and len(idempotency_key) > 128:
            raise HTTPException(status_code=400, detail={"error_type": "INVALID_IDEMPOTENCY_KEY"})
        if request.task_type.value not in identity.allowed_tasks:
            raise HTTPException(
                status_code=403,
                detail={"error_type": "POLICY_DENIED", "message": "task type is not allowed"},
            )
        return _call(
            lambda: service.create_execution(
                identity.tenant_id,
                identity.agent_id,
                request,
                idempotency_key,
            )
        )

    @app.get("/v1/executions/{execution_id}", response_model=ExecutionView)
    def get_execution(
        execution_id: str,
        identity: GatewayIdentity = Depends(identity_dependency),
    ) -> ExecutionView:
        return _call(lambda: service.get_execution(execution_id, identity.tenant_id, identity.agent_id))

    @app.post("/v1/executions/{execution_id}/cancel", response_model=ExecutionView)
    def cancel_execution(
        execution_id: str,
        identity: GatewayIdentity = Depends(identity_dependency),
    ) -> ExecutionView:
        return _call(lambda: service.cancel_execution(execution_id, identity.tenant_id, identity.agent_id))

    @app.get("/v1/executions/{execution_id}/artifacts/{artifact_name}")
    def download_artifact(
        execution_id: str,
        artifact_name: str,
        identity: GatewayIdentity = Depends(identity_dependency),
    ) -> Response:
        data, media_type, name = _call(
            lambda: service.download_artifact(
                execution_id,
                artifact_name,
                identity.tenant_id,
                identity.agent_id,
            )
        )
        return Response(
            content=data,
            media_type=media_type,
            headers={"Content-Disposition": f'attachment; filename="{name}"'},
        )

    return app


def _call(function: Callable[[], object]):
    try:
        return function()
    except GatewayError as exc:
        raise HTTPException(
            status_code=exc.status_code,
            detail={"error_type": exc.error_type, "message": str(exc)},
        ) from exc
