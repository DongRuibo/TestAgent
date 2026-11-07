"""经人工确认后，把已审核图文保存到小红书草稿箱。"""

from __future__ import annotations

import fcntl
from io import BytesIO
from pathlib import Path
from typing import Any
from uuid import uuid4

from PIL import Image, UnidentifiedImageError
from rpa_agent_module import (
    ConfigurationError,
    JobConflictError,
    JobInputError,
    JobNotFoundError,
    JobUnavailableError,
    RpaExecutionModule,
    RpaModuleError,
    RpaModuleConfig,
)
from rpa_agent_module.database import JobRepository
from rpa_agent_module.models import verified_draft_result

from testagent_dev_agent.paths import REPOSITORY_ROOT


RPA_XHS_DATA_DIR = REPOSITORY_ROOT / ".local" / "rpa-agent-module"
RPA_XHS_IMAGE_ROOT = REPOSITORY_ROOT / "workspace" / "content_assets"
RPA_XHS_WAIT_TIMEOUT_SECONDS = 180
_ALLOWED_IMAGE_FORMATS = {"JPEG", "MPO", "PNG", "WEBP"}
CONTENT_IMAGE_MAX_BYTES = 32 * 1024 * 1024
_IMAGE_SUFFIXES = {"JPEG": ".jpg", "PNG": ".png", "WEBP": ".webp"}


def store_content_image(content: bytes) -> str:
    """校验浏览器上传的图片，并保存到小红书 RPA 受控目录。"""

    if not content or len(content) > CONTENT_IMAGE_MAX_BYTES:
        raise ValueError("图片大小必须大于 0 且不超过 32MB。")
    try:
        with Image.open(BytesIO(content)) as image:
            image.verify()
            image_format = image.format or ""
    except (OSError, UnidentifiedImageError) as exc:
        raise ValueError("文件不是有效图片。") from exc
    if image_format not in _ALLOWED_IMAGE_FORMATS:
        raise ValueError("图片仅支持 JPEG、PNG 或 WebP。")
    if image_format == "MPO":
        try:
            with Image.open(BytesIO(content)) as image:
                image.seek(0)
                normalized = BytesIO()
                image.convert("RGB").save(normalized, format="JPEG", quality=95)
                content = normalized.getvalue()
                image_format = "JPEG"
        except (OSError, UnidentifiedImageError) as exc:
            raise ValueError("文件不是有效图片。") from exc

    root = RPA_XHS_IMAGE_ROOT.resolve()
    upload_root = root / "uploads"
    upload_root.mkdir(parents=True, exist_ok=True)
    target = (upload_root / f"{uuid4().hex}{_IMAGE_SUFFIXES[image_format]}").resolve()
    if not target.is_relative_to(root):  # pragma: no cover - defensive invariant
        raise ValueError("图片保存路径超出受控目录。")
    target.write_bytes(content)
    return str(target)


def validate_content_image_paths(value: str | list[str]) -> list[str]:
    """把用户审核的图片路径收口到唯一受控目录。"""

    raw_paths = value.splitlines() if isinstance(value, str) else value
    if not isinstance(raw_paths, list):
        raise ValueError("图片路径必须是每行一条的文本。")
    cleaned = [str(item).strip() for item in raw_paths if str(item).strip()]
    if not 1 <= len(cleaned) <= 9:
        raise ValueError("确认内容必须提供 1-9 张图片。")
    root = RPA_XHS_IMAGE_ROOT.resolve()
    normalized: list[str] = []
    for raw in cleaned:
        path = Path(raw).expanduser()
        if not path.is_absolute():
            raise ValueError("图片路径必须是绝对路径。")
        if path.is_symlink():
            raise ValueError(f"图片不能是符号链接：{path.name}")
        resolved = path.resolve()
        if not resolved.is_relative_to(root):
            raise ValueError(f"图片必须位于受控目录：{root}")
        if not resolved.is_file():
            raise ValueError(f"图片文件不存在或类型不安全：{resolved.name}")
        size = resolved.stat().st_size
        if size <= 0 or size > CONTENT_IMAGE_MAX_BYTES:
            raise ValueError(f"图片大小必须大于 0 且不超过 32MB：{resolved.name}")
        try:
            with Image.open(resolved) as image:
                image.verify()
                image_format = image.format or ""
        except (OSError, UnidentifiedImageError) as exc:
            raise ValueError(f"文件不是有效图片：{resolved.name}") from exc
        if image_format not in _ALLOWED_IMAGE_FORMATS:
            raise ValueError(f"图片仅支持 JPEG、PNG 或 WebP：{resolved.name}")
        stored = str(resolved)
        if stored not in normalized:
            normalized.append(stored)
    if not normalized:
        raise ValueError("至少需要一张有效图片。")
    return normalized


