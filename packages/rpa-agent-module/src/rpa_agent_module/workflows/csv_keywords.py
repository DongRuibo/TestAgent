"""为 CSV Robot 流程提供状态、下载检测、归档和 Finder 联动关键字。"""

from __future__ import annotations

import csv
import json
import shutil
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any

from rpa_agent_module.database import JobRepository

CSV_COLUMNS = ("record_id", "created_at", "region", "status", "title", "amount")


class CsvWorkflowLibrary:
    """向 Robot Framework 暴露 CSV 工作流所需的受控本地关键字。"""

    ROBOT_LIBRARY_SCOPE = "SUITE"

    def __init__(self, input_file: str, result_file: str) -> None:
        """读取模块生成的任务输入，并绑定结果文件和任务数据库。"""

        self.input_file = Path(input_file).resolve()
        self.result_file = Path(result_file).resolve()
        self.payload = json.loads(self.input_file.read_text(encoding="utf-8"))
        self.job_id = str(self.payload["job_id"])
        self.repository = JobRepository(Path(self.payload["database_path"]))
        self.incoming_dir = Path(self.payload["incoming_dir"]).resolve()
        self.output_dir = Path(self.payload["output_dir"]).resolve()

    def get_target_url(self) -> str:
        """返回部署时配置的已适配页面地址，不接受单次任务覆盖。"""

        return str(self.payload["target_url"])

    def get_profile_directory(self) -> str:
        """创建并返回 CSV 工作流专用 Chrome Profile 根目录。"""

        profile_dir = Path(self.payload["profile_dir"]).resolve()
        profile_dir.mkdir(parents=True, exist_ok=True)
        return str(profile_dir)

    def get_query_value(self, name: str) -> str:
        """按白名单字段读取已经由模块校验的查询参数。"""

        if name not in {"start_date", "end_date", "region", "status", "keyword"}:
            raise ValueError("查询字段不合法")
        return str(self.payload["query"][name])

    def prepare_download_directory(self) -> str:
        """准备当前任务独立下载目录，并拒绝复用含旧文件的目录。"""

        self.incoming_dir.mkdir(parents=True, exist_ok=True)
        if any(self.incoming_dir.iterdir()):
            raise RuntimeError("任务下载目录不是空目录")
        return str(self.incoming_dir)

    def update_job_step(self, step: str) -> None:
        """把 Robot 当前关键步骤写入 SQLite，供外部 Agent 轮询。"""

        allowed_steps = {
            "opening_target_page",
            "filling_form",
            "querying_data",
            "downloading_csv",
            "moving_file",
            "opening_finder",
            "verifying_result",
        }
        if step not in allowed_steps:
            raise ValueError("任务步骤不合法")
        self.repository.update_step(self.job_id, step)

    def wait_for_download_completion(self, timeout_seconds: int = 60) -> str:
        """等待唯一 CSV 稳定写入，存在临时文件或多个结果时拒绝成功。"""

        deadline = time.monotonic() + int(timeout_seconds)
        previous_size: int | None = None
        stable_checks = 0
        while time.monotonic() < deadline:
            temporary_files = list(self.incoming_dir.glob("*.crdownload"))
            csv_files = list(self.incoming_dir.glob("mock_records_*.csv"))
            if len(csv_files) > 1:
                raise RuntimeError("下载目录出现多个 CSV 文件")
            if not temporary_files and len(csv_files) == 1:
                current_size = csv_files[0].stat().st_size
                if current_size > 0 and current_size == previous_size:
                    stable_checks += 1
                else:
                    stable_checks = 0
                previous_size = current_size
                # 连续两次大小不变才移动，避免浏览器仍在写入。
                if stable_checks >= 2:
                    return str(csv_files[0].resolve())
            time.sleep(0.5)
        raise TimeoutError("等待 CSV 下载完成超时")

    def archive_downloaded_csv(self, downloaded_path: str) -> str:
        """校验 CSV 表头和数据后，按防覆盖规则移动到最终目录。"""

        source = Path(downloaded_path).resolve()
        if source.parent != self.incoming_dir or source.suffix.lower() != ".csv":
            raise ValueError("下载文件不在当前任务目录")
        with source.open("r", encoding="utf-8-sig", newline="") as file:
            reader = csv.DictReader(file)
            if tuple(reader.fieldnames or ()) != CSV_COLUMNS:
                raise ValueError("CSV 表头不符合预期")
            rows = list(reader)
        if not rows:
            raise ValueError("CSV 没有数据行")

        self.output_dir.mkdir(parents=True, exist_ok=True)
        query = self.payload["query"]
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        base_name = f"业务数据_{query['start_date']}_{query['end_date']}_{timestamp}"
        target = self.output_dir / f"{base_name}.csv"
        suffix = 2
        while target.exists():
            target = self.output_dir / f"{base_name}_{suffix}.csv"
            suffix += 1
        shutil.move(str(source), str(target))
        if not target.is_file() or target.stat().st_size <= 0:
            raise RuntimeError("CSV 移动后验证失败")
        return str(target.resolve())

    def mark_no_data(self) -> None:
        """写入无数据成功结果，不创建或归档空 CSV。"""

        self._write_result(
            {
                "result_type": "no_data",
                "has_data": False,
                "query": self.payload["query"],
                "finder_opened": False,
            }
        )

    def open_finder_and_write_result(self, final_path: str) -> bool:
        """尝试用 Finder 定位文件，展示失败时仍保留归档成功结果。"""

        path = Path(final_path).resolve()
        finder_opened = False
        if bool(self.payload.get("open_finder", True)) and sys.platform == "darwin":
            try:
                # 参数数组只包含模块验证后的绝对路径，不经过 Shell。
                subprocess.run(
                    ["open", "-R", str(path)],
                    check=True,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    timeout=10,
                )
                finder_opened = True
            except (OSError, subprocess.SubprocessError):
                finder_opened = False
        self._write_result(
            {
                "result_type": "file_archived",
                "has_data": True,
                "query": self.payload["query"],
                "original_filename": "mock_records_*.csv",
                "final_filename": path.name,
                "final_path": str(path),
                "file_size": path.stat().st_size,
                "finder_opened": finder_opened,
            }
        )
        return finder_opened

    def _write_result(self, result: dict[str, Any]) -> None:
        """原子替换任务结果文件，避免运行器读取到半写入 JSON。"""

        self.result_file.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.result_file.with_suffix(".tmp")
        temporary.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        temporary.replace(self.result_file)
