"""用真实Selenium库验证失败钩子关闭，不启动浏览器或读取账号。"""

import json
import subprocess
import sys

from rpa_agent_module.environment import subprocess_environment


def test_real_selenium_failure_hook_is_disabled(tmp_path):
    # 独立进程避免Robot替身模块缓存影响真实库，也将驱动缓存限制到临时目录。
    environment = subprocess_environment()
    environment.update(ROBOCORP_HOME=str(tmp_path / "robocorp"), PYTHONDONTWRITEBYTECODE="1",
                       PYTHON_DOTENV_DISABLED="1")
    script = """
import json
from RPA.Browser.Selenium import Selenium
calls = []
default = Selenium(auto_close=False)
default.capture_page_screenshot = lambda: calls.append('default')
default.failure_occurred()
disabled = Selenium(auto_close=False, run_on_failure=None)
disabled.capture_page_screenshot = lambda: calls.append('disabled')
disabled.failure_occurred()
print(json.dumps({'calls': calls, 'disabled_hook': disabled.run_on_failure_keyword}))
"""
    completed = subprocess.run([sys.executable, "-c", script], cwd=tmp_path, env=environment,
                               capture_output=True, text=True, timeout=30, check=True)
    result = json.loads(completed.stdout.strip().splitlines()[-1])
    assert result == {"calls": ["default"], "disabled_hook": None}
    assert not list(tmp_path.rglob("*.png"))
