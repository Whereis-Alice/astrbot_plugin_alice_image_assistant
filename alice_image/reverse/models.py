"""图片搜索插件数据模型.

定义搜索结果的数据结构。
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace


class ProviderSearchError(RuntimeError):
    """An engine could not finish; this is not evidence of zero matches."""


@dataclass
class SearchResultItem:
    """单个搜索结果项.

    Attributes:
        similarity: 人类可读的相似度字符串 (如 "95.32%")，仅部分引擎有值；
            保留该字段是为了向后兼容既有展示逻辑与 LLM 工具返回结构。
        score: 排序启发式分数 (0.0..1.0)，不是识别正确率或身份置信度。
        source_key: 归一化来源键 (如 "saucenao"、"ascii2d/bovw")，比 source 更细。
        matched_by: 找到该 URL 的引擎展示名列表，不代表网页内容已被证实。
    """

    title: str
    url: str
    thumbnail: str = ""
    thumbnail_bytes: bytes | None = None
    source: str = ""
    similarity: str | None = None
    description: str | None = None
    domain: str | None = None
    score: float | None = None
    source_key: str = ""
    matched_by: list[str] = field(default_factory=list)
    evidence: list[dict[str, str]] = field(default_factory=list)

    def with_thumbnail_bytes(self, bytes_data: bytes) -> SearchResultItem:
        """返回带有缩略图字节的新实例."""
        # 用 replace 而不是手写全部字段，避免以后新增字段时漏拷贝导致分数丢失
        return replace(self, thumbnail_bytes=bytes_data)


@dataclass
class ProviderSearchOutcome:
    """Per-request items and safe notices; never store notices on a shared strategy."""

    items: list[SearchResultItem] = field(default_factory=list)
    notices: list[str] = field(default_factory=list)


@dataclass
class ExplorationResult:
    """搜索结果集合."""

    items: list[SearchResultItem] = field(default_factory=list)
    evidence_items: list[SearchResultItem] = field(default_factory=list)
    attempted_strategies: list[str] = field(default_factory=list)
    failed_strategies: list[str] = field(default_factory=list)
    notices: list[str] = field(default_factory=list)

    @property
    def all_failed(self) -> bool:
        return bool(self.attempted_strategies) and len(self.failed_strategies) == len(self.attempted_strategies)
