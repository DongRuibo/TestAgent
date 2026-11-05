"""让 Agent 读取绑定账号的官方后台笔记数据，不执行发布或投流。"""

from __future__ import annotations

import json
import shutil
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from rpa_agent_module import (
    RpaExecutionModule, RpaModuleConfig, RpaModuleError,
    XiaohongshuMetricsAccount, XiaohongshuMetricsRequest,
)
from rpa_agent_module.xiaohongshu_metrics import parse_metrics_export

from testagent_dev_agent.paths import REPOSITORY_ROOT

RPA_DATA_DIR = REPOSITORY_ROOT / ".local" / "rpa-agent-module"
ACCOUNT_FILE = RPA_DATA_DIR / "xiaohongshu-metrics-account.json"
OUTPUT_DIR = REPOSITORY_ROOT / "workspace" / "xiaohongshu_metrics"
RPA_XHS_METRICS_TOOL_NAME = "collect_xiaohongshu_metrics"
RPA_XHS_METRICS_TOOL = {
    "type": "function",
    "function": {
        "name": RPA_XHS_METRICS_TOOL_NAME,
        "description": "读取已绑定小红书账号的官方后台笔记数据。日期是笔记首发日期，默认最近30天，单次最多31天/50篇。需要专用Chrome登录；失败后等待人工处理，不自动重试。返回JSON路径，缺失咨询/线索/花费不能补零。只读，不投流。",
        "parameters": {
            "type": "object",
            "properties": {
                "start_date": {"type": "string", "description": "首发起始日期 YYYY-MM-DD，与结束日期同时提供"},
                "end_date": {"type": "string", "description": "首发结束日期 YYYY-MM-DD，与起始日期同时提供"},
                "max_posts": {"type": "integer", "minimum": 1, "maximum": 50, "default": 20},
            },
            "additionalProperties": False,
        },
    },
}


def _error(code: str, message: str, *, job_id: str | None = None, status: str = "failed") -> dict[str, Any]:
    return {"success": False, "error_type": code, "message": message,
            "job_id": job_id, "status": status, "retryable": False,
            "needs_user_action": status == "needs_user_action"}


def collect_xiaohongshu_metrics(
    start_date: str | None = None,
    end_date: str | None = None,
    max_posts: int = 20,
) -> dict[str, Any]:
    """同步等待一次有界采集；原始导出和规范化 JSON 仅保存在本地。"""

    today = datetime.now(ZoneInfo("Asia/Shanghai")).date()
    if (start_date is None) != (end_date is None):
        return _error("RPA_INVALID_INPUT", "请同时提供首发起始和结束日期，或同时省略。")
    try:
        query = XiaohongshuMetricsRequest(
            start_date=start_date if start_date is not None else today - timedelta(days=29),
            end_date=end_date if end_date is not None else today,
            max_posts=max_posts,
        )
    except ValueError:
        return _error("RPA_INVALID_INPUT", "首发日期须在最近180天内，范围1至31天，笔记上限1至50。")
    try:
        account = XiaohongshuMetricsAccount.model_validate_json(ACCOUNT_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return _error("RPA_ACCOUNT_NOT_BOUND", "请先在本地采集配置中绑定公开主页ID、小红书号和昵称。", status="needs_user_action")

    job_id = None
    try:
        with RpaExecutionModule(RpaModuleConfig(
            data_dir=RPA_DATA_DIR, xiaohongshu_enabled=True, xiaohongshu_metrics_account=account,
        )) as rpa:
            health = rpa.health()
            if not (health.platform_supported and health.chrome_available and health.database_ready):
                return _error("RPA_ENVIRONMENT_UNAVAILABLE", "需要本机macOS、Google Chrome和可用的RPA任务库。")
            job = rpa.submit("xiaohongshu_metrics", query.model_dump(mode="json"))
            job_id = job.id
            try:
                completed = rpa.wait(job.id, timeout=180)
            except TimeoutError:
                current = rpa.get_job(job.id)
                if not current.is_terminal:
                    rpa.stop(job.id)
                return _error("RPA_TIMEOUT", "采集等待超时，任务已停止；请检查专用Chrome后手动重试。", job_id=job.id)
        if completed.status != "succeeded":
            return _error(
                completed.error_code or "RPA_COLLECTION_FAILED",
                completed.error_message or "采集未完成，请检查专用Chrome。",
                job_id=completed.id, status=completed.status,
            )
        result = completed.result or {}
        if result.get("result_type") != "xiaohongshu_metrics":
            raise ValueError("无效结果类型")
        data = result["data"]
        if data["account"] != account.model_dump() or data["query"] != query.model_dump(mode="json"):
            raise ValueError("结果与任务不匹配")
        incoming = (RPA_DATA_DIR / "jobs" / job.id / "incoming").resolve()
        if not incoming.is_relative_to((RPA_DATA_DIR / "jobs").resolve()):
            raise ValueError("任务路径越界")
        raw = Path(data["raw_export_path"])
        if raw.is_symlink() or raw.resolve().parent != incoming:
            raise ValueError("结果文件不在任务下载目录")
        # 在向 Agent 交付前重新校验原文件，避免成功状态附带空数据或错误文件。
        verified = parse_metrics_export(raw, query=query, account=account, source_total=data["source_total"])
        target = OUTPUT_DIR / job.id
        target.mkdir(parents=True, exist_ok=False)
        export_path = target / "official-export.xlsx"
        shutil.copy2(raw, export_path)
        verified["raw_export_path"] = str(export_path)
        verified["collected_at"] = data["collected_at"]
        json_path = target / "metrics.json"
        json_path.write_text(json.dumps(verified, ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8")
        return {
            "success": True, "workflow": "xiaohongshu_metrics", "job_id": job.id,
            "status": "succeeded", "account": verified["account"],
            "query": verified["query"], "collected_count": verified["collected_count"],
            "source_total": verified["source_total"], "truncated": verified["truncated"],
            "summary": verified["summary"], "notes": verified["notes"],
            "missing_fields": verified["missing_fields"],
            "date_filter_kind": verified["date_filter_kind"],
            "metrics_period": verified["metrics_period"], "traffic_scope": verified["traffic_scope"],
            "path": str(json_path.relative_to(REPOSITORY_ROOT)),
            "raw_export_path": str(export_path.relative_to(REPOSITORY_ROOT)),
        }
    except PermissionError:
        return _error("RPA_HOST_PERMISSION_REQUIRED", "本机权限阻止了浏览器或回环端口访问，请在允许本机RPA的环境中运行。", job_id=job_id)
    except RpaModuleError:
        return _error("RPA_UNAVAILABLE", "RPA任务或专用Chrome暂不可用，请检查是否有任务运行或浏览器占用。", job_id=job_id)
    except Exception:
        return _error("RPA_COLLECTION_FAILED", "采集执行或结果校验失败，未返回可用数据。", job_id=job_id)
