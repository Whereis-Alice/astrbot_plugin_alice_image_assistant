"""搜图结果融合与排序.

把"URL 规范化 / 跨引擎去重 / 共识加成 / 稳定排序"这些纯逻辑独立出来，
一是让 service.py 只负责编排（可读性），二是这些纯函数可以直接单测。
"""

from __future__ import annotations

import urllib.parse
from dataclasses import dataclass, field, replace

from .constant import (
    DEFAULT_SOURCE_CONFIDENCE,
    DEFAULT_SOURCE_PRIORITY,
    SOURCE_CONFIDENCE,
    SOURCE_NAME_TO_KEY,
    SOURCE_PRIORITY,
)
from .models import SearchResultItem

# 常见跟踪参数：这些参数不影响页面内容，保留会让同一页面被判成不同结果
TRACKING_PARAM_PREFIXES: tuple[str, ...] = ("utm_", "ref_", "share_", "spm_")
TRACKING_PARAM_NAMES: frozenset[str] = frozenset(
    {
        "ref",
        "from",
        "spm",
        "fbclid",
        "gclid",
        "msclkid",
        "mkt_tok",
        "igshid",
        "share",
        "share_id",
        "share_source",
        "share_plate",
        "_hsenc",
        "_hsmi",
        "yclid",
    }
)

# 每多一个引擎命中同一 URL 的加成，以及加成上限
# 加成刻意做得很小：共识只用于"同分时抬到前面"，不能让弱引擎的共识压过 SauceNAO 高相似度
CONSENSUS_BONUS_PER_ENGINE = 0.05
MAX_CONSENSUS_BONUS = 0.15


def _is_tracking_param(name: str) -> bool:
    """判断查询参数是否为跟踪参数."""
    lowered = name.lower()
    if lowered in TRACKING_PARAM_NAMES:
        return True
    return lowered.startswith(TRACKING_PARAM_PREFIXES)


def normalize_url(url: str) -> str:
    """将 URL 规范化为跨引擎去重键.

    统一 scheme/host 大小写、去掉 www. 前缀与默认端口、去掉末尾斜杠、
    丢弃常见跟踪参数并对剩余参数排序、丢弃 fragment。

    Args:
        url: 原始 URL

    Returns:
        规范化后的 URL；无法解析时返回原串的小写形式（至少能自身去重）
    """
    if not url:
        return ""

    raw = url.strip()
    if not raw:
        return ""

    try:
        parsed = urllib.parse.urlsplit(raw)
    except ValueError:
        return raw.lower()

    if not parsed.netloc:
        # 相对路径 / 畸形链接无法规范化，退化处理而不是丢弃
        return raw.lower()

    scheme = (parsed.scheme or "https").lower()
    netloc = parsed.netloc.lower()
    if scheme == "https" and netloc.endswith(":443"):
        netloc = netloc[: -len(":443")]
    elif scheme == "http" and netloc.endswith(":80"):
        netloc = netloc[: -len(":80")]
    # www 前缀不影响页面内容，剥掉才能让两个引擎给出的同一页面命中同一个 key
    netloc = netloc.removeprefix("www.")

    path = parsed.path or "/"
    if len(path) > 1:
        path = path.rstrip("/") or "/"

    kept_pairs = [
        (key, value)
        for key, value in urllib.parse.parse_qsl(parsed.query, keep_blank_values=True)
        if not _is_tracking_param(key)
    ]
    query = urllib.parse.urlencode(sorted(kept_pairs))

    return urllib.parse.urlunsplit((scheme, netloc, path, query, ""))


def resolve_source_key(item: SearchResultItem) -> str:
    """获取结果项的归一化来源键.

    Args:
        item: 搜索结果项

    Returns:
        归一化来源键，未知来源返回空字符串
    """
    if item.source_key:
        return item.source_key.lower()
    # 旧结果只有展示名，按名称映射兜底，避免全部落到"未知来源"档
    return SOURCE_NAME_TO_KEY.get((item.source or "").strip().lower(), "")


def source_confidence(source_key: str) -> float:
    """获取来源可信度系数.

    Args:
        source_key: 归一化来源键

    Returns:
        可信度系数 (0.0..1.0)
    """
    key = (source_key or "").lower()
    if key in SOURCE_CONFIDENCE:
        return SOURCE_CONFIDENCE[key]
    # "ascii2d/xxx" 这类子类型未登记时，退回父类型系数
    parent = key.split("/", 1)[0]
    return SOURCE_CONFIDENCE.get(parent, DEFAULT_SOURCE_CONFIDENCE)


def source_priority(source_key: str) -> int:
    """获取来源优先级 (数值越小越优先).

    Args:
        source_key: 归一化来源键

    Returns:
        优先级数值
    """
    key = (source_key or "").lower()
    if key in SOURCE_PRIORITY:
        return SOURCE_PRIORITY[key]
    parent = key.split("/", 1)[0]
    return SOURCE_PRIORITY.get(parent, DEFAULT_SOURCE_PRIORITY)


def positional_score(index: int, total: int, source_key: str) -> float:
    """按结果位次生成归一化置信度.

    ascii2d / Google Lens 不返回相似度，只能用"排在越前越可信"近似，
    再乘来源可信度系数，使其与 SauceNAO 的真实相似度可比。

    Args:
        index: 结果在该引擎内的下标 (从 0 开始)
        total: 该引擎本次取用的结果总数
        source_key: 归一化来源键

    Returns:
        归一化分数 (0.0..1.0)
    """
    if total <= 0 or index < 0:
        return 0.0
    ratio = 1.0 - (index / total)
    ratio = max(0.0, min(1.0, ratio))
    return round(ratio * source_confidence(source_key), 6)


