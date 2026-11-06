"""本次串行验收共用的持久预算；不处理并发或生产计费。"""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
import hashlib
import json
from pathlib import Path
import time
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class BudgetStopped(RuntimeError):
    """授权、费用或计量不明确时停止后续网络请求。"""


class BudgetApproval(BaseModel):
    model_config = ConfigDict(extra="forbid")
    schema_version: Literal[1] = 1
    provider: str = Field(min_length=1)
    model: str = Field(min_length=1)
    base_url: str = Field(min_length=1)
    price_source: str = Field(pattern=r"^https://")
    input_bound_source: str = Field(pattern=r"^https://")
    verification_evidence_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    ledger_path: Path
    verified_at: str = Field(min_length=1)
    input_bound_basis: str = Field(min_length=1)
    input_cny_per_million: Decimal = Field(gt=0)
    output_cny_per_million: Decimal = Field(gt=0)
    max_input_tokens: int = Field(gt=0)
    max_request_bytes: int = Field(gt=0)
    max_output_tokens: int = Field(gt=0)
    max_billable_output_tokens: int = Field(gt=0)
    max_attempts: int = Field(gt=0)
    max_seconds: int = Field(gt=0)
    request_timeout_seconds: int = Field(gt=0, le=120)
    max_cny: Decimal = Field(gt=0, le=20)

    @model_validator(mode="after")
    def request_fits(self):
        if not self.ledger_path.is_absolute():
            raise ValueError("共享账本必须是复核时固定的绝对路径")
        if self.max_output_tokens > self.max_billable_output_tokens:
            raise ValueError("请求输出上限不能超过最坏计费输出预留")
        if self.worst_request_cny > self.max_cny:
            raise ValueError("单次最坏预留超过整批授权金额，不能请求")
        return self

    @property
    def worst_request_cny(self) -> Decimal:
        return (self.input_cny_per_million * self.max_input_tokens
                + self.output_cny_per_million * self.max_billable_output_tokens) / Decimal(1_000_000)


class LiveBudget:
    """所有 runner 和复测传同一账本路径；进程重启不会清空计数和未知预留。"""

    def __init__(self, approval_path: Path, ledger_path: Path, *, model: str, base_url: str | None):
        self.approval = BudgetApproval.model_validate_json(approval_path.read_text("utf-8"))
        self.path = ledger_path
        canonical = self.approval.model_dump_json()
        self.approval_sha256 = hashlib.sha256(canonical.encode()).hexdigest()
        if model != self.approval.model or (base_url or "").rstrip("/") != self.approval.base_url.rstrip("/"):
            raise BudgetStopped("模型或服务地址与已复核的预算不一致")
        if ledger_path.resolve() != self.approval.ledger_path.resolve():
            raise BudgetStopped("必须复用审批绑定的单一账本，不能另建账本重置额度")
        if ledger_path.exists():
            self.data = json.loads(ledger_path.read_text("utf-8"))
            if self.data["approval_sha256"] != self.approval_sha256:
                raise BudgetStopped("预算账本已绑定其他审批参数，不能重置或替换")
            if any(item["status"] == "reserved" for item in self.data["attempts"]):
                self.data["halted_reason"] = "unresolved_reserved_attempt"
                self._save()
        else:
            self.data = {"schema_version": 1, "approval_sha256": self.approval_sha256,
                         "approval": self.approval.model_dump(mode="json"), "started_at_epoch": time.time(),
                         "halted_reason": None, "attempts": []}
            self._save()

    def _save(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        pending = self.path.with_suffix(self.path.suffix + ".pending")
        pending.write_text(json.dumps(self.data, ensure_ascii=False, indent=2) + "\n", "utf-8")
        pending.replace(self.path)

    @property
    def committed_cny(self) -> Decimal:
        return sum((Decimal(item["actual_cny"] if item.get("actual_cny") is not None else item["reserved_cny"])
                    for item in self.data["attempts"]), Decimal(0))

    def check(self):
        if self.data["halted_reason"]:
            raise BudgetStopped(self.data["halted_reason"])
        reason = None
        if len(self.data["attempts"]) >= self.approval.max_attempts:
            reason = "sdk_attempt_limit"
        elif time.time() - self.data["started_at_epoch"] >= self.approval.max_seconds:
            reason = "batch_time_limit"
        elif self.committed_cny + self.approval.worst_request_cny > self.approval.max_cny:
            reason = "insufficient_fee_reserve"
        if reason:
            self.data["halted_reason"] = reason
            self._save()
            raise BudgetStopped(reason)

    def reserve(self, request: dict[str, Any], label: dict[str, Any]) -> dict[str, Any]:
        self.check()
        if request["model"] != self.approval.model:
            raise BudgetStopped("请求模型与审批不一致")
        request_bytes = len(json.dumps(request, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))
        if request_bytes > self.approval.max_request_bytes:
            self.data["halted_reason"] = "request_bytes_limit"
            self._save()
            raise BudgetStopped("request_bytes_limit")
        item = {"attempt_id": len(self.data["attempts"]) + 1, "status": "reserved", **label,
                "started_at": datetime.now(timezone.utc).isoformat(), "request_bytes": request_bytes,
                "reserved_cny": str(self.approval.worst_request_cny), "actual_cny": None, "usage": None}
        self.data["attempts"].append(item)
        # 在真正进入SDK之前落盘，失联、超时和进程中断均保留最坏预留。
        self._save()
        return item

    def settle(self, item: dict[str, Any], usage: dict[str, Any] | None, error_type: str | None = None):
        item["error_type"] = error_type
        item["usage"] = usage
        valid = usage is not None and all(type(usage.get(key)) is int and usage[key] >= 0
            for key in ("prompt_tokens", "completion_tokens", "total_tokens"))
        valid = valid and usage["total_tokens"] >= usage["prompt_tokens"] + usage["completion_tokens"]
        if not valid:
            item["status"] = "unknown"
            self.data["halted_reason"] = "unknown_usage"
        else:
            item["status"] = "observed"
            # 若total还包括独立列出的思考量，也全部按输出最高价计入。
            output_tokens = usage["total_tokens"] - usage["prompt_tokens"]
            item["billable_output_tokens"] = output_tokens
            actual = (Decimal(usage["prompt_tokens"]) * self.approval.input_cny_per_million
                + Decimal(output_tokens) * self.approval.output_cny_per_million) / Decimal(1_000_000)
            item["actual_cny"] = str(actual)
            details = usage.get("completion_tokens_details") or {}
            if details.get("reasoning_tokens", 0):
                self.data["halted_reason"] = "thinking_not_disabled"
            if (usage["prompt_tokens"] > self.approval.max_input_tokens
                    or output_tokens > self.approval.max_billable_output_tokens):
                self.data["halted_reason"] = "provider_usage_exceeds_approved_bound"
        self._save()

    def snapshot(self):
        return {"ledger_path": str(self.path.resolve()), "approval_sha256": self.approval_sha256,
                "sdk_attempts": len(self.data["attempts"]), "committed_upper_cny": str(self.committed_cny),
                "observed_cost_cny": str(sum((Decimal(x["actual_cny"]) for x in self.data["attempts"] if x.get("actual_cny") is not None), Decimal(0))),
                "unknown_attempts": sum(x["status"] != "observed" for x in self.data["attempts"]),
                "halted_reason": self.data["halted_reason"], "approval": self.approval.model_dump(mode="json")}
