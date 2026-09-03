#!/usr/bin/env python3
"""启动本地 Mem0-primary + Elasticsearch Memory 后端。"""

from __future__ import annotations

import stat
import subprocess
import sys
from pathlib import Path

from dotenv import dotenv_values


PROJECT_ROOT = Path(__file__).resolve().parents[1]
ALLOWED_ACTIONS = {"up", "down", "ps", "logs"}
COMPOSE_FILES = (
    "compose.yaml",
    "compose.elasticsearch.yaml",
    "compose.elasticsearch-runtime.yaml",
)


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
    memory_root = memory_env.parent
    compose_files = [memory_root / name for name in COMPOSE_FILES]
    if not all(path.is_file() for path in compose_files):
        raise SystemExit("MEMORY_ENV_FILE does not belong to a LazyTT Memory checkout")

    command = ["docker", "compose", "--env-file", str(memory_env)]
    for compose_file in compose_files:
        command.extend(["-f", str(compose_file)])
    if action == "up":
        command.extend(
            [
                "up",
                "--build",
                "-d",
                "mem0-config-elasticsearch",
                "memory-service-elasticsearch",
            ]
        )
    elif action == "down":
        command.append("down")
    elif action == "ps":
        command.extend(["ps", "-a"])
    else:
        command.extend(
            [
                "logs",
                "--tail",
                "200",
                "memory-service-elasticsearch",
                "mem0",
                "elasticsearch",
                "mem0-postgres",
            ]
        )

    subprocess.run(
        command,
        cwd=memory_root,
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
