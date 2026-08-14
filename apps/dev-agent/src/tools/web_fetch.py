"""兼容入口；实现已迁移到独立的 ``testagent_web_tools`` 包。"""

from testagent_web_tools.fetch import web_fetch

__all__ = ["web_fetch"]
