from __future__ import annotations

from io import BytesIO
import json
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from PIL import Image

from agent.context import DEFAULT_TOOL_RESULT_MAX_CHARS, serialize_tool_result
from agent.loop import DEFAULT_TOOL_BUDGETS, SYSTEM_PROMPT, TOOL_DEFINITIONS
from tools.content_operations import (
    CONTENT_PERFORMANCE_TOOL_NAME,
    CONTENT_WEEKLY_PLAN_TOOL_NAME,
    ContentOperationTools,
    analyze_content_performance,
    compact_content_operation_result,
    generate_content_weekly_plan,
)
from tools.present_generation_ui import (
    GenerationUiPresenter,
    TrustedPresentationResultRegistry,
)
from tools.rpa_xiaohongshu import (
    save_xiaohongshu_draft,
    validate_content_image_paths,
)
from web_agent import app as web_app
from web_agent.app import create_app
from web_agent.conversations import SQLiteConversationStore


FIXTURE_ROOT = Path(__file__).resolve().parents[1] / "fixtures" / "content_operations_v0"


def test_weekly_plan_builds_five_reviewable_items_and_trusted_card() -> None:
    registry = TrustedPresentationResultRegistry()
    tools = ContentOperationTools(registry)
    result = tools.generate_weekly_plan("2026-08-17")
    presenter = GenerationUiPresenter(registry)
    ui = presenter.present(
        "content_weekly_plan",
        "柒昱美学获客内容周计划",
        result["presentation"]["result_ref"],
    )
    compact = compact_content_operation_result(result, ui_result=ui)
    serialized = serialize_tool_result(
        compact, max_chars=DEFAULT_TOOL_RESULT_MAX_CHARS
    )

    assert result["success"] is True
    assert len(result["plan"]["contents"]) == 5
    assert result["plan"]["source_mode"] == "离线演示线索"
    assert all(item["source_url"] for item in result["plan"]["contents"])
    assert ui["success"] is True
    artifact = presenter.artifacts[0]
    assert "请选择 1～9 张图片" in json.dumps(artifact["spec"], ensure_ascii=False)
    assert [item["id"] for item in artifact["spec"]["actions"]] == [
        "review_content_plan",
        "save_xiaohongshu_draft",
    ]
    assert "最终发布" not in {item["id"] for item in artifact["spec"]["actions"]}
    assert len(serialized) <= DEFAULT_TOOL_RESULT_MAX_CHARS
    assert json.loads(serialized).get("truncated") is not True
    assert CONTENT_WEEKLY_PLAN_TOOL_NAME in TOOL_DEFINITIONS
    assert DEFAULT_TOOL_BUDGETS[CONTENT_WEEKLY_PLAN_TOOL_NAME] == 1
    assert "小红书草稿" in SYSTEM_PROMPT


def test_weekly_plan_rejects_non_monday_and_private_sources() -> None:
    with pytest.raises(ValueError, match="周一"):
        generate_content_weekly_plan("2026-08-18")
    with pytest.raises(ValueError, match="内网"):
        generate_content_weekly_plan(
            "2026-08-17",
            source_items=[
                {
                    "title": "私有来源",
                    "url": "http://127.0.0.1/internal",
                    "summary": "不应进入公开来源。",
                }
            ],
        )


def test_performance_csv_produces_bounded_deterministic_advice() -> None:
    result = analyze_content_performance(
        "fixtures/content_operations_v0/content_performance.csv"
    )
    decisions = {
        item["content_id"]: item["decision"] for item in result["contents"]
    }

    assert decisions == {
        "CO-20260817-01": "建议小额投流",
        "CO-20260817-02": "继续观察",
        "CO-20260817-03": "继续观察",
        "CO-20260817-04": "建议停止",
        "CO-20260817-05": "继续观察",
    }
    assert "不创建订单" in result["spend_boundary"]
    assert CONTENT_PERFORMANCE_TOOL_NAME in TOOL_DEFINITIONS


def test_content_images_are_limited_to_valid_files_in_controlled_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "content_assets"
    root.mkdir()
    image_path = root / "case.png"
    Image.new("RGB", (32, 32), "white").save(image_path)
    monkeypatch.setattr("tools.rpa_xiaohongshu.RPA_XHS_IMAGE_ROOT", root)

    assert validate_content_image_paths(str(image_path)) == [str(image_path.resolve())]

    outside = tmp_path / "outside.png"
    Image.new("RGB", (32, 32), "white").save(outside)
    with pytest.raises(ValueError, match="受控目录"):
        validate_content_image_paths(str(outside))

    link = root / "linked.png"
    link.symlink_to(outside)
    with pytest.raises(ValueError, match="符号链接"):
        validate_content_image_paths(str(link))


