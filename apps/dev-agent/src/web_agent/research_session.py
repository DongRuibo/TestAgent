"""每轮研究的固定取数上限、请求指纹；成功缓存与未知状态均持久化。"""

import hashlib
import json
import re

from tools.social_research import _keyword, _limit, _xiaohongshu_url
from tools.research_images import IMAGE_TOOL_NAME, VisualReceipt
from urllib.parse import urlsplit


CAPS = {"social_search_posts": 2, "social_get_post_detail": 2,
    "social_get_comments": 2, "social_get_creator_posts": 0, "social_get_creator_stats": 0, IMAGE_TOOL_NAME: 2}
LABELS = {"social_search_posts": "搜索", "social_get_post_detail": "读取笔记",
    "social_get_comments": "读取评论", IMAGE_TOOL_NAME: "分析图片"}


def research_limits(notes=""):
    limits = {**CAPS, "comment_limit": 10}
    number = r"([0-9]+|零|一|二|两|三|四|五|六|七|八|九|十)"
    for key, label in (("social_search_posts", "(?:搜索|检索|关键词)"), ("social_get_post_detail", "(?:详情|核实)"),
                       ("social_get_comments", "评论"), ("comment_limit", "每篇"), (IMAGE_TOOL_NAME, "图片分析")):
        patterns = (rf"{label}(?:最多|不超过|上限|限|只|仅|读取|取|\s)*{number}\s*(?:次|篇|条|个)",
            rf"{number}\s*(?:次|篇|个)\s*(?:关键词)?{label}")
        for pattern in patterns:
            for match in re.finditer(pattern, notes):
                value = match[1]
                amount = int(value) if value.isdigit() else {"零": 0, "一": 1, "二": 2, "两": 2, "三": 3,
                    "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9, "十": 10}[value]
                limits[key] = min(limits[key], amount)
    if re.search(r"(?:不|无需|不用)(?:分析图片|做图片分析|看图)", notes):
        limits[IMAGE_TOOL_NAME] = 0
    return limits


def normalized_request(name, args, kwargs):
    keys = ["url", "image_indexes"] if name == IMAGE_TOOL_NAME else ["keyword", "limit"] if name == "social_search_posts" else ["url", "limit"]
    if len(args) > len(keys) or set(keys[:len(args)]) & kwargs.keys():
        raise ValueError("取数参数重复或过多。")
    params = {**dict(zip(keys, args)), **kwargs}
    allowed = keys[:1] if name in {"social_get_post_detail", "social_get_creator_stats"} else keys
    if params.keys() - set(allowed):
        raise ValueError("取数参数不受支持。")
    if name == "social_search_posts":
        params["keyword"] = _keyword(params.get("keyword"))
    else:
        params["url"] = _xiaohongshu_url(params.get("url"))
    if name == IMAGE_TOOL_NAME:
        values = params.get("image_indexes")
        if not isinstance(values, list) or not 1 <= len(values) <= 3 or any(type(i) is not int or i < 1 for i in values) or len(set(values)) != len(values):
            raise ValueError("图片序号统一从1开始，不能使用0；请选择详情image_options中的1至3个不同序号。")
        params["image_indexes"] = sorted(values)
        params["url"] = params["url"].split("?", 1)[0].split("#", 1)[0]
    if "limit" in allowed:
        params["limit"] = _limit(params.get("limit", 10))
    fingerprint = hashlib.sha256(json.dumps([name, params], sort_keys=True).encode()).hexdigest()
    return params, fingerprint


def post_key(url):
    return urlsplit(url).path.rstrip("/")


def source_receipts(state):
    """仅从宿主实际成功且非空的取数回执建立来源等级，不接受模型自报已核实。"""
    sources = {}
    for key, call in state.get("research_calls", {}).items():
        result = call.get("result") or {}
        if call.get("status") != "success" or not result.get("success"):
            continue
        name = call["tool"]
        items = result.get("items", [])
        if name == "social_search_posts":
            urls = [item.get("url", "") for item in items]
            level = "search_only"
        elif name == "social_get_post_detail":
            urls = [item.get("url") or call["params"]["url"] for item in items if item.get("content", "").strip()]
            level = "body_read"
        elif name == "social_get_comments" and any(item.get("text", "").strip() for item in items):
            urls = [call["params"]["url"]]
            level = "comments_read"
        elif name == IMAGE_TOOL_NAME and result.get("analysis"):
            urls = [call["params"]["url"]]
            level = "visual_read"
        else:
            continue
        for url in urls:
            if not url:
                continue
            post = post_key(url)
            previous = sources.get(post)
            rank = {"search_only": 0, "comments_read": 1, "visual_read": 2, "body_read": 3}
            sources[post] = {"verification": previous["verification"] if previous and rank[previous["verification"]] > rank[level] else level,
                "receipt_ids": [*(previous or {}).get("receipt_ids", []), key], "url": (previous or {}).get("url", url),
                "collected_at": (previous or {}).get("collected_at") or result.get("collected_at") or result.get("analyzed_at")}
    return sources


def visual_receipts(state):
    receipts = []
    for key, call in state.get("research_calls", {}).items():
        if call["tool"] != IMAGE_TOOL_NAME:
            continue
        result = call.get("result") or {}
        receipts.append(VisualReceipt(analysis_id="visual-" + key[:12], post_url=call["params"]["url"],
            status="success" if result.get("success") else "failed", image_indexes=call["params"]["image_indexes"],
            analyzed_at=result.get("analyzed_at", ""), analysis=result.get("analysis"),
            available_image_count=result.get("available_image_count"),
            message=result.get("message", "图片分析中断，未获得可用观察。" )).model_dump())
    return receipts


def stopped(code, message):
    return {"success": False, "error_type": code, "retryable": False, "message": message}
