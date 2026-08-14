from __future__ import annotations

import stat
import sqlite3
from uuid import UUID, uuid4

import pytest

from web_agent.conversations import ConversationNotFoundError, SQLiteConversationStore


OFFICE_KNOWLEDGE_BASE_ID = "21d66b91-701a-4e33-9f66-877244fae693"
PDF_KNOWLEDGE_BASE_ID = "11d66b91-701a-4e33-9f66-877244fae693"


def test_sqlite_conversation_store_persists_complete_exchanges(tmp_path) -> None:
    path = tmp_path / "conversations.sqlite3"
    store = SQLiteConversationStore(path)
    conversation_id = store.create()
    store.append_exchange(conversation_id, "你好", "你好，我是 Agent。")

    reopened = SQLiteConversationStore(path)

    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert reopened.messages(conversation_id) == [
        {"role": "user", "content": "你好"},
        {"role": "assistant", "content": "你好，我是 Agent。"},
    ]


def test_sqlite_conversation_store_keeps_stopped_turn_out_of_model_history(
    tmp_path,
) -> None:
    path = tmp_path / "conversations.sqlite3"
    store = SQLiteConversationStore(path)
    conversation_id = store.create()
    turn_id = store.begin_turn(conversation_id, "不要删除这个问题")

    store.stop_turn(
        conversation_id,
        turn_id,
        partial_answer="已经生成的部分回答",
        trace=["Step 1: 调用模型"],
        duration_ms=42,
    )

    records = SQLiteConversationStore(path).message_records(conversation_id)
    assert [(item.role, item.content, item.status) for item in records] == [
        ("user", "不要删除这个问题", "stopped"),
        ("assistant", "已经生成的部分回答", "stopped"),
    ]
    assert records[1].trace == ("Step 1: 调用模型",)
    assert records[1].duration_ms == 42
    assert store.messages(conversation_id) == []


def test_sqlite_conversation_store_persists_assistant_execution_details(tmp_path) -> None:
    path = tmp_path / "conversations.sqlite3"
    store = SQLiteConversationStore(path)
    conversation_id = store.create()
    store.append_exchange(
        conversation_id,
        "运行代码",
        "执行完成。",
        trace=["Step 1: 调用模型", "Tool: run_code", "Step 1: 模型给出最终回答"],
        duration_ms=125,
        context={"history_message_count": 0, "memory_policy": "disabled"},
    )

    records = SQLiteConversationStore(path).message_records(conversation_id)

    assert records[0].trace == ()
    assert records[0].duration_ms is None
    assert records[1].trace == (
        "Step 1: 调用模型",
        "Tool: run_code",
        "Step 1: 模型给出最终回答",
    )
    assert records[1].duration_ms == 125
    assert records[1].context == {
        "history_message_count": 0,
        "memory_policy": "disabled",
    }


def test_sqlite_conversation_store_appends_ui_event_result_without_user_message(
    tmp_path,
) -> None:
    store = SQLiteConversationStore(tmp_path / "conversations.sqlite3")
    conversation_id = store.create()
    store.append_exchange(conversation_id, "计算材料", "请填写参数。")

    store.append_assistant_response(
        conversation_id,
        "计算完成。",
        trace=["Step 1: 调用模型"],
        duration_ms=28,
        context={"memory_policy": "skip"},
    )

    records = store.message_records(conversation_id)
    assert [(item.role, item.content) for item in records] == [
        ("user", "计算材料"),
        ("assistant", "请填写参数。"),
        ("assistant", "计算完成。"),
    ]
    assert records[2].turn_id is None
    assert records[2].trace == ("Step 1: 调用模型",)


def test_sqlite_conversation_store_keeps_messages_from_database_without_details_table(
    tmp_path,
) -> None:
    path = tmp_path / "legacy.sqlite3"
    conversation_id = str(uuid4())
    with sqlite3.connect(path) as connection:
        connection.executescript(
            """
            CREATE TABLE conversations (
                conversation_id TEXT PRIMARY KEY,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE messages (
                message_id INTEGER PRIMARY KEY AUTOINCREMENT,
                conversation_id TEXT NOT NULL REFERENCES conversations(conversation_id)
                    ON DELETE CASCADE,
                role TEXT NOT NULL CHECK (role IN ('user', 'assistant')),
                content TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            """
        )
        connection.execute(
            "INSERT INTO conversations VALUES (?, ?, ?)",
            (conversation_id, "2026-08-13T00:00:00+00:00", "2026-08-13T00:00:00+00:00"),
        )
        connection.execute(
            "INSERT INTO messages (conversation_id, role, content, created_at) VALUES (?, ?, ?, ?)",
            (conversation_id, "assistant", "旧回答", "2026-08-13T00:00:00+00:00"),
        )

    records = SQLiteConversationStore(path).message_records(conversation_id)

    assert len(records) == 1
    assert records[0].content == "旧回答"
    assert records[0].trace == ()
    assert records[0].duration_ms is None
    assert records[0].context is None


