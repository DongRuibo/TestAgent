"""Mini Agent V2 的命令行入口：运行支持本地、Web 和云端工具的 Agent。"""

from __future__ import annotations

import argparse
from collections.abc import Callable
from typing import Protocol
from uuid import uuid4

from dotenv import load_dotenv

from agent.loop import AgentLoop, DEFAULT_TOOL_HANDLERS, TOOL_DEFINITIONS
from agent.skill_tools import register_skill_tools
from agent.skills import SkillCatalog, SkillConfigurationError
from llm.client import LLMClient
from memory.client import build_memory_client_from_env
from tools.lazytt_mcp import LazyTtMcpError, build_lazytt_mcp_tools_from_env
from testagent_dev_agent.paths import REPOSITORY_ROOT


MAX_HISTORY_MESSAGES = 20
LOCAL_AREA_TOOL_NAME = "calculate_material_quantities"
MCP_AREA_TOOL_SUFFIX = "__area_calculate"


class RunnableAgent(Protocol):
    def run(
        self,
        task: str,
        *,
        history: list[dict[str, str]] | None = None,
        episode_id: str | None = None,
    ) -> str: ...


def parse_args() -> argparse.Namespace:
    """解析命令行参数。

    task 是可选位置参数。未提供时，程序会在 main 函数中进入交互输入模式。
    """
    parser = argparse.ArgumentParser(description="Mini Agent V2")
    parser.add_argument(
        "task",
        nargs="?",
        help="Message to send. If omitted, the program prompts for it.",
    )
    parser.add_argument("--skill", help="Explicitly activate one loaded Skill.")
    parser.add_argument(
        "--list-skills",
        action="store_true",
        help="List loaded Skills and exit.",
    )
    return parser.parse_args()


def main() -> None:
    """加载配置；有命令行任务时单轮运行，否则进入持续对话。"""
    # 从项目根目录的 .env 文件加载模型密钥、模型名和可选的服务地址。
    load_dotenv()
    args = parse_args()
    try:
        skill_catalog = SkillCatalog.from_directory(
            REPOSITORY_ROOT / ".agents" / "skills"
        )
    except SkillConfigurationError as exc:
        raise SystemExit(f"Skill configuration error: {exc}") from exc
    if getattr(args, "list_skills", False):
        for skill in skill_catalog.skills:
            print(f"{skill.name}\t{skill.version}\t{skill.description}")
        return
    if getattr(args, "skill", None) and args.task is None:
        raise SystemExit("--skill requires a task.")

    # 把配置错误转换成简洁的命令行提示，避免用户看到冗长的异常堆栈。
    try:
        client = LLMClient.from_env()
    except RuntimeError as exc:
        raise SystemExit(f"Configuration error: {exc}") from exc

    # V2 将模型交给 Agent Loop，由循环负责分发多个工具并生成最终回答。
    try:
        memory = build_memory_client_from_env()
    except RuntimeError as exc:
        raise SystemExit(f"Memory configuration error: {exc}") from exc
    definitions = dict(TOOL_DEFINITIONS)
    handlers = register_skill_tools(DEFAULT_TOOL_HANDLERS, skill_catalog)
    try:
        mcp_tools = build_lazytt_mcp_tools_from_env()
    except LazyTtMcpError as exc:
        raise SystemExit(f"MCP configuration error: {exc}") from exc
    mcp_budgets = None
    if mcp_tools is not None:
        if any(
            name.startswith("mcp__") and name.endswith(MCP_AREA_TOOL_SUFFIX)
            for name in mcp_tools.tool_definitions
        ):
            definitions.pop(LOCAL_AREA_TOOL_NAME, None)
            handlers.pop(LOCAL_AREA_TOOL_NAME, None)
        collisions = set(definitions) & set(mcp_tools.tool_definitions)
        if collisions:
            names = ", ".join(sorted(collisions))
            raise SystemExit(f"MCP tool name collision: {names}")
        definitions.update(mcp_tools.tool_definitions)
        handlers.update(mcp_tools.tool_handlers)
        mcp_budgets = mcp_tools.tool_budgets
    agent = AgentLoop(
        client,
        memory=memory,
        tool_definitions=definitions,
        tool_handlers=handlers,
        tool_budgets=mcp_budgets,
        skill_catalog=skill_catalog,
    )
    if args.task is not None:
        task = args.task.strip()
        if not task:
            raise SystemExit("Task cannot be empty.")
        try:
            answer = agent.run(
                task,
                episode_id=str(uuid4()),
                skill_name=getattr(args, "skill", None),
            )
        except SkillConfigurationError as exc:
            raise SystemExit(f"Skill activation error: {exc}") from exc
        print(f"Assistant: {answer}")
        return

    run_interactive(agent)


def run_interactive(
    agent: RunnableAgent,
    *,
    input_fn: Callable[[str], str] = input,
    output_fn: Callable[[str], None] = print,
) -> None:
    """运行命令行多轮对话，只保存已经完成的用户/助手文本轮次。"""
    history: list[dict[str, str]] = []
    episode_id = str(uuid4())
    output_fn("Mini Agent 已启动。输入 /clear 清空上下文，输入 /exit 退出。")

    while True:
        try:
            task = input_fn("You: ").strip()
        except (EOFError, KeyboardInterrupt):
            output_fn("\n对话已结束。")
            return

        if not task:
            continue
        command = task.lower()
        if command in {"/exit", "/quit", "exit", "quit"}:
            output_fn("对话已结束。")
            return
        if command == "/clear":
            history.clear()
            episode_id = str(uuid4())
            output_fn("上下文已清空。")
            continue

        try:
            answer = agent.run(task, history=history, episode_id=episode_id)
        except KeyboardInterrupt:
            output_fn("\n当前任务已中断。")
            continue
        except Exception as exc:
            output_fn(f"Assistant error: {exc}")
            continue

        output_fn(f"Assistant: {answer}")
        history.extend(
            [
                {"role": "user", "content": task},
                {"role": "assistant", "content": answer},
            ]
        )
        if len(history) > MAX_HISTORY_MESSAGES:
            del history[:-MAX_HISTORY_MESSAGES]


if __name__ == "__main__":
    # 只有直接执行 main.py 时才启动程序；被其他模块导入时不会自动运行。
    main()
