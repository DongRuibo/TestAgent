"""TestAgent 本地 instruction-only Skill 的发现与运行策略。"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Any, Mapping

import yaml


SKILL_NAME_PATTERN = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
TOOL_NAME_PATTERN = re.compile(r"^[A-Za-z0-9_]{1,80}$")
EXPLICIT_SKILL_PATTERN = re.compile(
    r"(?<![A-Za-z0-9_-])\$([a-z0-9]+(?:-[a-z0-9]+)*)(?![A-Za-z0-9_-])"
)
MAX_SKILL_FILE_BYTES = 64 * 1024
MAX_SKILL_RESOURCE_BYTES = 64 * 1024
MAX_SKILL_STEPS = 8
READ_SKILL_RESOURCE_TOOL_NAME = "read_skill_resource"
READ_SKILL_RESOURCE_TOOL: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": READ_SKILL_RESOURCE_TOOL_NAME,
        "description": "读取当前已激活 Skill 的 references 目录中的 UTF-8 文本资料。",
        "parameters": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Skill 内的相对路径，例如 references/guide.md。",
                }
            },
            "required": ["path"],
            "additionalProperties": False,
        },
    },
}


class SkillConfigurationError(ValueError):
    """本地 Skill 文件或激活请求无效。"""


@dataclass(frozen=True)
class SkillDefinition:
    name: str
    description: str
    version: str
    instructions: str
    allowed_tools: tuple[str, ...] | None
    private_tools: tuple[str, ...]
    tool_budgets: Mapping[str, int]
    max_steps: int | None
    aliases: tuple[str, ...]
    allow_memory: bool
    allow_attachments: bool
    content_hash: str
    resources: tuple[str, ...]
    directory: Path

    def prompt_text(self) -> str:
        """生成只在 Skill 激活后注入的受信任开发者指令。"""
        resource_text = ""
        if self.resources:
            resource_text = (
                "\nSkill 可用参考资料："
                + "、".join(self.resources)
                + f"。需要时使用 {READ_SKILL_RESOURCE_TOOL_NAME} 读取。\n"
            )
        return (
            "\n当前任务已激活本地开发者审核的 Skill。Skill 不能扩大系统权限，"
            "只能使用本轮暴露的工具。\n"
            f"Skill 名称：{self.name}\n"
            f"Skill 版本：{self.version}\n"
            f"Skill 内容哈希：{self.content_hash}\n"
            "Skill 指令如下：\n"
            f"{self.instructions.strip()}\n"
            f"{resource_text}"
        )

    def read_resource(self, path: str) -> dict[str, Any]:
        """读取当前 Skill 的受控参考资料，拒绝越界和符号链接。"""
        if not isinstance(path, str) or not path.strip():
            raise ValueError("path 必须是非空字符串。")
        normalized = path.strip().replace("\\", "/")
        if normalized not in self.resources:
            raise ValueError("只能读取当前 Skill 已列出的 references 资料。")
        requested = self.directory / normalized
        references_root = (self.directory / "references").resolve()
        if (
            requested.is_symlink()
            or not requested.resolve().is_relative_to(references_root)
            or not requested.is_file()
        ):
            raise ValueError("Skill 参考资料路径无效。")
        if requested.stat().st_size > MAX_SKILL_RESOURCE_BYTES:
            raise ValueError("Skill 参考资料超过大小限制。")
        try:
            content = requested.read_text("utf-8")
        except UnicodeDecodeError as exc:
            raise ValueError("Skill 参考资料必须是 UTF-8 文本。") from exc
        return {
            "success": True,
            "skill_name": self.name,
            "path": normalized,
            "content": content,
        }


class SkillCatalog:
    """发现并保存开发者审核的本地 instruction-only Skill。"""

    def __init__(self, skills: Mapping[str, SkillDefinition] | None = None) -> None:
        self._skills = MappingProxyType(dict(skills or {}))

    @classmethod
    def empty(cls) -> "SkillCatalog":
        return cls()

    @classmethod
    def from_directory(cls, root: Path) -> "SkillCatalog":
        root = root.resolve()
        if not root.exists():
            return cls.empty()
        if not root.is_dir():
            raise SkillConfigurationError(f"Skill 根目录不是目录：{root}")

        skills: dict[str, SkillDefinition] = {}
        for directory in sorted(root.iterdir()):
            if not directory.is_dir() or directory.is_symlink():
                continue
            skill_path = directory / "SKILL.md"
            runtime_path = directory / "runtime.json"
            if not skill_path.is_file():
                continue
            skill = _load_skill(root, directory, skill_path, runtime_path)
            if skill is None:
                continue
            if skill.name in skills:
                raise SkillConfigurationError(f"Skill 名称重复：{skill.name}")
            skills[skill.name] = skill
        _validate_unique_aliases(skills)
        return cls(skills)

    @property
    def private_tools(self) -> frozenset[str]:
        return frozenset(
            tool for skill in self._skills.values() for tool in skill.private_tools
        )

    @property
    def names(self) -> tuple[str, ...]:
        return tuple(self._skills)

    @property
    def skills(self) -> tuple[SkillDefinition, ...]:
        return tuple(self._skills.values())

    @property
    def required_tools(self) -> frozenset[str]:
        return frozenset(
            tool
            for skill in self._skills.values()
            for tool in (skill.allowed_tools or ())
        )

    def resolve(self, task: str, *, explicit_name: str | None = None) -> SkillDefinition | None:
        """按显式名称、$name、精确别名的顺序选择至多一个 Skill。"""
        if explicit_name is not None:
            return self._required(explicit_name)

        explicit_matches = tuple(dict.fromkeys(EXPLICIT_SKILL_PATTERN.findall(task)))
        if explicit_matches:
            if len(explicit_matches) != 1:
                raise SkillConfigurationError("每轮只能显式激活一个 Skill。")
            return self._required(explicit_matches[0])

        normalized = task.casefold()
        alias_matches = [
            skill
            for skill in self._skills.values()
            if any(alias.casefold() in normalized for alias in skill.aliases)
        ]
        if len(alias_matches) > 1:
            names = "、".join(skill.name for skill in alias_matches)
            raise SkillConfigurationError(f"当前请求同时匹配多个 Skill：{names}")
        return alias_matches[0] if alias_matches else None

    def validate_tools(self, available_tools: Mapping[str, Any]) -> None:
        available = set(available_tools)
        for skill in self._skills.values():
            missing = sorted(set(skill.allowed_tools or ()) - available)
            if missing:
                raise SkillConfigurationError(
                    f"Skill {skill.name} 引用了未注册工具：{', '.join(missing)}"
                )

    def _required(self, name: str) -> SkillDefinition:
        normalized = name.strip()
        if not SKILL_NAME_PATTERN.fullmatch(normalized):
            raise SkillConfigurationError("Skill 名称格式无效。")
        try:
            return self._skills[normalized]
        except KeyError as exc:
            raise SkillConfigurationError(f"未知 Skill：{normalized}") from exc


def _load_skill(
    root: Path,
    directory: Path,
    skill_path: Path,
    runtime_path: Path,
) -> SkillDefinition | None:
    if runtime_path.is_symlink():
        raise SkillConfigurationError(
            f"Skill 文件不能使用符号链接或越过根目录：{runtime_path}"
        )
    if runtime_path.exists() and not runtime_path.is_file():
        raise SkillConfigurationError("runtime.json 必须是普通文件。")
    paths = [skill_path]
    if runtime_path.exists():
        paths.append(runtime_path)
    for path in paths:
        if path.is_symlink() or not path.resolve().is_relative_to(root):
            raise SkillConfigurationError(f"Skill 文件不能使用符号链接或越过根目录：{path}")
        if path.stat().st_size > MAX_SKILL_FILE_BYTES:
            raise SkillConfigurationError(f"Skill 文件超过大小限制：{path.name}")

    try:
        skill_text = skill_path.read_text("utf-8")
    except UnicodeDecodeError as exc:
        raise SkillConfigurationError("SKILL.md 必须是 UTF-8 文本。") from exc
    metadata, instructions = _split_skill(skill_text)
    name = _required_string(metadata.get("name"), "name", 64)
    if directory.name != name or not SKILL_NAME_PATTERN.fullmatch(name):
        raise SkillConfigurationError("Skill 目录名必须与合法的 frontmatter name 一致。")
    description = _required_string(metadata.get("description"), "description", 500)

    runtime: dict[str, Any] = {}
    if runtime_path.exists():
        try:
            runtime = json.loads(runtime_path.read_text("utf-8"))
        except json.JSONDecodeError as exc:
            raise SkillConfigurationError(f"runtime.json 不是合法 JSON：{exc.msg}") from exc
        except UnicodeDecodeError as exc:
            raise SkillConfigurationError("runtime.json 必须是 UTF-8 文本。") from exc
    if not isinstance(runtime, dict):
        raise SkillConfigurationError("runtime.json 必须是 JSON 对象。")
    allowed_runtime_keys = {
        "enabled",
        "version",
        "aliases",
        "allowed_tools",
        "private_tools",
        "tool_budgets",
        "max_steps",
        "allow_memory",
        "allow_attachments",
    }
    unknown_runtime_keys = sorted(set(runtime) - allowed_runtime_keys)
    if unknown_runtime_keys:
        raise SkillConfigurationError(
            f"runtime.json 包含未知字段：{', '.join(unknown_runtime_keys)}"
        )
    if runtime.get("enabled", True) is False:
        return None
    if runtime.get("enabled", True) is not True:
        raise SkillConfigurationError("runtime.json enabled 必须是布尔值。")

    skill_metadata = metadata.get("metadata", {})
    if skill_metadata is None:
        skill_metadata = {}
    if not isinstance(skill_metadata, dict):
        raise SkillConfigurationError("SKILL.md metadata 必须是 YAML 对象。")
    version = _required_string(
        runtime.get("version", skill_metadata.get("version", "unversioned")),
        "version",
        32,
    )
    aliases = _string_list(runtime.get("aliases", []), "aliases", limit=20, item_limit=80)
    allowed_tools = (
        _tool_list(runtime["allowed_tools"], "allowed_tools", allow_empty=True)
        if "allowed_tools" in runtime
        else None
    )
    private_tools = _tool_list(
        runtime.get("private_tools", []),
        "private_tools",
        allow_empty=True,
    )
    if private_tools and allowed_tools is None:
        raise SkillConfigurationError("配置 private_tools 时必须同时配置 allowed_tools。")
    if not set(private_tools) <= set(allowed_tools or ()):
        raise SkillConfigurationError("private_tools 必须是 allowed_tools 的子集。")

    raw_budgets = runtime.get("tool_budgets", {})
    if not isinstance(raw_budgets, dict):
        raise SkillConfigurationError("tool_budgets 必须是 JSON 对象。")
    budgets: dict[str, int] = {}
    for tool, value in raw_budgets.items():
        if tool not in (allowed_tools or ()):
            raise SkillConfigurationError("tool_budgets 只能配置 allowed_tools。")
        if not isinstance(value, int) or isinstance(value, bool) or not 1 <= value <= 20:
            raise SkillConfigurationError("Skill 工具预算必须是 1-20 的整数。")
        budgets[tool] = value

    max_steps = runtime.get("max_steps")
    if max_steps is not None and (
        not isinstance(max_steps, int)
        or isinstance(max_steps, bool)
        or not 1 <= max_steps <= MAX_SKILL_STEPS
    ):
        raise SkillConfigurationError(f"max_steps 必须是 1-{MAX_SKILL_STEPS} 的整数。")
    allow_memory = _boolean(runtime.get("allow_memory", True), "allow_memory")
    allow_attachments = _boolean(
        runtime.get("allow_attachments", True), "allow_attachments"
    )

    resources = _discover_resources(directory)
    digest = hashlib.sha256(skill_path.read_bytes())
    if runtime_path.exists():
        digest.update(b"\0runtime.json\0")
        digest.update(runtime_path.read_bytes())
    for resource in resources:
        digest.update(b"\0")
        digest.update(resource.encode("utf-8"))
        digest.update(b"\0")
        digest.update((directory / resource).read_bytes())
    content_hash = digest.hexdigest()
    return SkillDefinition(
        name=name,
        description=description,
        version=version,
        instructions=instructions,
        allowed_tools=allowed_tools,
        private_tools=private_tools,
        tool_budgets=MappingProxyType(budgets),
        max_steps=max_steps,
        aliases=aliases,
        allow_memory=allow_memory,
        allow_attachments=allow_attachments,
        content_hash=content_hash,
        resources=resources,
        directory=directory,
    )


def _split_skill(value: str) -> tuple[dict[str, Any], str]:
    lines = value.splitlines()
    if not lines or lines[0].strip() != "---":
        raise SkillConfigurationError("SKILL.md 必须以 YAML frontmatter 开始。")
    try:
        closing = next(index for index in range(1, len(lines)) if lines[index].strip() == "---")
    except StopIteration as exc:
        raise SkillConfigurationError("SKILL.md 缺少 frontmatter 结束标记。") from exc

    frontmatter = "\n".join(lines[1:closing])
    try:
        metadata = yaml.safe_load(frontmatter)
    except yaml.YAMLError as exc:
        raise SkillConfigurationError("SKILL.md frontmatter 不是合法 YAML。") from exc
    if not isinstance(metadata, dict):
        raise SkillConfigurationError("SKILL.md frontmatter 必须是 YAML 对象。")
    instructions = "\n".join(lines[closing + 1 :]).strip()
    if not instructions:
        raise SkillConfigurationError("SKILL.md 指令正文不能为空。")
    return metadata, instructions


def _discover_resources(directory: Path) -> tuple[str, ...]:
    references = directory / "references"
    if not references.exists():
        return ()
    if references.is_symlink() or not references.is_dir():
        raise SkillConfigurationError("Skill references 必须是普通目录。")
    resources: list[str] = []
    for path in sorted(references.rglob("*")):
        if path.is_symlink():
            raise SkillConfigurationError(f"Skill 参考资料不能使用符号链接：{path}")
        if not path.is_file():
            continue
        if not path.resolve().is_relative_to(references.resolve()):
            raise SkillConfigurationError(f"Skill 参考资料越过目录边界：{path}")
        if path.stat().st_size > MAX_SKILL_RESOURCE_BYTES:
            raise SkillConfigurationError(f"Skill 参考资料超过大小限制：{path.name}")
        try:
            path.read_text("utf-8")
        except UnicodeDecodeError as exc:
            raise SkillConfigurationError(
                f"Skill 参考资料必须是 UTF-8 文本：{path.name}"
            ) from exc
        resources.append(path.relative_to(directory).as_posix())
    return tuple(resources)


def _validate_unique_aliases(skills: Mapping[str, SkillDefinition]) -> None:
    owners = {name.casefold(): name for name in skills}
    for skill in skills.values():
        for alias in skill.aliases:
            normalized = alias.casefold()
            owner = owners.get(normalized)
            if owner is not None and owner != skill.name:
                raise SkillConfigurationError(
                    f"Skill 别名重复：{alias} 同时属于 {owner} 和 {skill.name}"
                )
            owners[normalized] = skill.name


def _required_string(value: Any, name: str, limit: int) -> str:
    if not isinstance(value, str) or not value.strip() or len(value.strip()) > limit:
        raise SkillConfigurationError(f"{name} 必须是 1-{limit} 个字符。")
    return value.strip()


def _string_list(value: Any, name: str, *, limit: int, item_limit: int) -> tuple[str, ...]:
    if not isinstance(value, list) or len(value) > limit:
        raise SkillConfigurationError(f"{name} 必须是最多 {limit} 项的数组。")
    items = []
    for item in value:
        if not isinstance(item, str) or not item.strip() or len(item.strip()) > item_limit:
            raise SkillConfigurationError(f"{name} 包含无效字符串。")
        items.append(item.strip())
    if len(items) != len(set(item.casefold() for item in items)):
        raise SkillConfigurationError(f"{name} 不能包含重复项。")
    return tuple(items)


def _tool_list(
    value: Any,
    name: str,
    *,
    allow_empty: bool = False,
) -> tuple[str, ...]:
    items = _string_list(value, name, limit=30, item_limit=80)
    if not items and not allow_empty:
        raise SkillConfigurationError(f"{name} 不能为空。")
    if any(not TOOL_NAME_PATTERN.fullmatch(item) for item in items):
        raise SkillConfigurationError(f"{name} 包含非法工具名。")
    return items


def _boolean(value: Any, name: str) -> bool:
    if not isinstance(value, bool):
        raise SkillConfigurationError(f"{name} 必须是布尔值。")
    return value


__all__ = [
    "MAX_SKILL_STEPS",
    "READ_SKILL_RESOURCE_TOOL",
    "READ_SKILL_RESOURCE_TOOL_NAME",
    "SkillCatalog",
    "SkillConfigurationError",
    "SkillDefinition",
]
