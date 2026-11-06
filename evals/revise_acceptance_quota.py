"""本次验收的一次性配额修订提案；默认只归档和准备，不改原审批或账本。"""

from __future__ import annotations

import argparse
from copy import deepcopy
from datetime import datetime, timezone
from decimal import Decimal
import hashlib
import json
from pathlib import Path
import shutil
import time

from evals.live_budget import BudgetApproval, LiveBudget

CASE_RUNS = [{"case_id": f"L{i:02}", "repetition": rep} for rep in (1, 2) for i in range(1, 5)]


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def canonical(data) -> str:
    return sha(json.dumps(data, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode())


def validate_original(approval, ledger):
    if approval.max_attempts != 100 or approval.max_seconds != 1800:
        raise ValueError("只允许本批100次/1800秒的一次性有限修订")
    if ledger["halted_reason"] != "sdk_attempt_limit" or ledger.get("quota_revisions"):
        raise ValueError("只允许首次sdk_attempt_limit修订，不能清除其他止损或再次扩限")
    if ledger["approval_sha256"] != sha(approval.model_dump_json().encode()):
        raise ValueError("原审批canonical SHA与账本绑定不一致")
    if ledger["approval"] != approval.model_dump(mode="json"):
        raise ValueError("原账本审批内容不一致")
    attempts = ledger["attempts"]
    if len(attempts) != 100 or [item["attempt_id"] for item in attempts] != list(range(1, 101)):
        raise ValueError("原始100次请求序列不完整")
    committed = Decimal(0)
    for item in attempts:
        usage = item.get("usage")
        if item["status"] != "observed" or item.get("actual_cny") is None or not isinstance(usage, dict):
            raise ValueError("存在未知或未解决预留，不能修订")
        if (not all(type(usage.get(key)) is int and usage[key] >= 0 for key in ("prompt_tokens", "completion_tokens", "total_tokens"))
                or usage["total_tokens"] < usage["prompt_tokens"] + usage["completion_tokens"]):
            raise ValueError("历史用量不完整或不一致，不能修订")
        expected = (approval.input_cny_per_million * usage["prompt_tokens"]
            + approval.output_cny_per_million * (usage["total_tokens"] - usage["prompt_tokens"])) / Decimal(1_000_000)
        if Decimal(item["actual_cny"]) != expected:
            raise ValueError("历史费用与原单价和用量不一致")
        committed += expected
    if committed + approval.worst_request_cny > approval.max_cny:
        raise ValueError("余额无法容纳原单次最坏预留")
    return committed


def prepare(evidence_root: Path, proposal_root: Path):
    approval_path = evidence_root / "proposed-approval.json"
    ledger_path = evidence_root / "actual/shared-ledger.json"
    approval_raw, ledger_raw = approval_path.read_bytes(), ledger_path.read_bytes()
    approval = BudgetApproval.model_validate_json(approval_raw)
    ledger = json.loads(ledger_raw)
    if approval.ledger_path.resolve() != ledger_path.resolve():
        raise ValueError("审批未绑定同一个真实账本路径")
    committed = validate_original(approval, ledger)
    proposal_root.mkdir(parents=True, exist_ok=False)
    archive = proposal_root / "first-batch-snapshot"
    archive.mkdir()
    snapshot_hashes = {}
    files = {"proposed-approval.json": approval_path, "shared-ledger.json": ledger_path}
    for report in (evidence_root / "actual/results").rglob("report.json"):
        for name in ("manifest.json", "events.jsonl", "report.json", "report.md"):
            file = report.with_name(name)
            if file.exists():
                files["reports/" + str(file.relative_to(evidence_root / "actual/results"))] = file
    if not any(name.startswith("reports/") for name in files):
        raise ValueError("首轮报告尚未归档，不能准备修订")
    for name, path in files.items():
        target = archive / name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(path, target)
        snapshot_hashes[name] = sha(target.read_bytes())
    revised = approval.model_copy(update={"max_attempts": 300, "max_seconds": 3600})
    revised = BudgetApproval.model_validate(revised.model_dump())
    revision = {"reason": "原4场景各两次的一轮集中修复后定点复测；不重跑6个已过基础用例，不扩场景或金额，第二次触发上限即停止。",
        "created_at_utc": datetime.now(timezone.utc).isoformat(), "case_runs": CASE_RUNS,
        "applies_from_attempt_id": 101, "old_limits": {"max_attempts": 100, "max_seconds": 1800},
        "new_limits": {"max_attempts": 300, "max_seconds": 3600},
        "old_approval_raw_sha256": sha(approval_raw), "old_approval_canonical_sha256": ledger["approval_sha256"],
        "new_approval_canonical_sha256": sha(revised.model_dump_json().encode()),
        "old_ledger_raw_sha256": sha(ledger_raw), "old_ledger_canonical_sha256": canonical(ledger),
        "historical_attempts_canonical_sha256": canonical(ledger["attempts"]), "started_at_epoch": ledger["started_at_epoch"],
        "historical_committed_cny": str(committed), "snapshot_sha256": snapshot_hashes}
    new_approval_raw = (json.dumps(revised.model_dump(mode="json"), ensure_ascii=False, indent=2) + "\n").encode()
    revision["new_approval_raw_sha256"] = sha(new_approval_raw)
    revised_ledger = deepcopy(ledger)
    revised_ledger.update(approval=revised.model_dump(mode="json"), approval_sha256=revision["new_approval_canonical_sha256"],
                          halted_reason=None, quota_revisions=[revision])
    (proposal_root / "new-approval.json").write_bytes(new_approval_raw)
    (proposal_root / "new-ledger.json").write_text(json.dumps(revised_ledger, ensure_ascii=False, indent=2) + "\n", "utf-8")
    manifest = {"status": "proposal_only_not_applied", "approval_path": str(approval_path.resolve()), "ledger_path": str(ledger_path.resolve()),
        "revision": revision, "proposed_ledger_raw_sha256": sha((proposal_root / "new-ledger.json").read_bytes()),
        "original_seconds_elapsed_at_prepare": time.time() - ledger["started_at_epoch"],
        "revised_seconds_remaining_at_prepare": 3600 - (time.time() - ledger["started_at_epoch"]),
        "remaining_cny": str(approval.max_cny - committed), "worst_request_cny": str(approval.worst_request_cny)}
    (proposal_root / "revision-proposal.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", "utf-8")
    return manifest


def apply(proposal_root: Path):
    manifest = json.loads((proposal_root / "revision-proposal.json").read_text("utf-8"))
    revision = manifest["revision"]
    approval_path, ledger_path = Path(manifest["approval_path"]), Path(manifest["ledger_path"])
    old_approval_raw, old_ledger_raw = approval_path.read_bytes(), ledger_path.read_bytes()
    if sha(old_approval_raw) != revision["old_approval_raw_sha256"] or sha(old_ledger_raw) != revision["old_ledger_raw_sha256"]:
        raise ValueError("原审批或账本已变化，不能应用该提案")
    original = BudgetApproval.model_validate_json(old_approval_raw)
    old_ledger = json.loads(old_ledger_raw)
    committed = validate_original(original, old_ledger)
    new_approval_raw, new_ledger_raw = (proposal_root / "new-approval.json").read_bytes(), (proposal_root / "new-ledger.json").read_bytes()
    revised = BudgetApproval.model_validate_json(new_approval_raw)
    new_ledger = json.loads(new_ledger_raw)
    if (sha(new_approval_raw) != revision["new_approval_raw_sha256"]
            or sha(new_ledger_raw) != manifest["proposed_ledger_raw_sha256"]):
        raise ValueError("提案文件已变化，不能应用")
    expected = original.model_copy(update={"max_attempts": 300, "max_seconds": 3600})
    if revised != expected or time.time() - old_ledger["started_at_epoch"] >= 3600:
        raise ValueError("只允许已复核的两个上限变化且不能超出原始时钟3600秒")
    expected_ledger = deepcopy(old_ledger)
    expected_ledger.update(approval=revised.model_dump(mode="json"), approval_sha256=sha(revised.model_dump_json().encode()),
                           halted_reason=None, quota_revisions=[revision])
    if new_ledger != expected_ledger:
        raise ValueError("提案更改了历史、费用、原始时钟或其他字段")
    for name, expected_hash in revision["snapshot_sha256"].items():
        if sha((proposal_root / "first-batch-snapshot" / name).read_bytes()) != expected_hash:
            raise ValueError("首轮字节快照已变化，不能应用")
    # 单一串行进程内更新；任何半途失败都会在预算绑定检查处停止，不发起模型调用。
    for path, raw in ((approval_path, new_approval_raw), (ledger_path, new_ledger_raw)):
        pending = path.with_suffix(path.suffix + ".pending")
        pending.write_bytes(raw)
        pending.replace(path)
    reopened = LiveBudget(approval_path, ledger_path, model=revised.model, base_url=revised.base_url)
    assert reopened.data["attempts"] == old_ledger["attempts"]
    assert reopened.data["started_at_epoch"] == old_ledger["started_at_epoch"] and reopened.committed_cny == committed
    assert reopened.data["halted_reason"] is None and reopened.snapshot()["unknown_attempts"] == 0
    receipt = {"status": "applied_no_model_called", "applied_at_utc": datetime.now(timezone.utc).isoformat(),
        "approval_raw_sha256": sha(approval_path.read_bytes()), "ledger_raw_sha256": sha(ledger_path.read_bytes()),
        "historical_attempts_preserved": True, "original_clock_preserved": True, "budget": reopened.snapshot()}
    (proposal_root / "apply-receipt.json").write_text(json.dumps(receipt, ensure_ascii=False, indent=2) + "\n", "utf-8")
    return receipt


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--evidence-root", type=Path)
    parser.add_argument("--proposal-root", type=Path, required=True)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    if args.apply:
        result = apply(args.proposal_root)
    else:
        if args.evidence_root is None:
            parser.error("准备提案需指定--evidence-root")
        result = prepare(args.evidence_root, args.proposal_root)
    print(json.dumps({"status": result["status"], "proposal_root": str(args.proposal_root)}, ensure_ascii=False))


if __name__ == "__main__":
    main()
