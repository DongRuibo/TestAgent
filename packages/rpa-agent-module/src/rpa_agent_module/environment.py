"""只把浏览器和 Robot 所需的系统环境传入子进程。"""

import os


def subprocess_environment() -> dict[str, str]:
    """不复制宿主密钥、代理设置或任意 Python 导入路径。"""

    allowed = ("PATH", "HOME", "TMPDIR", "TMP", "TEMP", "LANG", "LC_ALL", "LC_CTYPE",
               "USER", "LOGNAME", "SHELL", "SYSTEMROOT", "WINDIR", "DISPLAY", "XAUTHORITY")
    return {key: os.environ[key] for key in allowed if key in os.environ}
