"""完成协议：模型只能提出结束，任务后置条件由宿主绑定并核验。"""

from dataclasses import dataclass
import json
from typing import Callable, Literal

from pydantic import BaseModel, Field, model_validator


class CompletionDecision(BaseModel):
    status: Literal["completed", "needs_review"]
    reason: str
    evidence: dict = Field(default_factory=dict)

    @model_validator(mode="after")
    def completed_requires_evidence(self):
        if self.status == "completed" and not self.evidence:
            raise ValueError("完成结论必须包含宿主读回的证据引用。")
        return self


@dataclass(frozen=True)
class CompletionContract:
    task_type: str
    verify: Callable[[], CompletionDecision]


def complete_answer(model_answer: str, contract: CompletionContract | None):
    """只解析显式结构化声明，不通过自然语言关键词猜测业务完成。"""
    try:
        proposed = json.loads(model_answer)
    except (ValueError, TypeError):
        proposed = None
    has_claim = isinstance(proposed, dict) and "completed" in proposed
    if contract is None and not has_claim:
        return model_answer, None
    if contract is None:
        decision = CompletionDecision(status="needs_review", reason="HOST_CONTRACT_REQUIRED")
    else:
        try:
            decision = CompletionDecision.model_validate(contract.verify())
        except Exception:
            # 核验器不可用时停止升级；不重试工具，也不回显可能带私密数据的异常。
            decision = CompletionDecision(status="needs_review", reason="HOST_VERIFICATION_FAILED")
    receipt = {**decision.model_dump(), "task_type": contract.task_type if contract else "unbound",
        "model_proposed_completed": proposed.get("completed") if has_claim and type(proposed.get("completed")) is bool else None,
        "authority": "host"}
    answer = {"completed": decision.status == "completed"}
    if contract is not None:
        answer.update(receipt)
    return json.dumps(answer, ensure_ascii=False), receipt