def save_xiaohongshu_draft(
    *,
    title: str,
    body: str,
    tags: list[str],
    image_paths: list[str],
    module_factory: Any = RpaExecutionModule,
    execution_id: str | None = None,
    on_job_created: Any = None,
) -> dict[str, Any]:
    """同步等待一个受控 RPA 任务；成功只代表草稿箱核验通过。"""

    RPA_XHS_IMAGE_ROOT.mkdir(parents=True, exist_ok=True)
    config = RpaModuleConfig(
        data_dir=RPA_XHS_DATA_DIR,
        allowed_image_roots=(RPA_XHS_IMAGE_ROOT,),
        xiaohongshu_enabled=True,
    )
    job_id: str | None = None
    try:
        with module_factory(config) as rpa:
            health = rpa.health()
            if not health.platform_supported:
                return _error("RPA_PLATFORM_UNSUPPORTED", "小红书 RPA 只能在本机 macOS 上运行。")
            if not health.chrome_available:
                return _error("RPA_CHROME_UNAVAILABLE", "未检测到 Google Chrome。")
            if not health.database_ready:
                return _error("RPA_DATABASE_UNAVAILABLE", "RPA 任务数据库未就绪。")
            payload = {
                    "title": title,
                    "body": body,
                    "tags": tags,
                    "image_paths": image_paths,
                }
            options = {"execution_id": execution_id, "on_job_created": on_job_created} if execution_id is not None or on_job_created is not None else {}
            job = rpa.submit("xiaohongshu_draft", payload, **options)
            job_id = job.id
            try:
                completed = rpa.wait(job.id, timeout=RPA_XHS_WAIT_TIMEOUT_SECONDS)
            except TimeoutError:
                current = rpa.get_job(job.id)
                if not current.is_terminal:
                    current = rpa.stop(job.id)
                return _error(
                    "RPA_TIMEOUT",
                    "等待小红书草稿任务超时，任务已停止；不会自动重复提交。",
                    job_id=current.id,
                    status="needs_review",
                    needs_review=True,
                )
    except JobInputError as exc:
        return _error("RPA_INVALID_INPUT", str(exc), job_id=job_id)
    except JobConflictError as exc:
        return _error("RPA_BUSY", str(exc), job_id=job_id, retryable=True)
    except JobUnavailableError as exc:
        return _error("RPA_UNAVAILABLE", str(exc), job_id=job_id)
    except JobNotFoundError as exc:
        return _error("RPA_JOB_NOT_FOUND", str(exc), job_id=job_id)
    except ConfigurationError as exc:
        return _error("RPA_CONFIGURATION_ERROR", str(exc), job_id=job_id)
    except RpaModuleError as exc:
        return _error("RPA_MODULE_ERROR", str(exc), job_id=job_id)
    except Exception:
        return _error(
            "RPA_EXECUTION_ERROR",
            "小红书 RPA 出现未预期错误，请查看本机任务日志。",
            job_id=job_id,
            status="needs_review" if job_id or execution_id else "failed",
            needs_review=bool(job_id or execution_id),
        )

    return _draft_job_result(completed)


