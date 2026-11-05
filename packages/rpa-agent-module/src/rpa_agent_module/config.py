"""定义并校验嵌入式 RPA 模块的固定部署配置。"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlparse

from .exceptions import ConfigurationError
from .models import XiaohongshuMetricsAccount


def _default_data_dir() -> Path:
    """返回符合 macOS 习惯的默认模块数据目录。"""

    return Path.home() / "Library/Application Support/RPAAgentModule"


def _default_output_dir() -> Path:
    """返回默认 CSV 归档目录。"""

    return Path.home() / "Documents/RPA工作台/查询结果"


def _normalize_absolute_path(value: Path, field_name: str) -> Path:
    """展开用户目录并将配置路径标准化为绝对路径。"""

    path = value.expanduser()
    if not path.is_absolute():
        raise ConfigurationError(f"{field_name} 必须是绝对路径")
    return path.resolve()


def _require_http_url(value: str, field_name: str) -> str:
    """只允许部署人员配置格式明确的 HTTP(S) 页面地址。"""

    cleaned = value.strip()
    parsed = urlparse(cleaned)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ConfigurationError(f"{field_name} 必须是有效的 HTTP(S) URL")
    return cleaned.rstrip("/")


def _require_safe_selector(value: str, field_name: str, *, required: bool = True) -> str:
    """要求显式定位器并阻止把最终发布动作配置进草稿流程。"""

    cleaned = value.strip()
    if not cleaned:
        if required:
            raise ConfigurationError(f"{field_name} 不能为空")
        return ""
    # SeleniumLibrary 对裸 CSS 的解析可能落到 id/name，必须保留显式前缀。
    if not cleaned.casefold().startswith(("css:", "xpath:")):
        raise ConfigurationError(f"{field_name} 必须使用 css: 或 xpath: 前缀")
    if field_name == "draft_save" and any(word in cleaned.casefold() for word in ("publish", "发布")):
        raise ConfigurationError("draft_save 不得指向最终发布按钮")
    if len(cleaned) > 500:
        raise ConfigurationError(f"{field_name} 选择器过长")
    return cleaned


@dataclass(frozen=True)
class XiaohongshuSelectors:
    """保存已在专用 Chrome 中验证的小红书页面选择器。"""

    login: str = "css:.user-info"
    file_input: str = "css:input.upload-input"
    title: str = 'css:input[placeholder="填写标题会有更多赞哦"]'
    body: str = "css:.tiptap.ProseMirror[contenteditable=\"true\"]"
    tag: str = ""
    upload_done: str = "css:img.img.preview"
    draft_save: str = ""
    draft_saved: str = "css:.permission-info"
    draft_box: str = "css:.header-draft"
    draft_image_tab: str = (
        'xpath://div[contains(@class, "draft-drawer")]//div[contains(@class, "tab-item")]'
        '[.//span[starts-with(normalize-space(.), "图文笔记(")]]'
    )
    draft_title: str = "css:.draft-title-text.has-title"
    challenge: str = ""

    def __post_init__(self) -> None:
        """逐项校验定位器，允许标签、保存按钮和风控标志留空。"""

        optional = {"tag", "draft_save", "challenge"}
        for name in self.__dataclass_fields__:
            value = _require_safe_selector(getattr(self, name), name, required=name not in optional)
            object.__setattr__(self, name, value)

    def as_dict(self) -> dict[str, str]:
        """生成只包含固定页面定位器的 Robot 输入字典。"""

        return {name: str(getattr(self, name)) for name in self.__dataclass_fields__}


@dataclass(frozen=True)
class RpaModuleConfig:
    """集中保存路径、页面契约和小红书开关，不自动读取宿主 `.env`。"""

    data_dir: Path = field(default_factory=_default_data_dir)
    allowed_image_roots: tuple[Path, ...] | None = None
    csv_target_url: str = "http://127.0.0.1:8041/data-query"
    csv_output_dir: Path = field(default_factory=_default_output_dir)
    xiaohongshu_enabled: bool = False
    xiaohongshu_metrics_account: XiaohongshuMetricsAccount | None = None
    xiaohongshu_creator_url: str = (
        "https://creator.xiaohongshu.com/publish/publish"
        "?source=official&from=tab_switch&target=image"
    )
    xiaohongshu_selectors: XiaohongshuSelectors = field(default_factory=XiaohongshuSelectors)

    def __post_init__(self) -> None:
        """把路径和 URL 转为稳定形式，并拒绝空图片根目录。"""

        data_dir = _normalize_absolute_path(self.data_dir, "data_dir")
        if self.xiaohongshu_metrics_account is not None:
            object.__setattr__(
                self, "xiaohongshu_metrics_account",
                XiaohongshuMetricsAccount.model_validate(self.xiaohongshu_metrics_account),
            )
        output_dir = _normalize_absolute_path(self.csv_output_dir, "csv_output_dir")
        configured_roots = self.allowed_image_roots or (data_dir / "inputs",)
        roots = tuple(_normalize_absolute_path(path, "allowed_image_roots") for path in configured_roots)
        if not roots:
            raise ConfigurationError("allowed_image_roots 至少需要一个目录")
        object.__setattr__(self, "data_dir", data_dir)
        object.__setattr__(self, "csv_output_dir", output_dir)
        object.__setattr__(self, "allowed_image_roots", roots)
        object.__setattr__(self, "csv_target_url", _require_http_url(self.csv_target_url, "csv_target_url"))
        object.__setattr__(
            self,
            "xiaohongshu_creator_url",
            _require_http_url(self.xiaohongshu_creator_url, "xiaohongshu_creator_url"),
        )