@dataclass
class _UrlGroup:
    """同一规范化 URL 的命中分组."""

    key: str
    first_index: int
    first_item: SearchResultItem
    best_item: SearchResultItem
    best_score: float | None = None
    engines: list[str] = field(default_factory=list)

    @property
    def best_priority(self) -> int:
        """分组代表项的来源优先级."""
        return source_priority(resolve_source_key(self.best_item))

    def sort_key(self) -> tuple[float, int, int]:
        """排序键：分数降序 -> 来源优先级 -> 原始位次."""
        # score 为 None 视为 -1，保证"无置信度结果"永远排在有分数的结果之后
        score = self.best_score if self.best_score is not None else -1.0
        return (-score, self.best_priority, self.first_index)


def _better_item(
    current: SearchResultItem, candidate: SearchResultItem, candidate_is_later: bool
) -> bool:
    """判断候选项是否比当前代表项更适合作为分组代表.

    Args:
        current: 当前代表项
        candidate: 候选项
        candidate_is_later: 候选项是否出现在当前代表项之后

    Returns:
        True 表示应该替换代表项
    """
    current_score = current.score if current.score is not None else -1.0
    candidate_score = candidate.score if candidate.score is not None else -1.0
    if candidate_score != current_score:
        return candidate_score > current_score
    current_priority = source_priority(resolve_source_key(current))
    candidate_priority = source_priority(resolve_source_key(candidate))
    if candidate_priority != current_priority:
        return candidate_priority < current_priority
    # 完全同分同优先级时保持先出现者，保证排序稳定
    return not candidate_is_later


def group_by_url(items: list[SearchResultItem]) -> list[_UrlGroup]:
    """按规范化 URL 分组，返回首次出现顺序的分组列表.

    Args:
        items: 待分组的结果项

    Returns:
        分组列表 (按首次出现顺序)
    """
    groups: dict[str, _UrlGroup] = {}
    order: list[str] = []

    for index, item in enumerate(items):
        normalized = normalize_url(item.url)
        # 空 URL 无法去重，用下标构造唯一键，避免把不同的坏数据合成一条
        key = normalized or f"__no_url__:{index}"

        group = groups.get(key)
        if group is None:
            group = _UrlGroup(
                key=key,
                first_index=index,
                first_item=item,
                best_item=item,
                best_score=item.score,
            )
            groups[key] = group
            order.append(key)
        else:
            if _better_item(group.best_item, item, candidate_is_later=True):
                group.best_item = item
            if item.score is not None and (
                group.best_score is None or item.score > group.best_score
            ):
                group.best_score = item.score
            # 代表项没有缩略图时，借用其它引擎的缩略图，避免去重后丢图
            if not group.best_item.thumbnail and item.thumbnail:
                group.best_item = replace(
                    group.best_item,
                    thumbnail=item.thumbnail,
                    thumbnail_bytes=item.thumbnail_bytes,
                )

        engine = (item.source or "").strip()
        if engine and engine not in group.engines:
            group.engines.append(engine)

    return [groups[key] for key in order]


def _consensus_bonus(engine_count: int) -> float:
    """按命中引擎数量计算共识加成."""
    if engine_count <= 1:
        return 0.0
    return min(MAX_CONSENSUS_BONUS, CONSENSUS_BONUS_PER_ENGINE * (engine_count - 1))


def _finalize(group: _UrlGroup) -> SearchResultItem:
    """把分组折叠回单个结果项，写入融合分数与 matched_by."""
    score = group.best_score
    if score is not None:
        score = round(min(1.0, score + _consensus_bonus(len(group.engines))), 6)
    return replace(group.best_item, score=score, matched_by=list(group.engines))


def dedupe_by_url(items: list[SearchResultItem]) -> list[SearchResultItem]:
    """按规范化 URL 去重，保留首次出现顺序.

    用于引擎内部去重 (如 ascii2d 的 bovw / color 两路命中高度重叠)：
    调用方按优先级排好输入顺序，这里保留先出现的那条作为代表。

    Args:
        items: 结果项列表

    Returns:
        去重后的结果列表
    """
    result: list[SearchResultItem] = []
    for group in group_by_url(items):
        # 引擎内去重保留先出现者 (调用方已按模式可信度排序)，但分数取两路最大值
        score = group.best_score
        if score is not None and group.first_item.score is not None:
            score = max(score, group.first_item.score)
        item = replace(
            group.first_item,
            score=score if score is not None else group.first_item.score,
            matched_by=list(group.engines),
        )
        if not item.thumbnail and group.best_item.thumbnail:
            item = replace(
                item,
                thumbnail=group.best_item.thumbnail,
                thumbnail_bytes=group.best_item.thumbnail_bytes,
            )
        result.append(item)
    return result


def merge_and_rank(items: list[SearchResultItem], limit: int) -> list[SearchResultItem]:
    """跨引擎融合排序并截断.

    步骤: 按规范化 URL 跨引擎去重 -> 多引擎共识加成 -> 稳定排序 -> 截断。
    先融合再截断，避免"高置信度结果被低置信度结果挤掉"。

    Args:
        items: 所有引擎的结果项 (按引擎注册顺序拼接)
        limit: 最终展示条数上限

    Returns:
        融合排序后的结果列表
    """
    if not items:
        return []

    groups = group_by_url(items)
    # sorted 是稳定排序，sort_key 里已经带上原始位次，结果完全确定
    groups.sort(key=lambda group: group.sort_key())
    merged = [_finalize(group) for group in groups]

    if limit > 0:
        return merged[:limit]
    return merged
