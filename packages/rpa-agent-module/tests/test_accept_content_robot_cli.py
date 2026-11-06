"""只验证录屏输出目录的入口语义，不启动浏览器、驱动或编码器。"""

import json
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace

import pytest

from scripts import accept_content_robot as launcher


@pytest.fixture
def metadata_only(monkeypatch, tmp_path):
    """仅替换SDK安装位置查询；不把此路径测试称为真实浏览器或编码验证。"""

    calls = []
    def probe(command, **kwargs):
        calls.append(command)
        if command[0] == sys.executable:
            return SimpleNamespace(stdout=str(tmp_path / "playwright-driver"))
        return SimpleNamespace(stdout=json.dumps({"revision": "1011", "executable": str(tmp_path / "missing-ffmpeg"),
            "registry_directory": str(tmp_path / "cache"), "download_urls": []}))
    monkeypatch.setattr(launcher, "subprocess", SimpleNamespace(run=probe))
    return calls


def test_video_parent_escape_rejected_without_creating_outside(tmp_path, monkeypatch, metadata_only):
    evidence, outside = tmp_path / "evidence", tmp_path / "outside"
    monkeypatch.setattr(sys, "argv", ["accept_content_robot", "--python", sys.executable, "--evidence", str(evidence),
        "--record-video", "--video-dir", str(evidence / ".." / "outside")])
    with pytest.raises(SystemExit) as error:
        launcher.main()
    assert error.value.code == 2 and not outside.exists() and metadata_only == []


def test_valid_video_child_normalized_and_missing_encoder_stops(tmp_path, monkeypatch, metadata_only):
    evidence = tmp_path / "evidence"
    monkeypatch.setattr(sys, "argv", ["accept_content_robot", "--python", sys.executable, "--evidence", str(evidence),
        "--record-video", "--video-dir", str(evidence / "nested" / ".." / "videos")])
    assert launcher.main() == 2
    result = json.loads((evidence / "video-preflight.json").read_text("utf-8"))
    assert result["record_dir"] == str(evidence.resolve() / "videos")
    assert result["executable_ready"] is False and result["browser_started"] is False
    assert not (evidence / "videos").exists() and len(metadata_only) == 2


def test_tmp_alias_and_canonical_video_directory_are_same_root(monkeypatch, metadata_only):
    with tempfile.TemporaryDirectory(prefix="testagent-cli-path-", dir="/tmp") as directory:
        evidence = Path(directory) / "evidence"
        canonical_video = evidence.resolve() / "videos"
        monkeypatch.setattr(sys, "argv", ["accept_content_robot", "--python", sys.executable, "--evidence", str(evidence),
            "--record-video", "--video-dir", str(canonical_video)])
        assert launcher.main() == 2
        result = json.loads((evidence / "video-preflight.json").read_text("utf-8"))
        assert result["record_dir"] == str(canonical_video) and len(metadata_only) == 2


def test_recording_remains_off_by_default(tmp_path, monkeypatch, metadata_only):
    evidence = tmp_path / "evidence"
    monkeypatch.setattr(sys, "argv", ["accept_content_robot", "--python", sys.executable, "--evidence", str(evidence)])
    launcher.main()
    result = json.loads((evidence / "preflight.json").read_text("utf-8"))
    assert result["record_video"] is False and result["browser_started"] is False
    assert not (evidence / "video-preflight.json").exists() and metadata_only == []
