"""恢复分类必须保留未知，不能把缺失执行记录当作安全的零。"""

from copy import deepcopy

from evals import reliability_runner as v1
from evals.recovery_analysis import analyze


def test_missing_recovery_observations_are_unknown():
    result = analyze({"cases": [], "summary": {"repeated_runs": {"k": 1},
        "metrics": {"duplicate_side_effect_rate": v1.fraction(0, 10, 10)}}})
    assert result["metrics"]["automatic_business_recovery_rate"] == v1.fraction(0, 15, 15)
    assert result["metrics"]["task_loss_rate"]["unknown"] == 11
    assert result["metrics"]["unsafe_continuation_rate"] == v1.fraction(0, 2, 2)
    assert result["metrics"]["duplicate_side_effect_rate"]["value"] is None
