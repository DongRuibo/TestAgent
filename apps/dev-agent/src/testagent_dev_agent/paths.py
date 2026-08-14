"""开发测试台访问 Monorepo 固定样本和本地 Artifact 的路径。"""

from __future__ import annotations

import os
from pathlib import Path


def repository_root() -> Path:
    configured = os.getenv("TESTAGENT_PROJECT_ROOT", "").strip()
    if configured:
        return Path(configured).expanduser().resolve()
    return Path(__file__).resolve().parents[4]


REPOSITORY_ROOT = repository_root()
