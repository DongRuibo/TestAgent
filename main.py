"""兼容启动入口；开发 Agent 实现位于 ``apps/dev-agent``。"""

from testagent_dev_agent.cli import main, parse_args, run_interactive

__all__ = ["main", "parse_args", "run_interactive"]


if __name__ == "__main__":
    main()
