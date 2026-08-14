"""校验外部 Agent 提供的本地图片路径、目录边界和真实图片格式。"""

from __future__ import annotations

from pathlib import Path

from PIL import Image, UnidentifiedImageError

from .exceptions import JobInputError

MAX_IMAGE_BYTES = 32 * 1024 * 1024
ALLOWED_FORMATS = {"JPEG", "PNG", "WEBP"}


def _is_within(path: Path, root: Path) -> bool:
    """判断解析后的文件是否位于允许根目录内。"""

    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


def validate_image_paths(image_paths: list[Path], allowed_roots: tuple[Path, ...]) -> list[str]:
    """返回校验后的图片绝对路径，拒绝越界、伪格式或超大文件。"""

    roots = tuple(root.resolve() for root in allowed_roots)
    validated: list[str] = []
    for supplied_path in image_paths:
        path = supplied_path.expanduser().resolve()
        if not any(_is_within(path, root) for root in roots):
            raise JobInputError(f"图片不在允许目录中：{supplied_path.name}")
        if not path.is_file() or path.is_symlink():
            raise JobInputError(f"图片文件不存在或类型不安全：{supplied_path.name}")
        if path.stat().st_size <= 0 or path.stat().st_size > MAX_IMAGE_BYTES:
            raise JobInputError(f"图片大小必须大于0且不超过32MB：{supplied_path.name}")
        try:
            # 真实解码检查可避免把伪造后缀的任意文件交给浏览器上传控件。
            with Image.open(path) as image:
                image.verify()
                image_format = image.format or ""
        except (UnidentifiedImageError, OSError) as exc:
            raise JobInputError(f"文件不是有效图片：{supplied_path.name}") from exc
        if image_format not in ALLOWED_FORMATS:
            raise JobInputError(f"图片仅支持 JPEG、PNG 或 WebP：{supplied_path.name}")
        validated.append(str(path))
    return validated
