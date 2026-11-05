"""小红书公开数据研究的原子只读 Tools。"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timezone
from typing import Any, Protocol
from urllib.parse import urlsplit

from tools.guaikei import GuaikeiCli, GuaikeiError


SOCIAL_SEARCH_POSTS_TOOL_NAME = "social_search_posts"
SOCIAL_GET_POST_DETAIL_TOOL_NAME = "social_get_post_detail"
SOCIAL_GET_COMMENTS_TOOL_NAME = "social_get_comments"
SOCIAL_GET_CREATOR_POSTS_TOOL_NAME = "social_get_creator_posts"
SOCIAL_GET_CREATOR_STATS_TOOL_NAME = "social_get_creator_stats"
SOCIAL_RESEARCH_TOOL_NAMES = (
    SOCIAL_SEARCH_POSTS_TOOL_NAME,
    SOCIAL_GET_POST_DETAIL_TOOL_NAME,
    SOCIAL_GET_COMMENTS_TOOL_NAME,
    SOCIAL_GET_CREATOR_POSTS_TOOL_NAME,
    SOCIAL_GET_CREATOR_STATS_TOOL_NAME,
)


class SocialResearchProvider(Protocol):
    def search(self, keyword: str) -> dict[str, Any]: ...

    def detail(self, url: str) -> dict[str, Any]: ...

    def comments(self, url: str, limit: int) -> dict[str, Any]: ...

    def creator_posts(self, url: str, limit: int) -> dict[str, Any]: ...


def _tool(
    name: str,
    description: str,
    properties: dict[str, Any],
    required: list[str],
) -> dict[str, Any]:
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": description,
            "parameters": {
                "type": "object",
                "properties": properties,
                "required": required,
                "additionalProperties": False,
            },
        },
    }


_URL_PROPERTY = {
    "type": "string",
    "maxLength": 2000,
    "description": "公开小红书笔记或博主主页的 HTTPS URL。",
}
_LIMIT_PROPERTY = {
    "type": "integer",
    "minimum": 1,
    "maximum": 20,
    "description": "最多返回 20 条。",
}

SOCIAL_SEARCH_POSTS_TOOL = _tool(
    SOCIAL_SEARCH_POSTS_TOOL_NAME,
    "按关键词搜索小红书公开笔记，返回统一字段；只读，不登录、不发布。",
    {
        "keyword": {
            "type": "string",
            "maxLength": 80,
            "description": "明确的小红书搜索关键词。",
        },
        "limit": _LIMIT_PROPERTY,
    },
    ["keyword"],
)
SOCIAL_GET_POST_DETAIL_TOOL = _tool(
    SOCIAL_GET_POST_DETAIL_TOOL_NAME,
    "读取一篇公开小红书笔记详情，用于核实搜索候选。",
    {"url": _URL_PROPERTY},
    ["url"],
)
SOCIAL_GET_COMMENTS_TOOL = _tool(
    SOCIAL_GET_COMMENTS_TOOL_NAME,
    "读取公开小红书笔记评论样本，用于识别用户问题与需求信号。",
    {"url": _URL_PROPERTY, "limit": _LIMIT_PROPERTY},
    ["url"],
)
SOCIAL_GET_CREATOR_POSTS_TOOL = _tool(
    SOCIAL_GET_CREATOR_POSTS_TOOL_NAME,
    "读取公开小红书博主主页的作品样本，用于竞品内容研究。",
    {"url": _URL_PROPERTY, "limit": _LIMIT_PROPERTY},
    ["url"],
)
SOCIAL_GET_CREATOR_STATS_TOOL = _tool(
    SOCIAL_GET_CREATOR_STATS_TOOL_NAME,
    "读取小红书博主账号统计快照：粉丝、关注、获赞和收藏；需要公开博主主页 URL。",
    {"url": _URL_PROPERTY},
    ["url"],
)
SOCIAL_RESEARCH_TOOL_DEFINITIONS = (
    SOCIAL_SEARCH_POSTS_TOOL,
    SOCIAL_GET_POST_DETAIL_TOOL,
    SOCIAL_GET_COMMENTS_TOOL,
    SOCIAL_GET_CREATOR_POSTS_TOOL,
    SOCIAL_GET_CREATOR_STATS_TOOL,
)


class SocialResearchTools:
    def __init__(
        self,
        provider: SocialResearchProvider | None = None,
        *,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.provider = provider or GuaikeiCli.from_env()
        self.clock = clock or (lambda: datetime.now(timezone.utc))

    def search_posts(self, keyword: str, limit: int = 10) -> dict[str, Any]:
        try:
            keyword = _keyword(keyword)
            limit = _limit(limit)
            raw = self.provider.search(keyword)
            items = [_normalize_post(item) for item in _items(raw, "posts")]
            items = [item for item in items if item["title"] or item["url"]]
            return _success(
                raw,
                items[:limit],
                collected_at=self._collected_at(),
                query=keyword,
            )
        except (GuaikeiError, ValueError) as exc:
            return _error(exc)

    def get_post_detail(self, url: str) -> dict[str, Any]:
        try:
            url = _xiaohongshu_url(url)
            raw = self.provider.detail(url)
            result = _results(raw)
            if isinstance(result, list):
                result = result[0] if result else {}
            if not isinstance(result, dict):
                raise ValueError("笔记详情结果格式无效。")
            item = _normalize_post(result)
            if not item["url"]:
                item["url"] = url
            return _success(raw, [item], collected_at=self._collected_at())
        except (GuaikeiError, ValueError) as exc:
            return _error(exc)

    def get_comments(self, url: str, limit: int = 10) -> dict[str, Any]:
        try:
            url = _xiaohongshu_url(url)
            limit = _limit(limit)
            raw = self.provider.comments(url, limit)
            items = [_normalize_comment(item) for item in _items(raw, "comments")]
            items = [item for item in items if item["text"]]
            return _success(
                raw,
                items[:limit],
                collected_at=self._collected_at(),
                source_url=url,
            )
        except (GuaikeiError, ValueError) as exc:
            return _error(exc)

    def get_creator_posts(self, url: str, limit: int = 10) -> dict[str, Any]:
        try:
            url = _xiaohongshu_url(url)
            limit = _limit(limit)
            raw = self.provider.creator_posts(url, limit)
            items = [_normalize_post(item) for item in _items(raw, "posts")]
            items = [item for item in items if item["title"] or item["url"]]
            return _success(
                raw,
                items[:limit],
                collected_at=self._collected_at(),
                source_url=url,
            )
        except (GuaikeiError, ValueError) as exc:
            return _error(exc)

    def get_creator_stats(self, url: str) -> dict[str, Any]:
        try:
            url = _xiaohongshu_url(url)
            parts = urlsplit(url).path.strip("/").split("/")
            if len(parts) != 3 or parts[:2] != ["user", "profile"]:
                raise ValueError("账号统计需要公开小红书博主主页 URL。")
            raw = self.provider.creator_posts(url, 0)
            result = _results(raw)
            user = _mapping(result, "user") if isinstance(result, dict) else {}
            if not user:
                raise GuaikeiError(
                    "INVALID_PROVIDER_RESPONSE", "账号统计结果缺少博主信息。"
                )
            item = {
                "platform": "xiaohongshu",
                "author_id": _text(user, "user_id", "id") or parts[2],
                "author_name": _text(user, "nickname", "name"),
                "description": _text(user, "desc", "description"),
                "following": _number(user, "following", "follows"),
                "followers": _number(user, "followers", "fans"),
                "likes": _number(user, "likes", "liked"),
                "favorites": _number(user, "favorites", "collected"),
                "interactions": _number(user, "interactions", "interaction"),
                "url": url,
            }
            return _success(
                raw, [item], collected_at=self._collected_at(), source_url=url
            )
        except (GuaikeiError, ValueError) as exc:
            return _error(exc)

    def handlers(self) -> dict[str, Callable[..., dict[str, Any]]]:
        return {
            SOCIAL_SEARCH_POSTS_TOOL_NAME: self.search_posts,
            SOCIAL_GET_POST_DETAIL_TOOL_NAME: self.get_post_detail,
            SOCIAL_GET_COMMENTS_TOOL_NAME: self.get_comments,
            SOCIAL_GET_CREATOR_POSTS_TOOL_NAME: self.get_creator_posts,
            SOCIAL_GET_CREATOR_STATS_TOOL_NAME: self.get_creator_stats,
        }

    def _collected_at(self) -> str:
        value = self.clock()
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc).isoformat(timespec="seconds")


def _keyword(value: str) -> str:
    if not isinstance(value, str):
        raise ValueError("keyword 必须是字符串。")
    normalized = " ".join(value.split())
    if not normalized or len(normalized) > 80:
        raise ValueError("keyword 必须是 1-80 个字符。")
    return normalized


def _limit(value: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 20:
        raise ValueError("limit 必须是 1-20 的整数。")
    return value


def _xiaohongshu_url(value: str) -> str:
    if not isinstance(value, str):
        raise ValueError("url 必须是字符串。")
    normalized = value.strip()
    parsed = urlsplit(normalized)
    hostname = (parsed.hostname or "").rstrip(".").casefold()
    if (
        parsed.scheme.casefold() != "https"
        or parsed.username is not None
        or parsed.password is not None
        or parsed.port is not None
        or not (
            hostname == "xiaohongshu.com"
            or hostname.endswith(".xiaohongshu.com")
        )
    ):
        raise ValueError("只允许公开小红书 HTTPS URL。")
    if len(normalized) > 2000:
        raise ValueError("url 不能超过 2000 个字符。")
    return normalized


def _results(raw: dict[str, Any]) -> Any:
    _validate_envelope(raw)
    return raw.get("results", raw.get("data", raw))


def _items(raw: dict[str, Any], preferred_key: str) -> list[dict[str, Any]]:
    result = _results(raw)
    if isinstance(result, list):
        values = result
    elif isinstance(result, dict):
        values = next(
            (
                result[key]
                for key in (preferred_key, "items", "list", "notes", "note", "data")
                if isinstance(result.get(key), list)
            ),
            [],
        )
    else:
        values = []
    return [item for item in values if isinstance(item, dict)]


def _validate_envelope(raw: dict[str, Any]) -> None:
    status = raw.get("status")
    error_code = raw.get("errcode", raw.get("error_code"))
    if status in {"error", "failed", "failure"} or error_code not in {
        None,
        0,
        "0",
        "OK",
    }:
        retryable = error_code in {423, 429, "423", "429"}
        raise GuaikeiError(
            "PROVIDER_ERROR",
            "GUAIKEI API 返回失败状态。",
            retryable=retryable,
            provider_code=error_code,
        )


def _normalize_post(item: dict[str, Any]) -> dict[str, Any]:
    author = _mapping(item, "author", "user", "creator")
    images = _media_urls(item, "image_urls", "image_list", "images")
    cover = next(iter(_media_urls(item, "cover_url", "cover")), "")
    return {
        "platform": "xiaohongshu",
        "post_id": _text(item, "post_id", "note_id", "id"),
        "title": _text(item, "title", "note_title", "display_title"),
        "content": _text(item, "content", "desc", "description", "summary"),
        "author_id": _text(author, "user_id", "author_id", "id")
        or _text(item, "author_id", "user_id"),
        "author_name": _text(author, "nickname", "name", "user_name")
        or _text(item, "author_name", "nickname", "user_name"),
        "published_at": _text(
            item, "published_at", "publish_time", "time", "create_time"
        ),
        "likes": _number(item, "likes", "liked_count", "like_count"),
        "comments": _number(item, "comments", "comment_count"),
        "favorites": _number(
            item,
            "favorites",
            "saves",
            "collected_count",
            "collect_count",
            "collected",
        ),
        "shares": _number(item, "shares", "share_count", "shared_count", "share"),
        "views": _number(item, "views", "view_count", "play_count", "view"),
        "url": _text(item, "url", "note_url", "share_url", "link"),
        "type": _text(item, "type"),
        "cover_url": cover,
        "image_urls": images,
        "image_options": [{"image_index": i, "label": "首图" if i == 1 else "配图"}
            for i in range(1, len(images or ([cover] if cover else [])) + 1)],
        "video_urls": _media_urls(item, "video_urls", "video_url", "video"),
        "video_duration": _number(item, "video_duration"),
        "tags": _labels(item, "tags"),
        "topics": _labels(item, "topics"),
    }


def _media_urls(item: dict[str, Any], *keys: str) -> list[str]:
    urls = []
    for key in keys:
        value = item.get(key)
        candidates = [value] if isinstance(value, str) else value
        if not isinstance(candidates, list):
            continue
        for url in candidates:
            if isinstance(url, str):
                url = url.strip()
                if url.startswith(("https://", "http://")) and url not in urls:
                    urls.append(url)
    return urls


def _labels(item: dict[str, Any], key: str) -> list[str]:
    values = item.get(key)
    if not isinstance(values, list):
        return []
    labels = []
    for value in values:
        name = _text(value, "name") if isinstance(value, dict) else ""
        if isinstance(value, str):
            name = value.strip()[:2000]
        if name:
            labels.append(name)
    return labels


def _normalize_comment(item: dict[str, Any]) -> dict[str, Any]:
    author = _mapping(item, "author", "user")
    return {
        "platform": "xiaohongshu",
        "comment_id": _text(item, "comment_id", "id"),
        "text": _text(item, "text", "content", "comment"),
        "author_id": _text(author, "user_id", "author_id", "id")
        or _text(item, "author_id", "user_id"),
        "author_name": _text(author, "nickname", "name")
        or _text(item, "author_name", "nickname"),
        "published_at": _text(item, "published_at", "create_time", "time"),
        "likes": _number(item, "likes", "liked_count", "like_count"),
    }


def _mapping(item: dict[str, Any], *keys: str) -> dict[str, Any]:
    for key in keys:
        value = item.get(key)
        if isinstance(value, dict):
            return value
    return {}


def _text(item: dict[str, Any], *keys: str) -> str:
    for key in keys:
        value = item.get(key)
        if isinstance(value, str):
            return value.strip()[:2000]
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            return str(value)
    return ""


def _number(item: dict[str, Any], *keys: str) -> int | float | None:
    for key in keys:
        value = item.get(key)
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            return value
        if isinstance(value, str):
            cleaned = value.strip().replace(",", "")
            try:
                return float(cleaned) if "." in cleaned else int(cleaned)
            except ValueError:
                continue
    return None


def _success(
    raw: dict[str, Any],
    items: list[dict[str, Any]],
    *,
    collected_at: str,
    **context: str,
) -> dict[str, Any]:
    request_id = raw.get("request_id")
    stamped_items = [dict(item, collected_at=collected_at) for item in items]
    return {
        "success": True,
        "platform": "xiaohongshu",
        "provider": "guaikei_cli",
        "request_id": request_id if isinstance(request_id, str) else "",
        "collected_at": collected_at,
        "items": stamped_items,
        "item_count": len(stamped_items),
        **context,
    }


def _error(exc: GuaikeiError | ValueError) -> dict[str, Any]:
    if isinstance(exc, GuaikeiError):
        error_type = exc.error_type
        retryable = exc.retryable
    else:
        error_type = "INVALID_ARGUMENTS"
        retryable = False
    result = {
        "success": False,
        "platform": "xiaohongshu",
        "provider": "guaikei_cli",
        "error_type": error_type,
        "message": str(exc),
        "retryable": retryable,
    }
    if isinstance(exc, GuaikeiError) and exc.provider_code is not None:
        result["provider_code"] = exc.provider_code
    return result


__all__ = [
    "SOCIAL_RESEARCH_TOOL_DEFINITIONS",
    "SOCIAL_RESEARCH_TOOL_NAMES",
    "SocialResearchProvider",
    "SocialResearchTools",
]
