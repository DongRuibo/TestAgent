"""read_file 工具的路径安全与读取行为测试。"""

from pathlib import Path

from tools.read_file import read_file


def test_read_file_returns_workspace_text(tmp_path: Path) -> None:
    """验证工具能读取 workspace 内的 UTF-8 文本。"""
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "note.md").write_text("Agent Loop", encoding="utf-8")

    result = read_file("note.md", workspace=workspace)

    assert result == {
        "success": True,
        "path": "note.md",
        "content": "Agent Loop",
        "total_chars": 10,
        "truncated": False,
    }


def test_read_file_rejects_parent_directory(tmp_path: Path) -> None:
    """验证 ../ 无法越过 workspace 边界读取外部文件。"""
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (tmp_path / "secret.txt").write_text("secret", encoding="utf-8")

    result = read_file("../secret.txt", workspace=workspace)

    assert result["success"] is False
    assert result["error_type"] == "PATH_OUTSIDE_WORKSPACE"


def test_read_file_returns_structured_missing_file_error(tmp_path: Path) -> None:
    """验证文件不存在时返回结构化错误，而不是让 Agent 崩溃。"""
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    result = read_file("missing.md", workspace=workspace)

    assert result["success"] is False
    assert result["error_type"] == "FILE_NOT_FOUND"
    assert result["retryable"] is True
