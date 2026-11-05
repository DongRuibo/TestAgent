"""小红书公开数据 Tools 与 GUAIKEI CLI 适配器的离线测试。"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from testagent_dev_agent.paths import REPOSITORY_ROOT
from tools.guaikei import CLI_BOOTSTRAP, GuaikeiCli, GuaikeiError
from tools.social_research import SocialResearchTools


class FixtureProvider:
    def __init__(self) -> None:
        path = (
            REPOSITORY_ROOT
            / "fixtures"
            / "content_research_v0"
            / "guaikei_cli_samples.json"
        )
        self.payload = json.loads(path.read_text("utf-8"))

    def search(self, keyword: str) -> dict[str, Any]:
        return self.payload["search"]

    def detail(self, url: str) -> dict[str, Any]:
        return self.payload["detail"]

    def comments(self, url: str, limit: int) -> dict[str, Any]:
        return self.payload["comments"]

    def creator_posts(self, url: str, limit: int) -> dict[str, Any]:
        return self.payload["creator_posts"]


def _cli_root(tmp_path: Path) -> Path:
    root = tmp_path / "guaikei"
    scripts = root / "src" / "xiaohongshu"
    scripts.mkdir(parents=True)
    for name in ("search-cli.js", "detail-cli.js", "comment-cli.js", "post-cli.js"):
        (scripts / name).write_text("// fixture", "utf-8")
    return root


def test_social_tools_normalize_fixed_fixture() -> None:
    tools = SocialResearchTools(FixtureProvider())

    search = tools.search_posts(" 小户型   改造 ", limit=1)
    detail = tools.get_post_detail(
        "https://www.xiaohongshu.com/explore/note-001"
    )
    comments = tools.get_comments(
        "https://www.xiaohongshu.com/explore/note-001", limit=1
    )
    creator = tools.get_creator_posts(
        "https://www.xiaohongshu.com/user/profile/creator-001", limit=1
    )

    assert search["success"] is True
    assert search["query"] == "小户型 改造"
    assert search["items"][0]["platform"] == "xiaohongshu"
    assert search["items"][0]["favorites"] == 96
    assert search["items"][0]["views"] is None
    assert search["items"][0]["collected_at"]
    assert detail["items"][0]["url"].endswith("note-001")
    assert comments["items"][0]["text"].startswith("想知道")
    assert creator["items"][0]["title"] == "旧房收纳改造前后对比"


def test_social_tools_accept_guaikei_ok_success_code() -> None:
    class OkProvider(FixtureProvider):
        def search(self, keyword: str) -> dict[str, Any]:
            return {**super().search(keyword), "error_code": "OK"}

    result = SocialResearchTools(OkProvider()).search_posts("杭州全屋定制")

    assert result["success"] is True
    assert result["items"][0]["post_id"] == "note-001"


def test_detail_preserves_observed_media_and_share_fields() -> None:
    provider = FixtureProvider()
    # 2026-09-10 实测响应结构；内容、身份和资源地址均为合成样例。
    provider.payload["detail"]["results"].update({
        "shared_count": "40",
        "type": "video",
        "image_list": ["https://example.com/cover.webp"],
        "video_url": ["http://example.com/video.mp4"],
        "tags": [{"id": "tag-1", "name": "收纳"}],
        "topics": [{"id": "topic-1", "name": "家居"}],
    })

    result = SocialResearchTools(provider).get_post_detail(
        "https://www.xiaohongshu.com/explore/note-001"
    )

    item = result["items"][0]
    assert item["shares"] == 40
    assert item["type"] == "video"
    assert item["image_urls"] == ["https://example.com/cover.webp"]
    assert item["video_urls"] == ["http://example.com/video.mp4"]
    assert item["tags"] == ["收纳"]
    assert item["topics"] == ["家居"]
    assert item["views"] is None


def test_creator_posts_accepts_observed_note_container_and_aliases() -> None:
    provider = FixtureProvider()
    provider.payload["creator_posts"]["results"] = {
        "user": {},
        "note": [{
            "id": f"note-{index}", "title": f"合成作品 {index}",
            "create_time": "2026-09-09 18:37:58", "collected": "349",
            "share": "40", "view": "0", "likes": "135", "comments": "5",
            "cover": "https://example.com/cover.webp",
            "images": ["https://example.com/cover.webp"],
            "video": ["http://example.com/video.mp4"],
            "video_url": "http://example.com/video.mp4", "video_duration": "102",
        } for index in range(3)],
    }

    result = SocialResearchTools(provider).get_creator_posts(
        "https://www.xiaohongshu.com/user/profile/creator-001", limit=3
    )

    assert result["item_count"] == 3
    item = result["items"][0]
    assert item["published_at"] == "2026-09-09 18:37:58"
    assert (item["favorites"], item["shares"], item["views"]) == (349, 40, 0)
    assert item["cover_url"] == "https://example.com/cover.webp"
    assert item["image_urls"] == ["https://example.com/cover.webp"]
    assert item["video_urls"] == ["http://example.com/video.mp4"]
    assert item["video_duration"] == 102


def test_creator_stats_uses_zero_limit_and_preserves_unknowns(monkeypatch) -> None:
    provider = FixtureProvider()
    calls = []

    def creator_posts(url, limit):
        calls.append((url, limit))
        return {"status": "success", "results": {"user": {
            "nickname": "合成博主", "desc": "合成简介", "follows": "89",
            "fans": "97868", "liked": "913995", "collected": "768909",
        }}}

    monkeypatch.setattr(provider, "creator_posts", creator_posts)
    url = "https://www.xiaohongshu.com/user/profile/creator-001"
    tools = SocialResearchTools(provider)
    result = tools.handlers()["social_get_creator_stats"](url=url)

    assert calls == [(url, 0)]
    assert result["success"] is True
    item = result["items"][0]
    assert item["author_name"] == "合成博主"
    assert item["description"] == "合成简介"
    assert (item["following"], item["followers"]) == (89, 97868)
    assert (item["likes"], item["favorites"]) == (913995, 768909)
    assert item["interactions"] is None
    assert item["url"] == url
    assert item["collected_at"]


@pytest.mark.parametrize("payload", [
    {"status": "success", "results": {"user": {}}},
    {"status": "success", "results": []},
])
def test_creator_stats_rejects_missing_profile(payload, monkeypatch) -> None:
    provider = FixtureProvider()
    monkeypatch.setattr(provider, "creator_posts", lambda *args: payload)

    result = SocialResearchTools(provider).get_creator_stats(
        "https://www.xiaohongshu.com/user/profile/creator-001"
    )

    assert result["success"] is False
    assert result["error_type"] == "INVALID_PROVIDER_RESPONSE"


@pytest.mark.parametrize("url", [
    "https://example.com/profile/creator-001",
    "https://www.xiaohongshu.com/explore/note-001",
])
def test_creator_stats_rejects_wrong_url_without_provider_call(url, monkeypatch) -> None:
    provider = FixtureProvider()
    def unexpected_call(*args):
        raise AssertionError("invalid URL must not reach the provider")
    monkeypatch.setattr(provider, "creator_posts", unexpected_call)

    result = SocialResearchTools(provider).get_creator_stats(url)

    assert result["success"] is False
    assert result["error_type"] == "INVALID_ARGUMENTS"


def test_social_tools_reject_non_xiaohongshu_url_without_provider_call() -> None:
    result = SocialResearchTools(FixtureProvider()).get_post_detail(
        "https://example.com/private"
    )

    assert result["success"] is False
    assert result["error_type"] == "INVALID_ARGUMENTS"


def test_social_tools_surface_retryable_provider_error() -> None:
    class LockedProvider(FixtureProvider):
        def search(self, keyword: str) -> dict[str, Any]:
            return {"status": "failed", "errcode": 423, "errmsg": "secret"}

    result = SocialResearchTools(LockedProvider()).search_posts("装修")

    assert result == {
        "success": False,
        "platform": "xiaohongshu",
        "provider": "guaikei_cli",
        "error_type": "PROVIDER_ERROR",
        "message": "GUAIKEI API 返回失败状态。",
        "retryable": True,
        "provider_code": "423",
    }


def test_guaikei_cli_requires_explicit_configuration() -> None:
    with pytest.raises(GuaikeiError, match="GUAIKEI_CLI_DIR") as captured:
        GuaikeiCli(cli_dir=None, api_token=None).search("装修")

    assert captured.value.error_type == "PROVIDER_NOT_CONFIGURED"


def test_guaikei_cli_uses_fixed_script_and_minimal_environment(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = _cli_root(tmp_path)
    captured: dict[str, Any] = {}

    def fake_run(command: list[str], **kwargs: Any) -> Any:
        captured["command"] = command
        captured.update(kwargs)
        return SimpleNamespace(
            returncode=0,
            stdout=json.dumps({"status": "success", "results": []}),
            stderr="",
        )

    monkeypatch.setattr("tools.guaikei.shutil.which", lambda value: "/usr/bin/node")
    monkeypatch.setattr("tools.guaikei.subprocess.run", fake_run)
    monkeypatch.setenv("UNRELATED_SECRET", "must-not-be-forwarded")
    cli = GuaikeiCli(cli_dir=root, api_token="test-token")

    result = cli.search("收纳")

    assert result["status"] == "success"
    assert captured["command"] == [
        "/usr/bin/node",
        "-e",
        CLI_BOOTSTRAP,
        str(root / "src" / "xiaohongshu" / "search-cli.js"),
        "--keyword",
        "收纳",
    ]
    assert captured["shell"] is False
    assert captured["env"]["GUAIKEI_API_TOKEN"] == "test-token"
    assert "UNRELATED_SECRET" not in captured["env"]


def test_cli_bootstrap_disables_creation_retry_and_preserves_arguments(tmp_path):
    root = _cli_root(tmp_path)
    config = root / "src/config"
    config.mkdir()
    (config / "constants.js").write_text("module.exports = {CREATE_MAX_ATTEMPTS: 3, QUERY_MAX_ATTEMPTS: 20};")
    (root / "src/xiaohongshu/search-cli.js").write_text("""
