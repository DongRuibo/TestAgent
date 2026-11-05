"""在专用 Chrome 内运行固定导出流程，不读取 Cookie 或调用隐藏接口。"""

from __future__ import annotations

import json
import re
import time
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

from robot.api.deco import keyword
from robot.libraries.BuiltIn import BuiltIn
from selenium.common.exceptions import TimeoutException
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium.webdriver import ChromeOptions
from selenium.webdriver.support.ui import WebDriverWait

from rpa_agent_module.database import JobRepository
from rpa_agent_module.models import XiaohongshuMetricsAccount, XiaohongshuMetricsRequest
from rpa_agent_module.xiaohongshu_metrics import HOME_URL, METRICS_URL, parse_metrics_export


class UserActionRequired(Exception):
    pass


class XhsMetricsLibrary:
    ROBOT_LIBRARY_SCOPE = "SUITE"
    ROBOT_AUTO_KEYWORDS = False

    def __init__(self, input_file: str, result_file: str) -> None:
        self.payload = json.loads(Path(input_file).read_text(encoding="utf-8"))
        self.result_file = Path(result_file)
        self.repository = JobRepository(Path(self.payload["database_path"]))
        self.account = XiaohongshuMetricsAccount.model_validate(self.payload["account"])
        self.query = XiaohongshuMetricsRequest.model_validate(self.payload["query"])
        self.stage = "starting"
        self.download_configured = False
        self.succeeded = False

    @keyword
    def collect_metrics(self) -> None:
        """只返回固定状态消息，浏览器错误和页面正文不进入错误摘要。"""

        try:
            browser = BuiltIn().get_library_instance("RPA.Browser.Selenium")
            self._step("opening_metrics_browser")
            options = ChromeOptions()
            options.debugger_address = f"127.0.0.1:{self.payload['debug_port']}"
            options.page_load_strategy = "eager"
            browser.create_webdriver("Chrome", options=options)
            self.driver = browser.driver
            self.driver.set_page_load_timeout(30)
            self._step("opening_metrics_tab")
            # 已知托管标签的句柄，直接切换；库的模糊匹配会逐个读取其他标签页。
            self.driver.switch_to.window(self.payload["debug_target_id"])
            # 新建任务标签页，保留专用浏览器中可能尚未保存的草稿。
            self.driver.switch_to.new_window("tab")
            self._verify_account()
            self._step("selecting_publication_dates")
            self.driver.get(METRICS_URL)
            self._wait(lambda: self._visible(".d-daterangepicker-input-start input"))
            # 先扩展结束日期，再设置开始日期，最后收紧结束日期，避免交叉日期被控件拒绝。
            today = datetime.now(ZoneInfo("Asia/Shanghai")).date().isoformat()
            self._set_date("end", today)
            self._set_date("start", self.query.start_date.isoformat())
            self._set_date("end", self.query.end_date.isoformat())
            # 失焦关闭日期浮层；只点击固定筛选标签。
            self.driver.find_element(By.XPATH, "//*[normalize-space(text())='笔记首发时间']").click()
            source_total = self._wait_for_table()
            self._step("exporting_metrics")
            incoming = self.result_file.parent / "incoming"
            incoming.mkdir(exist_ok=False)
            self.driver.execute_cdp_cmd("Browser.setDownloadBehavior", {
                "behavior": "allow", "downloadPath": str(incoming.resolve()),
            })
            self.download_configured = True
            self._visible("button.download-btn").click()
            exported = self._wait_download(incoming)
            self._step("validating_metrics")
            data = parse_metrics_export(exported, query=self.query, account=self.account, source_total=source_total)
            # 再次核对后台号，拒绝采集期间发生的账号切换。
            self._verify_account()
            data["raw_export_path"] = str(exported.resolve())
            self._write({"status": "succeeded", "result_type": "xiaohongshu_metrics", "data": data})
            self.succeeded = True
        except UserActionRequired as exc:
            self._write({"status": "needs_user_action", "message": str(exc)})
        except ValueError:
            # 校验异常来自本地固定解析器，不回显单元格或浏览器底层消息。
            self._write({"status": "failed", "error_code": "metrics_validation_failed",
                         "message": "导出字段、日期或行数校验失败，未生成可用数据。"})
        except (TimeoutException, TimeoutError):
            message = {
                "verifying_metrics_account": "未能读取后台首页的小红书账号，请检查专用 Chrome 的登录及页面状态。",
                "selecting_publication_dates": "首发日期筛选或笔记表格未就绪，请检查该账号是否已开通数据中心以及内容分析页是否正常。",
                "exporting_metrics": "官方数据导出超时，请检查专用 Chrome 的下载或验证提示。",
            }.get(self.stage, "后台页面等待超时，请检查专用 Chrome。")
            self._write({"status": "needs_user_action", "message": message + "处理后手动重新运行。"})
        except Exception as exc:
            connecting = self.stage in {"opening_metrics_browser", "opening_metrics_tab"}
            self._write({"status": "failed", "exception_type": type(exc).__name__,
                         "error_code": "browser_connection_failed" if connecting else "metrics_execution_failed",
                         "message": "无法连接专用 Chrome，请检查 Chrome 与 ChromeDriver 版本是否匹配。"
                         if connecting else "后台采集失败，请检查专用 Chrome 与当前页面适配情况。"})
        finally:
            if self.download_configured:
                try:
                    self.driver.execute_cdp_cmd("Browser.setDownloadBehavior", {"behavior": "default"})
                except Exception:
                    pass
            if self.succeeded:
                try:
                    self.driver.close()
                except Exception:
                    pass  # 关闭本次任务标签页失败不影响已经校验的数据。

    def _step(self, value: str) -> None:
        self.stage = value
        self.repository.update_step(self.payload["job_id"], value)

    def _write(self, result: dict) -> None:
        result["collection_stage"] = self.stage
        if result["status"] != "succeeded" and hasattr(self, "driver"):
            try:
                location = urlparse(self.driver.current_url)
                result["page_path"] = location.path
                # 只记录固定筛选控件，避免将整页正文或登录信息写入诊断。
                state = {}
                for side in ("start", "end"):
                    element = self._visible(f".d-daterangepicker-input-{side} input")
                    value = element.get_attribute("value") if element else ""
                    state[side] = value if re.fullmatch(r"\d{4}-\d{2}-\d{2}", value) else None
                total = self._visible(".d-pagination-total")
                state["total"] = total.text if total and re.fullmatch(r"共\s*\d+\s*条[，,]\s*\d+\s*页", total.text) else None
                button = self._visible("button.download-btn")
                state["download_enabled"] = bool(button and button.is_enabled())
                result["page_controls"] = state
                if location.hostname == "creator.xiaohongshu.com" and "statistics" in location.path:
                    self.driver.save_screenshot(str(self.result_file.parent / "metrics-page.png"))
            except Exception:
                pass
        self.result_file.write_text(json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8")

    def _visible(self, selector: str):
        return next((e for e in self.driver.find_elements(By.CSS_SELECTOR, selector) if e.is_displayed()), None)

    def _guard(self) -> None:
        if urlparse(self.driver.current_url).hostname != "creator.xiaohongshu.com":
            raise UserActionRequired("请在专用 Chrome 中登录绑定的小红书账号后重新运行。")
        # 只检查用户可见页面，不读浏览器登录材料。
        text = self.driver.find_element(By.TAG_NAME, "body").text
        if any(word in text for word in ("扫码登录", "手机号登录", "拖动滑块", "安全验证", "请完成验证")):
            raise UserActionRequired("请在专用 Chrome 中完成登录或安全验证后重新运行。")
        if "开通数据中心" in text or "数据中心未开通" in text:
            raise UserActionRequired("该账号的数据中心尚未开通，请人工处理或切换到已开通的账号。")

    def _wait(self, predicate, seconds: int = 20):
        def check(_driver):
            self._guard()
            return predicate()
        return WebDriverWait(self.driver, seconds, poll_frequency=0.5).until(check)

    def _verify_account(self) -> None:
        self._step("verifying_metrics_account")
        self.driver.get(HOME_URL)
        def read_identity():
            match = re.search(r"小红书账号\s*[:：]\s*([A-Za-z0-9_-]+)", self.driver.find_element(By.TAG_NAME, "body").text)
            return match.group(1) if match else None
        actual = self._wait(read_identity)
        if actual != self.account.red_id:
            raise UserActionRequired("当前登录的小红书号与绑定账号不一致，请切换到绑定账号后重新运行。")

    def _set_date(self, side: str, value: str) -> None:
        selector = f".d-daterangepicker-input-{side} input"
        element = self._visible(selector)
        if element.get_attribute("value") != value:
            # 受控日期组件会恢复 clear() 清掉的值，必须用键盘选择后整体替换。
            element.click()
            element.send_keys(Keys.COMMAND, "a", Keys.NULL)
            element.send_keys(value)
            element.send_keys(Keys.ENTER)
        self._wait(lambda: self._visible(selector).get_attribute("value") == value)

    def _wait_for_table(self) -> int:
        previous = None
        since = time.monotonic()
        def settled():
            nonlocal previous, since
            for side, value in (("start", self.query.start_date), ("end", self.query.end_date)):
                if self._visible(f".d-daterangepicker-input-{side} input").get_attribute("value") != value.isoformat():
                    return False
            total_element = self._visible(".d-pagination-total")
            button = self._visible("button.download-btn")
            if total_element is None or button is None or not button.is_enabled():
                return False
            match = re.search(r"共\s*(\d+)\s*条", total_element.text)
            if not match:
                return False
            total = int(match.group(1))
            rows = self.driver.find_elements(By.CSS_SELECTOR, "table tbody tr")
            signature = (total, tuple(row.text for row in rows))
            if signature != previous:
                previous, since = signature, time.monotonic()
            # 输入事件后等待表格稳定，下载后还须以日期和总条数验证。
            return (total,) if time.monotonic() - since >= 2 else False
        return self._wait(settled)[0]

    def _wait_download(self, directory: Path) -> Path:
        previous = None
        since = time.monotonic()
        def complete():
            nonlocal previous, since
            files = list(directory.glob("*.xlsx"))
            if len(files) > 1:
                raise ValueError("下载数量异常")
            if not files or list(directory.glob("*.crdownload")):
                return False
            size = files[0].stat().st_size
            if size > 10 * 1024 * 1024:
                raise ValueError("下载过大")
            if size != previous:
                previous, since = size, time.monotonic()
            return files[0] if size and time.monotonic() - since >= 1 else False
        return self._wait(complete, 45)