def test_sqlite_conversation_store_lists_recent_conversation_summaries(
    tmp_path,
) -> None:
    store = SQLiteConversationStore(tmp_path / "conversations.sqlite3")
    empty_conversation_id = store.create()
    active_conversation_id = store.create()
    first_message = "  帮我   分析本月经营数据，并给出异常原因和改进建议  "
    store.append_exchange(active_conversation_id, first_message, "好的。")

    summaries = store.conversation_summaries()

    assert [item.conversation_id for item in summaries] == [
        active_conversation_id,
        empty_conversation_id,
    ]
    assert summaries[0].title == "帮我 分析本月经营数据，并给出异常原因和改进建议"
    assert summaries[0].message_count == 2
    assert summaries[1].title == "新对话"
    assert summaries[1].message_count == 0


def test_sqlite_conversation_store_persists_custom_title_and_migrates_schema(
    tmp_path,
) -> None:
    path = tmp_path / "conversations.sqlite3"
    store = SQLiteConversationStore(path)
    conversation_id = store.create()
    store.append_exchange(conversation_id, "原始问题", "原始回答")

    store.rename(conversation_id, "  每周经营复盘  ")

    reopened = SQLiteConversationStore(path)
    summary = reopened.conversation_summaries()[0]
    assert summary.title == "每周经营复盘"
    with sqlite3.connect(path) as connection:
        columns = {
            str(row[1])
            for row in connection.execute("PRAGMA table_info(conversations)")
        }
    assert "custom_title" in columns


def test_sqlite_conversation_store_limits_and_truncates_summaries(tmp_path) -> None:
    store = SQLiteConversationStore(tmp_path / "conversations.sqlite3")
    first_conversation_id = store.create()
    second_conversation_id = store.create()
    store.append_exchange(first_conversation_id, "第一段对话", "回答")
    store.append_exchange(
        second_conversation_id,
        "这是一段明确超过三十六个字符的对话标题用于验证左侧列表不会无限变长而破坏整个页面布局",
        "回答",
    )

    summaries = store.conversation_summaries(limit=1)

    assert len(summaries) == 1
    assert summaries[0].conversation_id == second_conversation_id
    assert len(summaries[0].title) == 36
    assert summaries[0].title.endswith("…")
    with pytest.raises(ValueError):
        store.conversation_summaries(limit=0)


def test_sqlite_conversation_store_persists_safe_message_resources(tmp_path) -> None:
    store = SQLiteConversationStore(tmp_path / "conversations.sqlite3")
    conversation_id = store.create()

    message_id = store.append_exchange(
        conversation_id,
        "生成报告",
        "报告已生成。",
        resources=[
            {
                "kind": "artifact",
                "title": "report.pdf",
                "path": "artifacts/report.pdf",
                "media_type": "application/pdf",
            },
            {
                "kind": "source",
                "title": "官方说明",
                "url": "https://example.com/docs",
            },
            {
                "kind": "evidence",
                "title": "合同.pdf · 第 3 页",
                "subtitle": "付款节点原文",
            },
        ],
    )

    resources = store.conversation_resources(conversation_id)
    assert [item.message_id for item in resources] == [message_id] * 3
    assert resources[0].path == "artifacts/report.pdf"
    assert resources[1].url == "https://example.com/docs"
    assert resources[2].kind == "evidence"


@pytest.mark.parametrize(
    "resource",
    [
        {"kind": "artifact", "title": "secret", "path": "artifacts/../secret"},
        {"kind": "artifact", "title": "secret", "path": "/tmp/secret"},
        {"kind": "source", "title": "unsafe", "url": "javascript:alert(1)"},
    ],
)
def test_sqlite_conversation_store_rejects_unsafe_message_resource(
    tmp_path,
    resource,
) -> None:
    store = SQLiteConversationStore(tmp_path / "conversations.sqlite3")
    conversation_id = store.create()

    with pytest.raises(ValueError):
        store.append_exchange(
            conversation_id,
            "生成报告",
            "报告已生成。",
            resources=[resource],
        )

    assert store.messages(conversation_id) == []