def query_verified_draft(*, target: dict, authorized: bool, execution_id: str, job_id: str | None, query):
    """新旧宿主共用原执行读回入口；保存应答和模型声明均不是完成凭证。"""
    try:
        result = query(execution_id=execution_id, job_id=job_id)
    except Exception:
        result = None
    if (isinstance(result, dict) and result.get("success") is False
        and (job_id is None or result.get("job_id") is None or result.get("job_id") == job_id)):
        return result
    if (isinstance(result, dict) and result.get("success") is True
        and result.get("workflow") == "xiaohongshu_draft" and result.get("status") == "succeeded"
        and isinstance(result.get("job_id"), str) and bool(result["job_id"])
        and (job_id is None or result["job_id"] == job_id)
        and result.get("result_type") == "xiaohongshu_draft_saved"
        and result.get("final_publish_performed") is False
        and not result.get("needs_review") and not result.get("needs_user_action")
        and authorized and verified_draft_result(result.get("result"), target)
        and result.get("verification") == result["result"]["verification"]):
        return result
    return {"success": False, "status": "needs_review", "needs_review": True, "job_id": job_id,
        "error_type": "RPA_RESULT_UNVERIFIED", "message": "原执行未取得与审核内容一致的完整核验回执，请人工核对；不会重复保存。",
        "result": result, "final_publish_performed": result.get("final_publish_performed") if isinstance(result, dict) else None}


def query_xiaohongshu_draft(*, execution_id: str, job_id: str | None = None) -> dict[str, Any]:
    """只读核对原任务，不创建模块、浏览器或新的保存动作。"""

    database = RPA_XHS_DATA_DIR / "jobs.db"
    job = JobRepository(database).get_by_execution_id(execution_id) if database.is_file() else None
    if job is None or (job_id is not None and job.id != job_id):
        return _error("RPA_RESULT_UNKNOWN", "未找到匹配的原执行任务，请人工核验；不会自动再次保存。", job_id=job_id,
                      status="needs_review", needs_review=True)
    if job.status in {"queued", "running"}:
        lock_path = RPA_XHS_DATA_DIR / ".module.lock"
        if lock_path.is_file():
            with lock_path.open("rb") as lock_file:
                try:
                    fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    return _error("RPA_RUNNING", "原任务仍由本机模块持有，请稍后查询。", job_id=job.id, status=job.status)
                else:
                    fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
        return _error("RPA_INTERRUPTED", "原任务已失去执行实例，请人工核验草稿现场。", job_id=job.id,
                      status="needs_review", needs_review=True)
    return _draft_job_result(job)


def _draft_job_result(completed: Any) -> dict[str, Any]:
    """统一保存与只读查询的结果口径，旧的标题回执不能升级为完整成功。"""

    if completed.status == "needs_user_action":
        return _error(
            completed.error_code or "RPA_NEEDS_USER_ACTION",
            completed.error_message or "需要用户在专用 Chrome 中完成操作。",
            job_id=completed.id,
            status=completed.status,
            needs_user_action=True,
        )
    if completed.status != "succeeded" or not verified_draft_result(completed.result, completed.input):
        uncertain = completed.status in {"succeeded", "needs_review", "stopped"} or completed.error_code == "module_interrupted"
        return {**_error(
            completed.error_code or "RPA_JOB_FAILED",
            completed.error_message or "小红书草稿任务未成功完成。",
            job_id=completed.id,
            status="needs_review" if uncertain else completed.status,
            needs_review=uncertain,
        ), "result": completed.result}
    result = completed.result
    return {
        "success": True,
        "workflow": "xiaohongshu_draft",
        "job_id": completed.id,
        "status": completed.status,
        "result_type": result.get("result_type"),
        "draft_url": result.get("url"),
        "image_count": result.get("image_count"),
        "verification": result["verification"],
        "result": result,
        "message": result.get("message") or "小红书草稿已保存并完成草稿箱核验。",
        "final_publish_performed": False,
    }


def _error(
    error_type: str,
    message: str,
    *,
    job_id: str | None = None,
    status: str = "failed",
    retryable: bool = False,
    needs_user_action: bool = False,
    needs_review: bool = False,
) -> dict[str, Any]:
    if job_id and status == "failed":
        status, needs_review = "needs_review", True
    return {
        "success": False,
        "error_type": error_type,
        "message": message,
        "job_id": job_id,
        "status": status,
        "retryable": retryable,
        "needs_user_action": needs_user_action,
        "needs_review": needs_review,
        "final_publish_performed": False,
    }


__all__ = [
    "RPA_XHS_DATA_DIR",
    "RPA_XHS_IMAGE_ROOT",
    "CONTENT_IMAGE_MAX_BYTES",
    "save_xiaohongshu_draft",
    "query_xiaohongshu_draft",
    "store_content_image",
    "validate_content_image_paths",
]
