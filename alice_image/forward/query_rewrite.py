"""查询改写 / 多语言扩展（D 项）。

用户的自然语言描述（"帮我找一张可爱的初音未来穿白色连衣裙的图"）直接丢给
搜索引擎会严重拉低召回质量：Pixiv 的标签体系几乎全是日文，中文关键词几乎搜不到东西；
SerpApi / 搜图神器 也会被"帮我找一张""可爱的"这类无检索价值的修饰词干扰。
先把描述改写成面向图片搜索引擎的检索式，候选池的质量就已经提升一档，
后续视觉复核才有好料可挑。

注意：改写结果只用于「检索」，判定图片是否匹配永远使用用户原始描述，
否则会把改写引入的偏差当成真值，反而降低准确率。
"""

from __future__ import annotations

from collections import OrderedDict
from dataclasses import dataclass, field

from astrbot.api import logger

from .vlm_json import parse_json_payload

# 默认内存缓存条数：同一会话里用户经常重复同一描述，缓存可省掉重复 LLM 调用。
DEFAULT_CACHE_SIZE = 128
_FENCE = "\u0060\u0060\u0060"


@dataclass(slots=True)
class RewrittenQuery:
    """一次查询改写的结果；任何字段缺失都会回退到 original。"""

    original: str = ""
    zh: str = ""
    en: str = ""
    ja_tags: list[str] = field(default_factory=list)
    negative: list[str] = field(default_factory=list)
    rewritten: bool = False
    error: str = ""

    def for_source(self, source: str) -> str:
        """按目标图源产出实际检索词；任何缺失一律回退原查询。"""
        name = str(source or "").strip().lower()
        if name == "pixiv":
            # Pixiv 标签以日文为主，日文 tag 串联的召回率远高于中文描述。
            if self.ja_tags:
                return " ".join(self.ja_tags)
            return self.original
        if name == "serpapi":
            # Google 图片对英文关键词的索引更全，其次才用精炼中文。
            return self.en or self.zh or self.original
        if name == "soutu":
            # 搜图神器是中文图源，精炼中文关键词最合适。
            return self.zh or self.original
        return self.zh or self.original

    def to_json_dict(self) -> dict[str, object]:
        """透出给 ForwardOutcome / WebUI 的可序列化结构。"""
        return {
            "original": self.original,
            "zh": self.zh,
            "en": self.en,
            "ja_tags": list(self.ja_tags),
            "negative": list(self.negative),
            "rewritten": self.rewritten,
            "error": self.error,
        }


def fallback_query(query: str, error: str = "") -> RewrittenQuery:
    """构造「不改写」的结果对象，保证改写链路失败时找图流程照常进行。"""
    text = str(query or "")
    return RewrittenQuery(original=text, zh=text, en="", rewritten=False, error=error)


def build_rewrite_prompt(query: str, description: str = "") -> str:
    """构造改写 prompt；要求同时给出中文 / 英文 / 日文 tag 三种检索变体。"""
    safe_query = str(query or "")[:300].replace(_FENCE, "")
    safe_desc = str(description or "")[:300].replace(_FENCE, "")
    extra = ""
    if safe_desc and safe_desc != safe_query:
        extra = "补充描述：「" + safe_desc + "」\n"
    return (
        "你是图片搜索关键词专家。请把用户的自然语言找图需求改写成面向图片搜索引擎的检索式。\n\n"
        "用户需求：「" + safe_query + "」\n" + extra + "\n"
        "【改写要求】\n"
        "1. 去掉「帮我找一张」「来一张」「可爱的」「好看的」这类没有检索价值的口语与主观修饰。\n"
        "2. 保留所有具体可见要素：角色名、作品名、发色、服饰、动作、场景、画面类型。\n"
        "3. zh：3~8 个中文关键词，空格分隔，用于中文图源。\n"
        "4. en：3~8 个英文关键词，空格分隔，用于 Google 图片。\n"
        "5. ja_tags：面向 Pixiv 的日文标签数组。角色名要用日文原名（例如 初音未来 -> 初音ミク），"
        "服饰与场景用常见日文标签（例如 白色连衣裙 -> 白ワンピース）。最多 6 个，无法确定就少给。\n"
        "6. negative：需要排除的干扰词数组，没有就给空数组。\n"
        "7. 不要臆造用户没提到的角色、作品或属性。\n\n"
        "【输出格式】只输出一个 JSON 对象，不要 markdown 代码块，不要额外解释：\n"
        '{"zh": "初音未来 白色连衣裙 插画", "en": "hatsune miku white dress illustration",'
        ' "ja_tags": ["初音ミク", "白ワンピース"], "negative": ["cosplay"]}'
    )


