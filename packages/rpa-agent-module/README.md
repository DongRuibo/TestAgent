# RPA Agent Module

从 `rpa_agent_module-0.1.0-py3-none-any.whl` 恢复的可维护源码工程。

当前提供两个固定工作流：

- `xiaohongshu_draft`：把 Agent 已审核图文保存到小红书草稿箱，不最终发布。
- `csv_archive`：操作已适配查询页并归档 CSV。

0.1.1 增加小红书专用 Chrome 常驻复用：模块首次启动受控浏览器，后续任务附着到同一浏览器；用户关闭后下次任务自动重启。模块不会删除 Chrome 的 `SingletonLock`，也不会接管非模块启动的同 Profile 浏览器。
