"""评测报告 Receipt 审计器测试。"""

from __future__ import annotations

import hashlib
import json

from scripts.audit_evaluation_report import audit


def test_audit_validates_receipt_hash_and_cleanup(tmp_path) -> None:
    receipt_dir = tmp_path / "attempt"
    receipt_dir.mkdir()
    receipt_payload = {
        "sandbox_id": "sbi-1",
        "status": "passed",
        "cleanup": {"sandbox_id": "sbi-1", "destroyed": True},
    }
    receipt_path = receipt_dir / "execution-receipt.json"
    receipt_path.write_text(json.dumps(receipt_payload), encoding="utf-8")
    digest = hashlib.sha256(receipt_path.read_bytes()).hexdigest()
    report_path = tmp_path / "evaluation-report.json"
    report_path.write_text(
        json.dumps(
            {
                "results": [
                    {
                        "suite": "code",
                        "case_id": "one",
                        "cleanup_checked": True,
                        "sandbox_destroyed": True,
                        "evidence": {
                            "receipt": {
                                "relative_path": "attempt/execution-receipt.json",
                                "sha256": digest,
                            }
                        },
                    }
                ]
            }
        ),
        encoding="utf-8",
    )

    result = audit(report_path)

    assert result["passed"] is True
    assert result["receipts_checked"] == 1
