#!/usr/bin/env python3
"""使用 TestAgent 的 Kimi 配置启动本地 LazyTT Memory cloud 后端。"""

from __future__ import annotations

import os
import stat
import subprocess
import sys
from pathlib import Path

from dotenv import dotenv_values


PROJECT_ROOT = Path(__file__).resolve().parents[1]
ALLOWED_ACTIONS = {"up", "down", "ps", "logs"}
OFFICIAL_KIMI_BASE_URL = "https://api.kimi.com/coding/v1"


def main() -> None:
    action = sys.argv[1] if len(sys.argv) == 2 else "up"
    if action not in ALLOWED_ACTIONS:
        raise SystemExit("Usage: memory_backend.py [up|down|ps|logs]")

    agent_values = _load_private_env(PROJECT_ROOT / ".env")
    memory_env_file = str(agent_values.get("MEMORY_ENV_FILE") or "").strip()
    if not memory_env_file:
        raise SystemExit("MEMORY_ENV_FILE is required in TestAgent .env")
    memory_env = Path(memory_env_file).expanduser()
    if not memory_env.is_absolute():
        raise SystemExit("MEMORY_ENV_FILE must be an absolute path")
    _load_private_env(memory_env)
    memory_root = memory_env.parent
    backend_script = memory_root / "scripts" / "backend-stack.sh"
    if not backend_script.is_file():
        raise SystemExit("MEMORY_ENV_FILE does not belong to a LazyTT Memory checkout")

    child_env = os.environ.copy()
    if action == "up":
        api_key = str(agent_values.get("OPENAI_API_KEY") or "").strip()
        base_url = str(agent_values.get("OPENAI_BASE_URL") or "").rstrip("/")
        model = str(agent_values.get("OPENAI_MODEL") or "").strip()
        if not api_key:
            raise SystemExit("OPENAI_API_KEY is required for automatic memory extraction")
        if base_url != OFFICIAL_KIMI_BASE_URL or model != "k3":
            raise SystemExit(
                "automatic memory extraction requires TestAgent OPENAI_BASE_URL="
                f"{OFFICIAL_KIMI_BASE_URL} and OPENAI_MODEL=k3"
            )
        child_env.update(
            {
                "KIMI_EXTRACT_API_KEY": api_key,
                "KIMI_EXTRACT_BASE_URL": base_url,
                "KIMI_EXTRACT_MODEL": model,
                "MEMORY_EXTRACT_MODE": "kimi",
            }
        )

    subprocess.run(
        [str(backend_script), "cloud", action],
        cwd=memory_root,
        env=child_env,
        check=True,
    )


def _load_private_env(path: Path) -> dict[str, str | None]:
    try:
        metadata = path.lstat()
    except OSError as exc:
        raise SystemExit(f"cannot inspect private env file: {path}") from exc
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
        raise SystemExit(f"private env file must be a regular file: {path}")
    if metadata.st_mode & 0o077:
        raise SystemExit(f"private env file must use chmod 600: {path}")
    if metadata.st_size > 64 * 1024:
        raise SystemExit(f"private env file exceeds 64 KiB: {path}")
    return dict(dotenv_values(path))


if __name__ == "__main__":
    main()
