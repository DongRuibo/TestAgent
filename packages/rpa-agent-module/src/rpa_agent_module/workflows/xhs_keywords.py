"""为小红书草稿 Robot 流程提供受控输入、状态和结果关键字。"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from rpa_agent_module.database import JobRepository


class XhsWorkflowLibrary:
    """向 Robot Framework 暴露小红书网页自动化所需的安全数据关键字。"""

    ROBOT_LIBRARY_SCOPE = "SUITE"

    def __init__(self, input_file: str, result_file: str) -> None:
        """读取模块生成的任务输入，并绑定结果文件和任务数据库。"""

        self.input_file = Path(input_file).resolve()
        self.result_file = Path(result_file).resolve()
        self.payload = json.loads(self.input_file.read_text(encoding="utf-8"))
        self.job_id = str(self.payload["job_id"])
        self.repository = JobRepository(Path(self.payload["database_path"]))

    def get_target_url(self) -> str:
        """返回固定的小红书官方创作地址，不接受 Agent 单次覆盖。"""

        return str(self.payload["target_url"])

    def get_profile_directory(self) -> str:
        """创建并返回小红书专用 Chrome Profile，隔离日常浏览器。"""

        profile_dir = Path(self.payload["profile_dir"]).resolve()
        profile_dir.mkdir(parents=True, exist_ok=True)
        return str(profile_dir)

    def get_debug_port(self) -> int:
        """返回模块托管 Chrome 的本地调试端口。"""

        port = int(self.payload["debug_port"])
        if not 1024 <= port <= 65535:
            raise ValueError("小红书专用 Chrome 调试端口不合法")
        return port

    def get_debug_target_id(self) -> str:
        """返回模块专属自动化标签页 ID，避免接管用户正在查看的页面。"""

        target_id = str(self.payload["debug_target_id"]).strip()
        if not target_id or len(target_id) > 128 or not target_id.replace("-", "").isalnum():
            raise ValueError("小红书专用自动化标签页 ID 不合法")
        return target_id

    def get_selector(self, name: str) -> str:
        """按白名单返回现场验证选择器，允许三个非必要项留空。"""

        allowed = {
            "login",
            "file_input",
            "title",
            "body",
            "tag",
            "upload_done",
            "draft_save",
            "draft_saved",
            "draft_box",
            "draft_image_tab",
            "draft_title",
            "challenge",
        }
        if name not in allowed:
            raise ValueError("小红书选择器名称不合法")
        selector = str(self.payload["selectors"].get(name, "")).strip()
        if not selector and name not in {"challenge", "tag", "draft_save"}:
            raise ValueError(f"缺少小红书选择器：{name}")
        return selector

    def get_post_title(self) -> str:
        """返回 Agent 和用户已审核的标题，不调用任何大模型。"""

        return str(self.payload["post"]["title"])

    def get_post_body(self) -> str:
        """返回审核正文，并将标签按正文话题格式追加。"""

        body = str(self.payload["post"]["body"]).strip()
        tags = self.get_post_tags()
        return f"{body}\n\n{tags}" if tags else body

    def get_post_tags(self) -> str:
        """把审核标签格式化为安全文本，不拼接网页脚本。"""

        tags = [str(tag).strip() for tag in self.payload["post"].get("tags", []) if str(tag).strip()]
        return " ".join(tag if tag.startswith("#") else f"#{tag}" for tag in tags)

    def get_image_paths(self) -> list[str]:
        """返回模块已校验过且执行时仍存在的本地图片路径。"""

        paths = [str(Path(path).resolve()) for path in self.payload.get("image_paths", [])]
        if not paths or any(not Path(path).is_file() for path in paths):
            raise ValueError("小红书任务图片不存在")
        return paths

    def get_image_upload_value(self) -> str:
        """按 Selenium 多文件上传协议返回换行分隔的绝对路径。"""

        return "\n".join(self.get_image_paths())

    def update_job_step(self, step: str) -> None:
        """把 Robot 当前关键步骤写入 SQLite，供外部 Agent 轮询。"""

        allowed_steps = {
            "opening_target_page",
            "checking_login",
            "opening_composer",
            "uploading_images",
            "filling_content",
            "saving_draft",
            "verifying_draft",
            "opening_draft_box",
            "verifying_draft_box",
        }
        if step not in allowed_steps:
            raise ValueError("小红书任务步骤不合法")
        self.repository.update_step(self.job_id, step)

    def mark_needs_user_action(self, message: str) -> None:
        """写入人工介入结果，未登录或风控时停止后续动作。"""

        self._write_result(
            {
                "status": "needs_user_action",
                "message": message,
                "url": "人工介入后请重新创建任务",
            }
        )

    def mark_draft_saved(self, current_url: str) -> None:
        """仅在草稿箱验证通过后写入成功结果，绝不代表最终发布。"""

        self._write_result(
            {
                "status": "succeeded",
                "result_type": "xiaohongshu_draft",
                "message": "小红书草稿已保存并在草稿箱中确认，专用 Chrome 可继续复用",
                "url": current_url,
                "image_count": len(self.get_image_paths()),
            }
        )

    def _write_result(self, result: dict[str, Any]) -> None:
        """原子替换任务结果 JSON，避免运行器读取半写入内容。"""

        self.result_file.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.result_file.with_suffix(".tmp")
        temporary.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        temporary.replace(self.result_file)
