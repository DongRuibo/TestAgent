"""为小红书草稿 Robot 流程提供受控输入、状态和结果关键字。"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit

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

    def open_exact_draft(self, selector: str) -> None:
        """精确选择唯一同标题草稿；存在歧义时停止，防止误认旧草稿。"""

        from robot.libraries.BuiltIn import BuiltIn
        browser = BuiltIn().get_library_instance("RPA.Browser.Selenium")
        matches = [element for element in browser.get_webelements(selector)
                   if element.is_displayed() and element.text.strip() == self.get_post_title()]
        if len(matches) != 1:
            raise AssertionError("草稿箱未找到唯一匹配标题，请人工核验，不能直接重试保存")
        browser.click_element(matches[0])

    def mark_draft_saved(self, current_url: str, actual_title: str, actual_body: str, actual_image_count: int) -> bool:
        """只根据重新打开后的页面读值核验，保存预期、实际值及差异。"""

        title = str(actual_title).strip()
        body = str(actual_body).replace("\r\n", "\n").strip()
        tags = [tag if tag.startswith("#") else "#" + tag for tag in self.payload["post"].get("tags", [])]
        actual_tags = [tag for tag in tags if re.search(re.escape(tag) + r"(?=\s|$)", body)]
        expected = {"title": self.get_post_title(), "body": self.get_post_body(),
                    "image_count": len(self.payload["image_paths"]), "tags": tags}
        actual = {"title": title, "body": body, "image_count": int(actual_image_count), "tags": actual_tags}
        fields = {name: {"expected": value, "actual": actual[name], "matched": value == actual[name]}
                  for name, value in expected.items()}
        matched = all(field["matched"] for field in fields.values())
        parts = urlsplit(current_url)
        self._write_result({
            "status": "succeeded" if matched else "needs_review",
            "result_type": "xiaohongshu_draft",
            "message": "草稿重新打开后标题、正文、图片数量与标签均已核验" if matched else "草稿读回内容不一致，请人工核验；不会自动再次保存",
            "url": urlunsplit((parts.scheme, parts.netloc, parts.path, "", "")),
            "image_count": actual["image_count"],
            "verification": {"draft_reopened": True, "fields": fields},
            "tag_mode": "body_hashtags",
            "evidence_path": str(self.result_file.parent / "draft-body.png") if (self.result_file.parent / "draft-body.png").is_file() else None,
            "final_publish_performed": False,
        })
        return matched

    def capture_redacted_evidence(self) -> str:
        """仅截取正文编辑区域，不保存账号、登录二维码或页面认证信息。"""

        from robot.libraries.BuiltIn import BuiltIn
        browser = BuiltIn().get_library_instance("RPA.Browser.Selenium")
        path = self.result_file.parent / "draft-body.png"
        if browser.get_element_count(self.get_selector("body")):
            browser.capture_element_screenshot(self.get_selector("body"), str(path))
            return str(path)
        return ""

    def _write_result(self, result: dict[str, Any]) -> None:
        """原子替换任务结果 JSON，避免运行器读取半写入内容。"""

        self.result_file.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.result_file.with_suffix(".tmp")
        temporary.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        temporary.replace(self.result_file)
