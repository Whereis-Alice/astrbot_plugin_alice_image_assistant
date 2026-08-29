"""Pixiv 搜索查询计划（纯逻辑模块）。

本模块只做「计划」，不做任何网络调用，因此可以完整单测。

背景：原实现所有搜索入口都硬编码 search_target="partial_match_for_tags"，
既没有 exact_match_for_tags 优先，也没有 title_and_caption 兜底，
更没有把中文关键词通过 search_autocomplete 归一化成日文 tag，
导致中文 query 的命中率与精准度都很差。

本模块提供：
- extract_autocomplete_tags: 从 search_autocomplete 结果里提取候选 tag 名
- build_search_plan: 生成多级搜索计划（exact -> partial -> title_and_caption）
- 结果层工具: dedupe_illusts / sort_illusts_by_bookmarks / has_enough_results
- detect_popular_desc_degraded: 检测 popular_desc 被 Pixiv 静默降级为时间序
- describe_api_error / extract_illusts: API 返回对象的安全读取与中文化
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from typing import Any

# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------

SEARCH_TARGET_EXACT = "exact_match_for_tags"
SEARCH_TARGET_PARTIAL = "partial_match_for_tags"
SEARCH_TARGET_TITLE_CAPTION = "title_and_caption"

SORT_POPULAR_DESC = "popular_desc"
SORT_DATE_DESC = "date_desc"

DEFAULT_FILTER = "for_ios"

#: 计划中最多包含多少个搜索步骤（避免中文 query 触发过多网络请求）
DEFAULT_MAX_STEPS = 4

#: autocomplete 最多采纳几个归一化候选 tag
DEFAULT_AUTOCOMPLETE_LIMIT = 3


# ---------------------------------------------------------------------------
# 数据结构
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SearchStep:
    """一次具体的 search_illust 调用计划。

    :param word: 实际传给 API 的搜索词
    :param search_target: partial/exact/title_and_caption 之一
    :param sort: popular_desc 或 date_desc
    :param reason: 中文说明，用于日志与用户提示
    :param is_fallback: 是否为「等价于旧行为」的兜底步骤
    """

    word: str
    search_target: str = SEARCH_TARGET_PARTIAL
    sort: str = SORT_POPULAR_DESC
    reason: str = ""
    is_fallback: bool = False

    def to_search_kwargs(
        self,
        filter_name: str = DEFAULT_FILTER,
        extra: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """转换为 search_illust 的关键字参数。

        :param filter_name: Pixiv filter 参数，默认 for_ios
        :param extra: 额外参数（如 duration/req_auth），会覆盖同名键
        """
        kwargs: dict[str, Any] = {
            "word": self.word,
            "search_target": self.search_target,
            "sort": self.sort,
            "filter": filter_name,
        }
        if extra:
            kwargs.update(
                {k: v for k, v in extra.items() if v is not None}
            )
        return kwargs


@dataclass
class QueryPlanOptions:
    """搜索计划的可调选项（全部来自配置 + 代码内默认值）。"""

    enable_autocomplete: bool = True
    enable_exact: bool = True
    enable_title_caption: bool = True
    prefer_popular: bool = True
    autocomplete_limit: int = DEFAULT_AUTOCOMPLETE_LIMIT
    max_steps: int = DEFAULT_MAX_STEPS
    min_results: int = 10
    extra_words: Sequence[str] = field(default_factory=tuple)


# ---------------------------------------------------------------------------
# 配置读取（不改 schema，全部「读配置 + 代码内默认值」）
# ---------------------------------------------------------------------------


def _config_get(config: Any, key: str, default: Any) -> Any:
    """兼容 dict / SimpleNamespace / PixivConfig 三种配置载体的安全读取。"""
    if config is None:
        return default
    getter = getattr(config, "get", None)
    if callable(getter):
        try:
            value = getter(key, None)
        except TypeError:
            value = None
        if value is not None:
            return value
    value = getattr(config, key, None)
    if value is not None:
        return value
    raw = getattr(config, "config", None)
    if isinstance(raw, dict):
        value = raw.get(key)
        if value is not None:
            return value
    return default


def _as_bool(value: Any, default: bool) -> bool:
    """把配置值宽松地转成 bool（兼容 "true"/"是"/1 等写法）。"""
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    if isinstance(value, (int, float)):
        return bool(value)
    text = str(value).strip().casefold()
    if text in {"true", "1", "yes", "on", "开启", "是"}:
        return True
    if text in {"false", "0", "no", "off", "关闭", "否"}:
        return False
    return default


def _as_int(value: Any, default: int, minimum: int = 0) -> int:
    """把配置值转成 int，失败或越界时回退默认值。"""
    try:
        result = int(value)
    except (TypeError, ValueError):
        return default
    return result if result >= minimum else default


def options_from_config(config: Any) -> QueryPlanOptions:
    """从插件配置构造 QueryPlanOptions；所有键缺失时使用代码内默认值。"""
    return QueryPlanOptions(
        enable_autocomplete=_as_bool(
            _config_get(config, "search_autocomplete_enabled", None), True
        ),
        enable_exact=_as_bool(
            _config_get(config, "search_exact_first_enabled", None), True
        ),
        enable_title_caption=_as_bool(
            _config_get(config, "search_title_caption_fallback", None), True
        ),
        prefer_popular=_as_bool(
            _config_get(config, "search_prefer_popular", None), True
        ),
        autocomplete_limit=_as_int(
            _config_get(config, "search_autocomplete_limit", None),
            DEFAULT_AUTOCOMPLETE_LIMIT,
            minimum=1,
        ),
        max_steps=_as_int(
            _config_get(config, "search_plan_max_steps", None),
            DEFAULT_MAX_STEPS,
            minimum=1,
        ),
        min_results=_as_int(
            _config_get(config, "search_plan_min_results", None), 10, minimum=1
        ),
    )


# ---------------------------------------------------------------------------
# autocomplete 归一化
# ---------------------------------------------------------------------------


def _tag_field(tag: Any, key: str) -> str:
    """读取 tag 对象/字典的某个字段并转成去空白字符串。"""
    value = tag.get(key) if isinstance(tag, dict) else getattr(tag, key, None)
    if value is None:
        return ""
    return str(value).strip()


def extract_autocomplete_tags(
    autocomplete_result: Any, limit: int = DEFAULT_AUTOCOMPLETE_LIMIT
) -> list[str]:
    """从 search_autocomplete(_v2) 结果提取候选 tag 名（去重保序）。

    优先取 name（日文原 tag，Pixiv 侧命中率最高），其次取 translated_name。
    任何结构异常都返回空列表，让调用方原样回退到旧行为。
    """
    if autocomplete_result is None or limit <= 0:
        return []

    if isinstance(autocomplete_result, dict):
        raw_tags = autocomplete_result.get("tags")
    else:
        raw_tags = getattr(autocomplete_result, "tags", None)
    if not isinstance(raw_tags, (list, tuple)):
        return []

    seen: set[str] = set()
    names: list[str] = []
    for tag in raw_tags:
        # 每个 tag 只贡献一个候选词：优先日文原名，name 缺失时才退到译名。
        name = _tag_field(tag, "name") or _tag_field(tag, "translated_name")
        if not name:
            continue
        marker = name.casefold()
        if marker in seen:
            continue
        seen.add(marker)
        names.append(name)
        if len(names) >= limit:
            break
    return names[:limit]


# ---------------------------------------------------------------------------
# 计划生成
# ---------------------------------------------------------------------------


def _split_query_words(query: str) -> list[str]:
    """把用户 query 按空白切成词（保序去重）。"""
    seen: set[str] = set()
    words: list[str] = []
    for word in str(query or "").split():
        marker = word.casefold()
        if marker in seen:
            continue
        seen.add(marker)
        words.append(word)
    return words


def build_search_plan(
    query: str,
    *,
    normalized_tags: Sequence[str] = (),
    sort: str = SORT_POPULAR_DESC,
    options: QueryPlanOptions | None = None,
) -> list[SearchStep]:
    """生成多级搜索计划。

    顺序（精准度由高到低）：

    1. autocomplete 归一化 tag 的 exact_match_for_tags —— 最精准
    2. 原 query 的 exact_match_for_tags
    3. autocomplete 归一化 tag 的 partial_match_for_tags
    4. 原 query 的 partial_match_for_tags（is_fallback=True，等价旧行为）
    5. 原 query 的 title_and_caption —— 兜底捞标题/简介命中

    第 4 步始终存在（除被 max_steps 截断），因此 autocomplete 失败时
    调用方拿到的计划与旧行为完全一致。
    """
    opts = options or QueryPlanOptions()
    cleaned = str(query or "").strip()
    if not cleaned:
        return []

    plan_sort = sort or (SORT_POPULAR_DESC if opts.prefer_popular else SORT_DATE_DESC)

    # 归一化候选（排除与原 query 完全相同的项，避免重复请求）
    tags: list[str] = []
    seen = {cleaned.casefold()}
    for tag in normalized_tags or ():
        text = str(tag or "").strip()
        if not text or text.casefold() in seen:
            continue
        seen.add(text.casefold())
        tags.append(text)
    tags = tags[: max(opts.autocomplete_limit, 0)] if opts.enable_autocomplete else []

    normalized_word = " ".join(tags) if tags else ""
    steps: list[SearchStep] = []

    if normalized_word and opts.enable_exact:
        steps.append(
            SearchStep(
                word=normalized_word,
                search_target=SEARCH_TARGET_EXACT,
                sort=plan_sort,
                reason="使用 autocomplete 归一化后的官方 tag 做精确匹配",
            )
        )
    if opts.enable_exact:
        steps.append(
            SearchStep(
                word=cleaned,
                search_target=SEARCH_TARGET_EXACT,
                sort=plan_sort,
                reason="原关键词精确匹配 tag",
            )
        )
    if normalized_word:
        steps.append(
            SearchStep(
                word=normalized_word,
                search_target=SEARCH_TARGET_PARTIAL,
                sort=plan_sort,
                reason="归一化 tag 的部分匹配",
            )
        )
    steps.append(
        SearchStep(
            word=cleaned,
            search_target=SEARCH_TARGET_PARTIAL,
            sort=plan_sort,
            reason="原关键词部分匹配（与旧版行为一致的兜底）",
            is_fallback=True,
        )
    )
    if opts.enable_title_caption:
        steps.append(
            SearchStep(
                word=cleaned,
                search_target=SEARCH_TARGET_TITLE_CAPTION,
                sort=plan_sort,
                reason="标题与简介兜底搜索",
                is_fallback=True,
            )
        )

    deduped: list[SearchStep] = []
    seen_steps: set[tuple[str, str, str]] = set()
    for step in steps:
        key = (step.word.casefold(), step.search_target, step.sort)
        if key in seen_steps:
            continue
        seen_steps.add(key)
        deduped.append(step)

    limit = max(opts.max_steps, 1)
    if len(deduped) <= limit:
        return deduped

    # 截断时必须保住 is_fallback 步骤，否则 autocomplete 失效场景会退化
    head = deduped[: limit - 1]
    fallback = next((s for s in deduped if s.is_fallback), deduped[-1])
    if fallback in head:
        return deduped[:limit]
    return [*head, fallback]


def has_enough_results(count: int, min_results: int) -> bool:
    """判断当前已收集的结果数是否足够，用于提前终止后续搜索步骤。"""
    try:
        return int(count) >= int(min_results)
    except (TypeError, ValueError):
        return False


# ---------------------------------------------------------------------------
# 结果层工具
# ---------------------------------------------------------------------------


def _illust_id(illust: Any) -> Any:
    """安全读取作品 id（dict / 对象都支持）。"""
    if isinstance(illust, dict):
        return illust.get("id")
    return getattr(illust, "id", None)


def _illust_bookmarks(illust: Any) -> int:
    """安全读取收藏数，缺失按 0 处理。"""
    if isinstance(illust, dict):
        value = illust.get("total_bookmarks")
    else:
        value = getattr(illust, "total_bookmarks", None)
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def dedupe_illusts(illusts: Iterable[Any]) -> list[Any]:
    """按作品 id 去重并保持原顺序；id 缺失的项按对象身份保留。"""
    seen: set[Any] = set()
    result: list[Any] = []
    for illust in illusts or ():
        marker = _illust_id(illust)
        if marker is None:
            marker = id(illust)
        if marker in seen:
            continue
        seen.add(marker)
        result.append(illust)
    return result


def sort_illusts_by_bookmarks(illusts: Iterable[Any]) -> list[Any]:
    """按收藏数降序排序（稳定排序，收藏数相同保持原顺序）。"""
    return sorted(illusts or (), key=_illust_bookmarks, reverse=True)


def detect_popular_desc_degraded(illusts: Sequence[Any]) -> bool:
    """检测 popular_desc 是否被 Pixiv 对非 Premium 账号静默降级为时间序。

    判据：收藏数序列不是单调不增（说明没按热度排），
    同时作品 id 严格递减（说明实际按投稿时间倒序返回）。
    样本不足 3 条时一律返回 False，避免误判。
    """
    items = list(illusts or ())
    if len(items) < 3:
        return False

    bookmarks = [_illust_bookmarks(i) for i in items]
    ids = [_illust_id(i) for i in items]
    if any(i is None for i in ids):
        return False
    try:
        int_ids = [int(i) for i in ids]
    except (TypeError, ValueError):
        return False

    bookmarks_sorted = all(
        bookmarks[i] >= bookmarks[i + 1] for i in range(len(bookmarks) - 1)
    )
    ids_strictly_desc = all(
        int_ids[i] > int_ids[i + 1] for i in range(len(int_ids) - 1)
    )
    return (not bookmarks_sorted) and ids_strictly_desc


# ---------------------------------------------------------------------------
# API 返回对象的安全读取
# ---------------------------------------------------------------------------

#: Pixiv app-api 常见错误文案 -> 中文提示
_ERROR_HINTS: tuple[tuple[str, str], ...] = (
    ("rate limit", "请求过于频繁，已被 Pixiv 限流，请稍后再试。"),
    ("invalid_grant", "Pixiv 凭据已失效，请重新获取 refresh_token。"),
    ("oauth", "Pixiv 认证失败，请检查配置中的凭据信息。"),
    ("not found", "Pixiv 未找到对应内容。"),
    ("timeout", "连接 Pixiv 超时，请检查网络或代理设置。"),
)


def describe_api_error(result: Any) -> str | None:
    """把 API 返回的 error 对象转成可读中文提示；无错误时返回 None。

    只回显归纳后的中文文案，不再把原始异常/英文报文直接抛给用户。
    """
    if result is None:
        return "Pixiv API 未返回任何内容，请稍后再试。"

    error = result.get("error") if isinstance(result, dict) else getattr(result, "error", None)
    if not error:
        return None

    if isinstance(error, dict):
        parts = [
            str(error.get(key, "")).strip()
            for key in ("user_message", "message", "reason")
        ]
    else:
        parts = [
            str(getattr(error, key, "") or "").strip()
            for key in ("user_message", "message", "reason")
        ]
    detail = next((p for p in parts if p), "")
    lowered = detail.casefold()
    for marker, hint in _ERROR_HINTS:
        if marker in lowered:
            return hint
    if detail:
        return f"Pixiv API 返回错误：{detail}"
    return "Pixiv API 返回了未知错误，请稍后再试。"


def extract_illusts(result: Any) -> list[Any]:
    """安全提取 API 结果里的 illusts 列表；结构异常时返回空列表。"""
    if result is None:
        return []
    if isinstance(result, dict):
        illusts = result.get("illusts")
    else:
        illusts = getattr(result, "illusts", None)
    if not illusts:
        return []
    if isinstance(illusts, (list, tuple)):
        return list(illusts)
    return []


def extract_next_url(result: Any) -> str | None:
    """安全提取 next_url。"""
    if result is None:
        return None
    if isinstance(result, dict):
        next_url = result.get("next_url")
    else:
        next_url = getattr(result, "next_url", None)
    return str(next_url) if next_url else None
