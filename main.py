"""Mini Agent V2 的命令行入口：运行支持本地、Web 和云端工具的 Agent。"""

from __future__ import annotations

import argparse
from collections.abc import Callable
from typing import Protocol

from dotenv import load_dotenv

from agent.loop import AgentLoop
from llm.client import LLMClient


MAX_HISTORY_MESSAGES = 20


class RunnableAgent(Protocol):
    def run(self, task: str, *, history: list[dict[str, str]] | None = None) -> str: ...


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
    return parser.parse_args()


def main() -> None:
    """加载配置；有命令行任务时单轮运行，否则进入持续对话。"""
    # 从项目根目录的 .env 文件加载模型密钥、模型名和可选的服务地址。
    load_dotenv()
    args = parse_args()

    # 把配置错误转换成简洁的命令行提示，避免用户看到冗长的异常堆栈。
    try:
        client = LLMClient.from_env()
    except RuntimeError as exc:
        raise SystemExit(f"Configuration error: {exc}") from exc

    # V2 将模型交给 Agent Loop，由循环负责分发多个工具并生成最终回答。
    agent = AgentLoop(client)
    if args.task is not None:
        task = args.task.strip()
        if not task:
            raise SystemExit("Task cannot be empty.")
        answer = agent.run(task)
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
            output_fn("上下文已清空。")
            continue

        try:
            answer = agent.run(task, history=history)
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
