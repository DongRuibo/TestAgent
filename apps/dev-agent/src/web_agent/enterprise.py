"""单企业部署的确认档案与任务快照，不是模型 Memory。"""
import hashlib
import json
from datetime import datetime, timezone
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class EnterpriseUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    expected_version: int = Field(ge=0, strict=True)
    company_name: str = Field(min_length=1, max_length=200)
    company_profile: str = Field(min_length=1, max_length=6000)
    document_ids: list[UUID] = Field(default_factory=list, max_length=10)
    confirmed: bool = False


class EnterpriseStore:
    def __init__(self, conversations):
        self.conversations = conversations
        with conversations._connect() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS enterprise_profiles (
                    version INTEGER PRIMARY KEY, data_json TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS business_tasks (
                    conversation_id TEXT PRIMARY KEY REFERENCES conversations(conversation_id) ON DELETE CASCADE,
                    data_json TEXT NOT NULL
                );
            """)
            # 每个单进程 Web 实例只初始化一次；上次进程遗留的目标任务不得永久显示执行中。
            for identifier, raw in db.execute("SELECT conversation_id, data_json FROM business_tasks").fetchall():
                task = json.loads(raw)
                if task.get("phase") == "goal_running":
                    task.update(phase="goal_failed", question="上次目标处理已中断，可补充信息后继续；尚未自动重新执行。")
                    db.execute("UPDATE business_tasks SET data_json = ? WHERE conversation_id = ?",
                        (json.dumps(task, ensure_ascii=False), identifier))

    def profile(self):
        with self.conversations._connect() as db:
            row = db.execute("SELECT data_json FROM enterprise_profiles ORDER BY version DESC LIMIT 1").fetchone()
        return json.loads(row[0]) if row else None

    def save(self, request, documents=()):
        if not request.confirmed:
            raise ValueError("请检查企业资料并明确确认后保存。")
        with self.conversations._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            current = db.execute("SELECT COALESCE(MAX(version), 0) FROM enterprise_profiles").fetchone()[0]
            if request.expected_version != current:
                raise ValueError("企业档案已更新，请刷新后重新检查，避免覆盖他人的修改。")
            data = {"version": current + 1, "company_name": request.company_name,
                "company_profile": request.company_profile, "documents": list(documents),
                "confirmed_at": datetime.now(timezone.utc).isoformat()}
            data["content_hash"] = hashlib.sha256(json.dumps(data, ensure_ascii=False, sort_keys=True).encode()).hexdigest()
            db.execute("INSERT INTO enterprise_profiles VALUES (?, ?)", (data["version"], json.dumps(data, ensure_ascii=False)))
        return data

    def task(self, identifier):
        self.conversations.messages(identifier)
        with self.conversations._connect() as db:
            row = db.execute("SELECT data_json FROM business_tasks WHERE conversation_id = ?", (identifier,)).fetchone()
        return json.loads(row[0]) if row else None

    def start_goal(self, identifier, message):
        # 一个对话的一条运营任务始终沿用第一次绑定的企业快照。
        task = self.task(identifier) or {"profile": self.profile(), "skill_name": "operation-goal-setting"}
        if not task.get("profile"):
            task["profile"] = self.profile()
        return self.update_task(identifier, **{**task, "phase": "goal_running", "question": "", "last_input": message,
            "research_offer": None})

    def update_task(self, identifier, **values):
        self.conversations.messages(identifier)
        with self.conversations._connect() as db:
            db.execute("INSERT INTO business_tasks VALUES (?, ?) ON CONFLICT(conversation_id) DO UPDATE SET data_json = excluded.data_json",
                (identifier, json.dumps(values, ensure_ascii=False)))
        return values
