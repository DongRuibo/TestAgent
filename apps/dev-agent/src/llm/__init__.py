"""模型客户端包，对外统一暴露客户端和配置对象。"""

from .client import LLMClient, LLMConfig

# 明确声明公共 API，避免使用方依赖包内的其他实现细节。
__all__ = ["LLMClient", "LLMConfig"]