def test_sqlite_conversation_store_persists_generation_ui_artifact(tmp_path) -> None:
    store = SQLiteConversationStore(tmp_path / "conversations.sqlite3")
    conversation_id = store.create()
    artifact_id = str(uuid4())

    message_id = store.append_exchange(
        conversation_id,
        "展示销售数据",
        "这是销售简报。",
        ui_artifacts=[
            {
                "type": "generation_ui",
                "artifact_id": artifact_id,
                "scenario": "sales_briefing",
                "text": "这是销售简报。",
                "spec": {
                    "version": "0.1",
                    "scenario": "sales_briefing",
                    "root": {"id": "root", "type": "Column", "props": {}, "children": []},
                },
                "data": {"metrics": {"sales": 100}},
            }
        ],
    )

    restored = SQLiteConversationStore(store.path)
    artifacts = restored.generation_ui_artifacts(conversation_id)
    assert len(artifacts) == 1
    assert artifacts[0].artifact_id == artifact_id
    assert artifacts[0].message_id == message_id
    assert artifacts[0].spec["root"]["type"] == "Column"
    assert artifacts[0].data["metrics"]["sales"] == 100
    assert UUID(artifacts[0].conversation_id)


def test_sqlite_conversation_store_delete_removes_messages(tmp_path) -> None:
    store = SQLiteConversationStore(tmp_path / "conversations.sqlite3")
    conversation_id = store.create()
    store.append_exchange(conversation_id, "你好", "你好")

    store.delete(conversation_id)

    with pytest.raises(ConversationNotFoundError):
        store.messages(conversation_id)


def test_sqlite_conversation_store_persists_scoped_attachments(tmp_path) -> None:
    path = tmp_path / "conversations.sqlite3"
    store = SQLiteConversationStore(path)
    conversation_id = store.create()
    attachment = store.create_attachment(
        conversation_id,
        knowledge_base_kind="office",
        knowledge_base_id=OFFICE_KNOWLEDGE_BASE_ID,
        filename="synthetic.md",
        content_type="text/markdown",
        size_bytes=42,
    )
    document_id = str(uuid4())
    job_id = str(uuid4())

    store.mark_attachment_uploaded(
        conversation_id,
        attachment.attachment_id,
        document_id=document_id,
        job_id=job_id,
    )
    store.update_attachment_status(
        conversation_id,
        attachment.attachment_id,
        status="READY",
        progress=1,
    )

    reopened = SQLiteConversationStore(path)
    restored = reopened.attachments(conversation_id)
    assert len(restored) == 1
    assert restored[0].filename == "synthetic.md"
    assert restored[0].knowledge_base_kind == "office"
    assert restored[0].knowledge_base_id == OFFICE_KNOWLEDGE_BASE_ID
    assert restored[0].status == "READY"
    assert reopened.document_reference_count(document_id) == 1


def test_sqlite_conversation_delete_cascades_attachments(tmp_path) -> None:
    store = SQLiteConversationStore(tmp_path / "conversations.sqlite3")
    conversation_id = store.create()
    store.create_attachment(
        conversation_id,
        knowledge_base_kind="pdf",
        knowledge_base_id=PDF_KNOWLEDGE_BASE_ID,
        filename="temporary.pdf",
        content_type="application/pdf",
        size_bytes=10,
    )

    store.delete(conversation_id)

    with pytest.raises(ConversationNotFoundError):
        store.attachments(conversation_id)


def test_sqlite_conversation_store_marks_legacy_attachment_for_reupload(
    tmp_path,
) -> None:
    path = tmp_path / "legacy.sqlite3"
    conversation_id = str(uuid4())
    attachment_id = str(uuid4())
    now = "2026-08-11T00:00:00+00:00"
    with sqlite3.connect(path) as connection:
        connection.executescript(
            """
            CREATE TABLE conversations (
                conversation_id TEXT PRIMARY KEY,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE attachments (
                attachment_id TEXT PRIMARY KEY,
                conversation_id TEXT NOT NULL,
                document_id TEXT,
                job_id TEXT,
                filename TEXT NOT NULL,
                content_type TEXT NOT NULL,
                size_bytes INTEGER NOT NULL,
                status TEXT NOT NULL,
                progress REAL NOT NULL,
                error_message TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            """
        )
        connection.execute(
            "INSERT INTO conversations VALUES (?, ?, ?)",
            (conversation_id, now, now),
        )
        connection.execute(
            """
            INSERT INTO attachments VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                attachment_id,
                conversation_id,
                str(uuid4()),
                str(uuid4()),
                "legacy.pdf",
                "application/pdf",
                10,
                "READY",
                1,
                None,
                now,
                now,
            ),
        )

    restored = SQLiteConversationStore(path).attachments(conversation_id)[0]

    assert restored.knowledge_base_kind is None
    assert restored.knowledge_base_id is None
    assert restored.status == "FAILED"
    assert restored.error_message == "ATTACHMENT_ROUTE_MIGRATION_REQUIRED"