const config = require('../config/constants');
console.log(JSON.stringify({status:'success',create_attempts:config.CREATE_MAX_ATTEMPTS,
 query_attempts:config.QUERY_MAX_ATTEMPTS,args:process.argv.slice(2)}));
""")
    result = GuaikeiCli(cli_dir=root, api_token="test-token").search("模拟收纳")
    assert result["create_attempts"] == 1 and result["query_attempts"] == 20
    assert result["args"] == ["--keyword", "模拟收纳"]


def test_guaikei_cli_never_returns_non_json_output(tmp_path: Path, monkeypatch) -> None:
    root = _cli_root(tmp_path)
    monkeypatch.setattr("tools.guaikei.shutil.which", lambda value: "/usr/bin/node")
    monkeypatch.setattr(
        "tools.guaikei.subprocess.run",
        lambda *args, **kwargs: SimpleNamespace(
            returncode=0,
            stdout="banner only",
            stderr="token=test-token",
        ),
    )

    with pytest.raises(GuaikeiError, match="合法 JSON") as captured:
        GuaikeiCli(cli_dir=root, api_token="test-token").search("收纳")

    assert captured.value.error_type == "INVALID_PROVIDER_RESPONSE"
    assert "test-token" not in str(captured.value)


def test_guaikei_cli_surfaces_only_sanitized_provider_code(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = _cli_root(tmp_path)
    monkeypatch.setattr("tools.guaikei.shutil.which", lambda value: "/usr/bin/node")
    monkeypatch.setattr(
        "tools.guaikei.subprocess.run",
        lambda *args, **kwargs: SimpleNamespace(
            returncode=1,
            stdout=json.dumps(
                {
                    "status": "error",
                    "error_code": "ERRCODE_18",
                    "message": "token=test-token",
                }
            ),
            stderr="token=test-token",
        ),
    )

    with pytest.raises(GuaikeiError, match="provider_code=ERRCODE_18") as captured:
        GuaikeiCli(cli_dir=root, api_token="test-token").detail(
            "https://www.xiaohongshu.com/explore/note-001"
        )

    assert captured.value.provider_code == "ERRCODE_18"
    assert captured.value.retryable is False
    assert "test-token" not in str(captured.value)


@pytest.mark.parametrize("stderr, expected", [
    (b"private=test-token", "PROVIDER_TIMEOUT"),
    ("搜索任务创建成功, 正在搜索中... private=test-token".encode(), "PROVIDER_RESULT_PENDING"),
])
def test_guaikei_timeout_never_invites_duplicate_creation(tmp_path, monkeypatch, stderr, expected):
    import subprocess
    root = _cli_root(tmp_path)
    monkeypatch.setattr("tools.guaikei.shutil.which", lambda value: "/usr/bin/node")
    def timeout(*args, **kwargs):
        raise subprocess.TimeoutExpired("node", 45, stderr=stderr)
    monkeypatch.setattr("tools.guaikei.subprocess.run", timeout)
    with pytest.raises(GuaikeiError) as caught:
        GuaikeiCli(cli_dir=root, api_token="test-token").search("收纳")
    assert caught.value.error_type == expected
    assert caught.value.retryable is False
    assert "test-token" not in str(caught.value)
