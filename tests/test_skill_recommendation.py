"""推荐只读、模型输出校验，以及 Web 普通对话与显式选择的执行边界。"""

import json
from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from agent import skill_recommendation
from agent.loop import AgentLoop, DEFAULT_TOOL_HANDLERS
from agent.skill_recommendation import recommend_skill
from agent.skill_tools import register_skill_tools
from agent.skills import SkillCatalog, SkillConfigurationError
from testagent_dev_agent.paths import REPOSITORY_ROOT
from web_agent.app import create_app
from web_agent.conversations import SQLiteConversationStore


@pytest.fixture
def catalog():
    return SkillCatalog.from_directory(REPOSITORY_ROOT / ".agents" / "skills")


def test_rule_match_needs_no_model(catalog, monkeypatch):
    def unexpected(*args):
        pytest.fail("rule match must not invoke model")

    monkeypatch.setattr(skill_recommendation, "_recommend_from_model", unexpected)
    result = recommend_skill("请执行网络数据研究与选题", catalog)
    assert result.skill_name == "network-research-and-topic-selection"
    assert result.source == "rule"
    with pytest.raises(SkillConfigurationError):
        recommend_skill("$unknown-skill", catalog)
    with pytest.raises(SkillConfigurationError):
        recommend_skill("$operation-goal-setting $network-research-and-topic-selection", catalog)
    assert recommend_skill("你好", SkillCatalog()).skill_name is None


def test_model_receives_only_metadata_and_current_question(catalog, monkeypatch):
    requests = []
    options = []

    class Client:
        def __init__(self, **kwargs):
            options.append(kwargs)
            self.chat = SimpleNamespace(completions=self)

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def create(self, **kwargs):
            requests.append(kwargs)
            return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(
                content='{"skill_name":"operation-goal-setting","reason":"需要制定小红书周期运营目标。"}',
                tool_calls=None,
            ))])

    monkeypatch.setenv("OPENAI_API_KEY", "test-only")
    monkeypatch.setenv("OPENAI_MODEL", "test-only")
    monkeypatch.setattr(skill_recommendation, "OpenAI", Client)
    result = recommend_skill("为企业制定下个月的小红书运营目标", catalog)
    assert result.skill_name == "operation-goal-setting"
    assert result.source == "model"
    assert len(requests) == 1
    assert "tools" not in requests[0]
    data = json.loads(requests[0]["messages"][1]["content"])
    assert set(data) == {"task", "candidates"}
    assert all(set(item) == {"name", "description"} for item in data["candidates"])
    assert options[0]["timeout"] == 15.0
    assert options[0]["max_retries"] == 0


@pytest.mark.parametrize("model_result", [
    '{"skill_name":"not-installed","reason":"test"}',
    '{"skill_name":12,"reason":"test"}',
    '{"skill_name":null,"reason":"   "}',
    '{"skill_name":null,"reason":"test","tool_calls":[]}',
    "not json",
    "[]",
    RuntimeError("provider-secret-must-not-be-returned"),
])
def test_recommendation_failure_is_safe_and_creates_no_turn(tmp_path, monkeypatch, model_result):
    def predict(*args):
        if isinstance(model_result, Exception):
            raise model_result
        return model_result

    def unexpected_agent(*args):
        pytest.fail("recommendation must not construct or execute an Agent")

    monkeypatch.setattr(skill_recommendation, "_recommend_from_model", predict)
    client = TestClient(create_app(
        unexpected_agent,
        conversation_store=SQLiteConversationStore(tmp_path / "conversations.sqlite3"),
    ))
    result = client.post("/api/skills/recommend", json={"message": "请帮我制定目标"})
    assert result.status_code == 200
    assert result.json()["skill_name"] is None
    assert result.json()["source"] == "unavailable"
    assert "provider-secret" not in result.text
    assert client.get("/api/conversations").json()["conversations"] == []


def test_recommendation_no_match_and_request_validation(tmp_path, monkeypatch):
    monkeypatch.setattr(skill_recommendation, "_recommend_from_model", lambda *args:
        '{"skill_name":null,"reason":"当前是普通问答，无需业务 Skill。"}')
    client = TestClient(create_app(
        lambda *args: pytest.fail("must not execute Agent"),
        conversation_store=SQLiteConversationStore(tmp_path / "conversations.sqlite3"),
    ))
    result = client.post("/api/skills/recommend", json={"message": "你好"})
    assert result.json()["skill_name"] is None
    assert result.json()["source"] == "model"
    for payload in [{"message": " "}, {"message": "x" * 20_001},
                    {"message": "你好", "history": []}]:
        assert client.post("/api/skills/recommend", json=payload).status_code == 422
    assert client.post("/api/skills/recommend", json={"message": "$missing"}).status_code == 400


@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.parametrize("explicit", [False, True])
def test_custom_host_without_router_does_not_implicitly_activate_and_research_still_requires_confirmation(tmp_path, catalog, stream, explicit):
    requests = []

    class Client:
        def complete(self, messages, tools=None):
            requests.append((messages, tools))
            return SimpleNamespace(
                content="完成", tool_calls=None,
                model_dump=lambda **kwargs: {"role": "assistant", "content": "完成"},
            )

        def stream_complete(self, messages, tools=None, **kwargs):
            return self.complete(messages, tools)

    def factory(trace, scope):
        return AgentLoop(
            Client(), trace=trace, skill_catalog=catalog,
            tool_handlers=register_skill_tools(DEFAULT_TOOL_HANDLERS, catalog),
        )

    client = TestClient(create_app(
        factory,
        conversation_store=SQLiteConversationStore(tmp_path / "conversations.sqlite3"),
    ))
    payload = {"message": "$network-research-and-topic-selection 网络数据研究与选题"}
    if explicit:
        payload["skill_name"] = "network-research-and-topic-selection"
    if stream:
        payload["request_id"] = str(uuid4())
    response = client.post("/api/chat/stream" if stream else "/api/chat", json=payload)
    assert response.status_code == 200
    if explicit:
        assert "尚未调用研究接口" in response.text
        assert requests == []
        return
    assert "完成" in response.text
    assert len(requests) == 1
    messages, tools = requests[0]
    assert "Skill 名称：network-research-and-topic-selection" not in messages[0]["content"]
    names = {item["function"]["name"] for item in tools or []}
    assert "social_search_posts" not in names
