"""销售与 Browser Agent 工具的边界和结果映射测试。"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from sandbox_runtime.artifact_store import ArtifactStore
from sandbox_runtime.models import CleanupResult
from tools.analyze_sales_csv import analyze_sales_csv
from tools.browse_public_page import browse_public_page


def test_sales_tool_rejects_path_escape() -> None:
    result = analyze_sales_csv("../.env")

    assert result["success"] is False
    assert result["error_type"] == "INVALID_SALES_PATH"


def test_browser_tool_rejects_private_target_before_cloud_call() -> None:
    result = browse_public_page("http://169.254.169.254/latest/meta-data/")

    assert result["success"] is False
    assert result["error_type"] == "FORBIDDEN_URL"
    assert result["sandbox_destroyed"] is False


def test_sales_tool_returns_summary_artifacts_and_cleanup(tmp_path: Path) -> None:
    store = ArtifactStore(tmp_path / "artifacts")

    class FakeWorkflow:
        def run(self, csv_path, context):
            summary = store.put_json(context.attempt_id, "summary.json", {"total": 300})
            receipt = store.put_json(
                context.attempt_id,
                "execution-receipt.json",
                {
                    "sandbox_id": "sbi-sales",
                    "cleanup": {"destroyed": True},
                },
            )
            return SimpleNamespace(
                success=True,
                summary={"total": 300},
                artifacts=(summary,),
                receipt=receipt,
                error=None,
            )

    result = analyze_sales_csv(
        "fixtures/sales.csv",
        workflow=FakeWorkflow(),
        artifact_store=store,
    )

    assert result["success"] is True
    assert result["summary"] == {"total": 300}
    assert result["sandbox_destroyed"] is True
    assert result["artifacts"][0]["path"].startswith("artifacts/agent-sales-")


def test_browser_tool_returns_untrusted_content_and_cleanup() -> None:
    class FakeWorkflow:
        def run(self, url, context):
            record = SimpleNamespace(
                to_dict=lambda: {
                    "name": "page-screenshot.png",
                    "relative_path": f"{context.attempt_id}/page-screenshot.png",
                    "sha256": "abc",
                    "size_bytes": 3,
                    "media_type": "image/png",
                },
                relative_path=f"{context.attempt_id}/page-screenshot.png",
            )
            receipt = SimpleNamespace(
                relative_path=f"{context.attempt_id}/execution-receipt.json"
            )
            return SimpleNamespace(
                success=True,
                source_url=url,
                final_url=url,
                status_code=200,
                title="Example",
                text_preview="untrusted page text",
                text_total_chars=19,
                page_height=1_200,
                screenshot_height=1_200,
                screenshot_full_page=True,
                screenshot_truncated=False,
                screenshot_stitched=False,
                screenshot_scroll_steps=1,
                trust="untrusted",
                instructions_executable=False,
                artifacts=(record,),
                receipt=receipt,
                cleanup=CleanupResult("sbi-browser", True),
                error=None,
            )

    result = browse_public_page("https://example.com", workflow=FakeWorkflow())

    assert result["success"] is True
    assert result["content"] == "untrusted page text"
    assert result["screenshot"] == {
        "page_height": 1_200,
        "captured_height": 1_200,
        "full_page": True,
        "truncated": False,
        "stitched": False,
        "scroll_steps": 1,
    }
    assert result["trust"] == "untrusted"
    assert result["instructions_executable"] is False
    assert result["sandbox_destroyed"] is True