def test_content_image_upload_stores_valid_image_in_controlled_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    image_root = tmp_path / "content_assets"
    monkeypatch.setattr("tools.rpa_xiaohongshu.RPA_XHS_IMAGE_ROOT", image_root)
    content = BytesIO()
    Image.new("RGB", (32, 32), "white").save(content, format="PNG")
    app = create_app(
        agent_factory=lambda _trace, _scope: None,
        conversation_store=SQLiteConversationStore(tmp_path / "web.sqlite3"),
    )
    client = TestClient(app)

    response = client.post(
        "/api/content-assets/images",
        params={"filename": "case.png"},
        content=content.getvalue(),
        headers={"Content-Type": "image/png"},
    )

    assert response.status_code == 201
    payload = response.json()
    path = Path(payload["path"])
    assert payload["filename"] == "case.png"
    assert path.parent == (image_root / "uploads").resolve()
    assert path.suffix == ".png"
    assert path.is_file()
    assert validate_content_image_paths(payload["path"]) == [str(path)]


def test_content_image_upload_accepts_camera_mpo_jpg_as_standard_jpeg(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    image_root = tmp_path / "content_assets"
    monkeypatch.setattr("tools.rpa_xiaohongshu.RPA_XHS_IMAGE_ROOT", image_root)
    content = BytesIO()
    Image.new("RGB", (32, 32), "white").save(
        content,
        format="MPO",
        save_all=True,
        append_images=[Image.new("RGB", (32, 32), "black")],
    )
    app = create_app(
        agent_factory=lambda _trace, _scope: None,
        conversation_store=SQLiteConversationStore(tmp_path / "web.sqlite3"),
    )
    client = TestClient(app)

    response = client.post(
        "/api/content-assets/images",
        params={"filename": "camera.JPG"},
        content=content.getvalue(),
        headers={"Content-Type": "image/jpeg"},
    )

    assert response.status_code == 201
    path = Path(response.json()["path"])
    assert path.suffix == ".jpg"
    with Image.open(path) as stored:
        assert stored.format == "JPEG"
    assert validate_content_image_paths(str(path)) == [str(path)]


def test_content_image_upload_rejects_invalid_bytes_and_extension(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        "tools.rpa_xiaohongshu.RPA_XHS_IMAGE_ROOT",
        tmp_path / "content_assets",
    )
    app = create_app(
        agent_factory=lambda _trace, _scope: None,
        conversation_store=SQLiteConversationStore(tmp_path / "web.sqlite3"),
    )
    client = TestClient(app)

    invalid = client.post(
        "/api/content-assets/images",
        params={"filename": "fake.jpg"},
        content=b"not-an-image",
        headers={"Content-Type": "image/jpeg"},
    )
    unsupported = client.post(
        "/api/content-assets/images",
        params={"filename": "fake.gif"},
        content=b"not-an-image",
        headers={"Content-Type": "image/gif"},
    )

    assert invalid.status_code == 422
    assert invalid.json()["detail"]["error_type"] == "INVALID_CONTENT_IMAGE"
    assert unsupported.status_code == 415
    assert unsupported.json()["detail"]["error_type"] == "UNSUPPORTED_CONTENT_IMAGE_TYPE"


def test_xiaohongshu_runner_only_reports_verified_draft() -> None:
    captured: dict[str, object] = {}

    class FakeModule:
        def __init__(self, config) -> None:
            captured["config"] = config

        def __enter__(self):
            return self

        def __exit__(self, *_args) -> None:
            return None

        def health(self):
            return SimpleNamespace(
                platform_supported=True,
                chrome_available=True,
                database_ready=True,
            )

        def submit(self, workflow, payload):
            captured["workflow"] = workflow
            captured["payload"] = payload
            return SimpleNamespace(id="job-1")

        def wait(self, job_id, timeout):
            captured["timeout"] = timeout
            return SimpleNamespace(
                id=job_id,
                status="succeeded",
                result={
                    "result_type": "xiaohongshu_draft",
                    "url": "https://creator.xiaohongshu.com/draft",
                    "image_count": 1,
                    "message": "草稿箱已核验。",
                },
                error_code=None,
                error_message=None,
            )

    result = save_xiaohongshu_draft(
        title="标题",
        body="正文",
        tags=["杭州装修"],
        image_paths=["/controlled/case.png"],
        module_factory=FakeModule,
    )

    assert result["success"] is True
    assert result["final_publish_performed"] is False
    assert captured["workflow"] == "xiaohongshu_draft"
    assert captured["payload"] == {
        "title": "标题",
        "body": "正文",
        "tags": ["杭州装修"],
        "image_paths": ["/controlled/case.png"],
    }


def test_xiaohongshu_runner_surfaces_user_action_without_retry() -> None:
    calls = {"submit": 0}

    class FakeModule:
        def __init__(self, _config) -> None:
            pass

        def __enter__(self):
            return self

        def __exit__(self, *_args) -> None:
            return None

        def health(self):
            return SimpleNamespace(
                platform_supported=True,
                chrome_available=True,
                database_ready=True,
            )

        def submit(self, _workflow, _payload):
            calls["submit"] += 1
            return SimpleNamespace(id="job-login")

        def wait(self, job_id, timeout):
            return SimpleNamespace(
                id=job_id,
                status="needs_user_action",
                result=None,
                error_code="XHS_LOGIN_REQUIRED",
                error_message="请在专用 Chrome 中登录小红书。",
            )

    result = save_xiaohongshu_draft(
        title="标题",
        body="正文",
        tags=[],
        image_paths=["/controlled/case.png"],
        module_factory=FakeModule,
    )

    assert result["success"] is False
    assert result["needs_user_action"] is True
    assert result["final_publish_performed"] is False
    assert calls["submit"] == 1


def test_default_web_agent_auto_presents_content_cards(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        web_app.LLMClient,
        "from_env",
        classmethod(lambda cls: object()),
    )
    monkeypatch.setattr(web_app, "build_memory_client_from_env", lambda: None)
    agent = web_app._default_agent_factory(
        lambda _message: None,
        web_app.AttachmentRetrievalScope(),
        mcp_tools=None,
    )

    plan = agent.tool_handlers[CONTENT_WEEKLY_PLAN_TOOL_NAME]("2026-08-17")
    review = agent.tool_handlers[CONTENT_PERFORMANCE_TOOL_NAME](
        "fixtures/content_operations_v0/content_performance.csv"
    )

    assert plan["ui_presented"] is True
    assert review["ui_presented"] is True
    assert [item["scenario"] for item in agent.generation_ui_artifacts] == [
        "content_weekly_plan",
        "content_performance_review",
    ]


def test_content_review_and_fake_rpa_round_trip_through_web_api(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _result, artifact = _weekly_plan_artifact()
    store = SQLiteConversationStore(tmp_path / "web.sqlite3")
    image_root = tmp_path / "content_assets"
    image_root.mkdir()
    image_path = image_root / "case.png"
    Image.new("RGB", (32, 32), "white").save(image_path)
    monkeypatch.setattr("tools.rpa_xiaohongshu.RPA_XHS_IMAGE_ROOT", image_root)
    rpa_calls: list[dict[str, object]] = []

    def fake_save_xiaohongshu_draft(**payload):
        rpa_calls.append(payload)
        return {
            "success": True,
            "job_id": "fake-job-1",
            "status": "succeeded",
            "message": "离线假 RPA 已核验草稿。",
            "final_publish_performed": False,
        }

    monkeypatch.setattr(
        "web_agent.app.save_xiaohongshu_draft", fake_save_xiaohongshu_draft
    )

    class FakeAgent:
        last_context_receipt = None
        last_tool_results: list[dict[str, object]] = []

        def __init__(self, trace) -> None:
            self.trace = trace
            self.generation_ui_artifacts = [artifact]

        def run(self, task, *, history=None, episode_id=None):
            return "内容周计划已生成。"

    app = create_app(
        agent_factory=lambda trace, _scope: FakeAgent(trace),
        conversation_store=store,
    )
    client = TestClient(app)
    chat = client.post("/api/chat", json={"message": "生成下周内容计划"}).json()
    stored = chat["artifacts"][0]
    base = {
        "conversation_id": chat["conversation_id"],
        "message_id": int(stored["message_id"]),
        "artifact_id": stored["artifact_id"],
        "scenario": "content_weekly_plan",
        "event_type": "confirm",
    }
    review = client.post(
        "/api/generation/event",
        json={
            **base,
            "action_id": "review_content_plan",
            "arguments": {
                "content_id": "CO-20260817-01",
                "decision": "确认内容",
                "note": "已核对文案和素材",
                "image_paths_text": str(image_path),
            },
            "idempotency_key": str(uuid4()),
        },
    )
    rpa_key = str(uuid4())
    rpa_request = {
        **base,
        "action_id": "save_xiaohongshu_draft",
        "arguments": {"content_id": "CO-20260817-01"},
        "idempotency_key": rpa_key,
    }
    saved = client.post("/api/generation/event", json=rpa_request)
    repeated = client.post("/api/generation/event", json=rpa_request)

    assert review.status_code == 200
    assert review.json()["status"] == "approved"
    assert saved.status_code == 200
    assert saved.json()["status"] == "draft_saved"
    assert repeated.json() == saved.json()
    assert len(rpa_calls) == 1
    assert rpa_calls[0]["image_paths"] == [str(image_path.resolve())]
    restored = client.get(f"/api/conversations/{chat['conversation_id']}").json()
    restored_contents = restored["messages"][1]["artifacts"][0]["data"]["contents"]
    restored_item = restored_contents[0]
    assert restored_item["status"] == "approved"
    assert restored_item["rpa_status"] == "draft_saved"
    assert restored_item["rpa_job_id"] == "fake-job-1"


def _weekly_plan_artifact() -> tuple[dict, dict]:
    registry = TrustedPresentationResultRegistry()
    result = ContentOperationTools(registry).generate_weekly_plan("2026-08-17")
    presenter = GenerationUiPresenter(registry)
    presented = presenter.present(
        "content_weekly_plan",
        "柒昱美学获客内容周计划",
        result["presentation"]["result_ref"],
    )
    assert presented["success"] is True
    return result, presenter.artifacts[0]
