"""V1 文件读取工具：安全地读取 workspace 内的文本文件。"""

from __future__ import annotations

from pathlib import Path
from typing import Any


DEFAULT_WORKSPACE = Path(__file__).resolve().parent.parent / "workspace"
MAX_FILE_CHARS = 20_000


def read_file(
    path: str,
    workspace: Path = DEFAULT_WORKSPACE,
    max_chars: int = MAX_FILE_CHARS,
) -> dict[str, Any]:
    """读取 workspace 中的 UTF-8 文件，并返回方便模型理解的结构化结果。

    路径经过 resolve 后还会再次检查是否位于 workspace 内，防止模型使用
    ``../`` 或绝对路径读取任务范围之外的文件。
    """
    workspace = workspace.resolve()
    requested = Path(path)

    # 用户可能在任务中写 workspace/example.md；工具内部统一转为相对路径。
    if requested.parts and requested.parts[0] == workspace.name:
        requested = Path(*requested.parts[1:])

    if requested.is_absolute():
        return _error("PATH_OUTSIDE_WORKSPACE", "只允许使用 workspace 内的相对路径。")

    target = (workspace / requested).resolve()
    if not target.is_relative_to(workspace):
        return _error("PATH_OUTSIDE_WORKSPACE", "禁止读取 workspace 目录之外的文件。")
    if not target.exists():
        return _error("FILE_NOT_FOUND", f"文件不存在：{requested}", retryable=True)
    if not target.is_file():
        return _error("NOT_A_FILE", f"路径不是文件：{requested}", retryable=True)

    try:
        content = target.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        return _error("NOT_UTF8_TEXT", f"文件不是 UTF-8 文本：{requested}")
    except OSError as exc:
        return _error("READ_ERROR", f"读取文件失败：{exc}", retryable=True)

    # V1 先用简单截断保护上下文；V4 再实现完整结果存储与引用机制。
    truncated = len(content) > max_chars
    preview = content[:max_chars]
    return {
        "success": True,
        "path": str(target.relative_to(workspace)),
        "content": preview,
        "total_chars": len(content),
        "truncated": truncated,
    }


def _error(
    error_type: str,
    message: str,
    retryable: bool = False,
) -> dict[str, Any]:
    """统一构造工具错误，让错误也能作为观察结果反馈给模型。"""
    return {
        "success": False,
        "error_type": error_type,
        "message": message,
        "retryable": retryable,
    }
