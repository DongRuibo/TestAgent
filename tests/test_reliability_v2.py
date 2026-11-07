"""新语义必须同时保留历史证据和当前读回，不能靠改单个状态字段过关。"""

from copy import deepcopy
import json
from pathlib import Path

import pytest

from evals import reliability_runner as v1, reliability_v2_runner as v2


@pytest.fixture(scope="module")
def replay_observation(tmp_path_factory):
    case = next(c for c in v1.load_dataset().cases if c.case_id == "RT-E10")
    row = v1.run_probe(case, tmp_path_factory.mktemp("replay-v2") / "probe")
    row.update(case_id=case.case_id, repetition=1)
    return row


def test_v2_changes_only_the_confirmed_replay_contract():
    old = v1.load_dataset().model_dump()
    new = v2.load_dataset().model_dump()
    assert len(new["cases"]) == 50
    for a, b in zip(old["cases"], new["cases"]):
        if a["case_id"] != "RT-E10":
            assert a == b
        else:
            expected = deepcopy(a)
            expected["expected_final_state"]["receipt_matches_database"] = False
            expected["expected_tool_behavior"]["replay_contract"] = b["expected_tool_behavior"]["replay_contract"]
            assert expected == b


def test_same_observation_retains_v1_failure_and_passes_v2(replay_observation):
    old = next(c for c in v1.load_dataset().cases if c.case_id == "RT-E10")
    new = next(c for c in v2.load_dataset().cases if c.case_id == "RT-E10")
    assert not v1.evaluate(old, replay_observation)[0]
    assert v2.evaluate(new, replay_observation)[0]


@pytest.mark.parametrize("corruption", ["no_metadata", "wrong_target", "wrong_original", "rewritten_history", "no_effect", "wrong_job"])
def test_v2_rejects_false_replay_evidence(replay_observation, corruption):
    row = deepcopy(replay_observation)
    response = row["replayed_response"]
    if corruption == "no_metadata": response.pop("replayed")
    elif corruption == "wrong_target": response["current_data_patch"]["artifact_id"] = "another-artifact"
    elif corruption == "wrong_original": response["original_execution_status"] = "draft_saved"
    elif corruption == "rewritten_history": response["status"] = "draft_saved"
    elif corruption == "no_effect": row["evidence"]["effects"] = []
    elif corruption == "wrong_job": row["evidence"]["executions"][0]["job_id"] = "other-job"
    case = next(c for c in v2.load_dataset().cases if c.case_id == "RT-E10")
    assert not v2.evaluate(case, row)[0]