def _coerce_keywords(value: object, limit: int = 8) -> list[str]:
    """把 tag / negative 字段统一成去重后的字符串列表。"""
    if value is None:
        return []
    if isinstance(value, str):
        items = list(value.replace("，", ",").split(","))
    elif isinstance(value, (list, tuple)):
        items = [str(item) for item in value]
    else:
        return []
    cleaned: list[str] = []
    for item in items:
        text = str(item).strip()
        if text and text not in cleaned:
            cleaned.append(text)
    return cleaned[:limit]


def _coerce_phrase(value: object, limit: int = 120) -> str:
    """把 zh / en 字段规整成单行关键词串。"""
    if value is None:
        return ""
    if isinstance(value, (list, tuple)):
        value = " ".join(str(item) for item in value)
    return " ".join(str(value).split())[:limit]


def parse_rewrite_payload(text: str, query: str) -> RewrittenQuery | None:
    """解析改写响应；完全无有效字段时返回 None 以触发回退。"""
    data = parse_json_payload(text)
    if data is None:
        return None
    zh = _coerce_phrase(data.get("zh"))
    en = _coerce_phrase(data.get("en"))
    ja_tags = _coerce_keywords(data.get("ja_tags"), limit=6)
    negative = _coerce_keywords(data.get("negative"), limit=8)
    if not zh and not en and not ja_tags:
        return None
    return RewrittenQuery(
        original=str(query or ""),
        zh=zh or str(query or ""),
        en=en,
        ja_tags=ja_tags,
        negative=negative,
        rewritten=True,
    )


class QueryRewriter:
    """带 LRU 缓存的查询改写器；任何失败都静默回退到原查询。"""

    def __init__(self, config: dict[str, object] | None = None) -> None:
        cfg = config if isinstance(config, dict) else {}
        self.enabled = bool(cfg.get("enabled", True))
        self.provider_id = str(cfg.get("provider_id") or "").strip()
        self.cache_size = self._bounded_int(cfg.get("cache_size", DEFAULT_CACHE_SIZE))
        self._cache: OrderedDict[str, RewrittenQuery] = OrderedDict()

    @staticmethod
    def _bounded_int(value: object, default: int = DEFAULT_CACHE_SIZE) -> int:
        try:
            parsed = int(value)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            parsed = default
        return max(0, min(1024, parsed))

    def _cache_key(self, query: str, description: str) -> str:
        return query + "\u0001" + description

    def _cache_get(self, key: str) -> RewrittenQuery | None:
        if self.cache_size <= 0:
            return None
        hit = self._cache.get(key)
        if hit is not None:
            self._cache.move_to_end(key)
        return hit

    def _cache_put(self, key: str, value: RewrittenQuery) -> None:
        # 只缓存成功结果：失败结果缓存下来会让一次偶发抖动长期拖累检索质量。
        if self.cache_size <= 0 or not value.rewritten:
            return
        self._cache[key] = value
        self._cache.move_to_end(key)
        while len(self._cache) > self.cache_size:
            self._cache.popitem(last=False)

    async def rewrite(
        self,
        provider: object,
        query: str,
        *,
        description: str = "",
    ) -> RewrittenQuery:
        """把查询改写成多语言检索式；不可用时返回 rewritten=False 的原查询。"""
        text = str(query or "").strip()
        if not text:
            return fallback_query(text, "查询为空，跳过改写。")
        if not self.enabled:
            return fallback_query(text)
        if provider is None:
            logger.debug("[AliceImageQueryRewrite] 无可用改写模型，按原查询检索。")
            return fallback_query(text, "无可用改写模型。")

        desc = str(description or "").strip()
        key = self._cache_key(text, desc)
        cached = self._cache_get(key)
        if cached is not None:
            return cached

        prompt = build_rewrite_prompt(text, desc)
        try:
            response = await provider.text_chat(prompt=prompt)
            raw = str(getattr(response, "completion_text", "") or "").strip()
            if not raw and getattr(response, "result_chain", None) is not None:
                raw = str(response.result_chain.get_plain_text() or "").strip()
            parsed = parse_rewrite_payload(raw, text)
        except Exception as exc:
            logger.warning("[AliceImageQueryRewrite] 查询改写失败，按原查询检索：%s", exc)
            return fallback_query(text, str(exc))

        if parsed is None:
            logger.warning(
                "[AliceImageQueryRewrite] 改写响应无法解析，按原查询检索：%s",
                raw[:200] if raw else "(空响应)",
            )
            return fallback_query(text, "改写响应无法解析。")

        self._cache_put(key, parsed)
        logger.info(
            "[AliceImageQueryRewrite] 查询改写完成：zh=%s / en=%s / ja_tags=%s",
            parsed.zh,
            parsed.en,
            parsed.ja_tags,
        )
        return parsed
