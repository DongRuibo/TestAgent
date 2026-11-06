"""只在验收启动器生成的测试子进程中阻断驱动下载，保留真实 WebDriver。"""

from RPA.core import webdriver as rpa_webdriver
from selenium.webdriver.common.selenium_manager import SeleniumManager


def refuse_driver_download(*args, **kwargs):
    """本地驱动不存在或附着失败时明确失败，不允许回退联网下载。"""

    raise RuntimeError("本地验收禁止下载驱动：请提供已确认匹配的本地 ChromeDriver")


# 这两个入口分别覆盖 rpaframework 的失败回退和 Selenium Manager 的自动解析下载。
rpa_webdriver.download = refuse_driver_download
SeleniumManager.binary_paths = refuse_driver_download
RPA_ACCEPTANCE_NO_DOWNLOAD = True
