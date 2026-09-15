"""把自然语言搜图意图路由到现有反查策略。

参考了同类插件的“意图选择引擎”思路，但这里不引入新的搜索引擎：
路由只在 Alice 已有的 SauceNAO、Google Lens、Ascii2d 之间选择，
并且始终以运行时实际加载的策略为准。
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass


@dataclass(frozen=True)
class _IntentProfile:
    key: str
    label: str
    priorities: tuple[str, ...]
    keywords: tuple[tuple[str, int], ...]


@dataclass(frozen=True)
class IntentRoute:
    """一次意图解析的可序列化结果。"""

    category: str | None
    label: str | None
    strategy_names: tuple[str, ...] = ()
    matched_keywords: tuple[str, ...] = ()
    score: int = 0
    recognized: bool = False

    def to_dict(self) -> dict[str, object]:
        """转成 LLM / WebUI 可直接消费的普通字典。"""

        return {
            "category": self.category,
            "label": self.label,
            "strategies": list(self.strategy_names),
            "matched_keywords": list(self.matched_keywords),
            "score": self.score,
            "recognized": self.recognized,
        }


# 优先级是“这个意图下最值得先试”的顺序，而不是强制要求。
# 例如没有 SauceNAO 时，“出处”仍会回退到 Ascii2d / Google Lens。
_PROFILES: tuple[_IntentProfile, ...] = (
    _IntentProfile(
        key="character",
        label="角色 / 人物",
        priorities=("saucenao", "ascii2d", "googlelens"),
        keywords=(
            ("哪个角色", 14),
            ("角色是谁", 14),
            ("人物是谁", 14),
            ("角色", 10),
            ("人物", 10),
            ("是谁", 8),
            ("cos", 7),
            ("character", 10),
            ("who is", 8),
        ),
    ),
    _IntentProfile(
        key="source",
        label="出处 / 作者",
        priorities=("saucenao", "ascii2d", "googlelens"),
        keywords=(
            ("图片出处", 14),
            ("找出处", 14),
            ("画师", 12),
            ("作者", 10),
            ("来源", 10),
            ("出处", 10),
            ("pixiv", 8),
            ("pid", 8),
            ("原作", 7),
            ("source", 9),
            ("artist", 9),
            ("author", 8),
        ),
    ),
    _IntentProfile(
        key="similar",
        label="相似图片",
        priorities=("googlelens", "ascii2d", "saucenao"),
        keywords=(
            ("找相似图", 15),
            ("相似图片", 14),
            ("相似图", 12),
            ("找相似", 12),
            ("类似图片", 11),
            ("同款", 8),
            ("相似", 8),
            ("类似", 7),
            ("similar", 9),
            ("lookalike", 8),
        ),
    ),
    _IntentProfile(
        key="anime",
        label="动漫 / 插画",
        priorities=("ascii2d", "saucenao", "googlelens"),
        keywords=(
            ("二次元", 12),
            ("动漫图", 12),
            ("插画", 10),
            ("同人图", 10),
            ("动漫", 8),
            ("同人", 8),
            ("画风", 6),
            ("anime", 9),
            ("illustration", 8),
            ("fanart", 8),
        ),
    ),
    _IntentProfile(
        key="web",
        label="原图 / 网页",
        priorities=("googlelens", "saucenao", "ascii2d"),
        keywords=(
            ("找原图", 14),
            ("原图链接", 14),
            ("网页来源", 12),
            ("商品图", 10),
            ("新闻图", 10),
            ("原图", 9),
            ("网页", 7),
            ("综合搜索", 7),
            ("original image", 10),
            ("web", 6),
            ("product", 6),
            ("news", 6),
        ),
    ),
)

_STRATEGY_ALIASES: dict[str, frozenset[str]] = {
    "saucenao": frozenset({"saucenao", "sauce", "saucenao搜索", "sauce nao"}),
    "googlelens": frozenset({"google", "googlelens", "google lens", "lens"}),
    "ascii2d": frozenset({"ascii2d", "ascii", "ascii 2d", "2d"}),
}
_ALL_INTENT_ALIASES = frozenset({"all", "全部", "所有", "并行", "全部策略"})


def _compact(value: str) -> str:
    """只去掉空白和常见分隔符，保留中文语义字符。"""

    return re.sub(r"[\s_\-]+", "", value.casefold())


def _strategy_key(name: str) -> str:
    compact = _compact(name)
    for key, aliases in _STRATEGY_ALIASES.items():
        if compact in {_compact(alias) for alias in aliases}:
            return key
    return compact


def route_intent(
    intent: str | None,
    available_names: Iterable[str],
) -> IntentRoute:
    """在当前实际可用的策略中选择最适合某个自然语言意图的一个。

    设计约定：

    * `strategies` 显式指定时由调用方优先处理，本函数只处理 `intent`；
    * 意图为空或无法识别时返回空路由，调用方应保留原来的“并行全部策略”行为；
    * 意图已识别但首选引擎未加载时，沿 profile 的优先级回退，实在没有才用全部；
    * 返回的名称保留运行时策略的原始大小写，方便日志和结果展示。
    """

    available = tuple(str(name).strip() for name in available_names if str(name).strip())
    raw = str(intent or "").strip()
    if not raw:
        return IntentRoute(None, None)

    compact = _compact(raw)
    if compact in {_compact(alias) for alias in _ALL_INTENT_ALIASES}:
        return IntentRoute(
            category="all",
            label="全部策略",
            strategy_names=available,
            matched_keywords=(raw,),
            score=1,
            recognized=True,
        )

    # 允许 LLM 把引擎名放进 intent；显式 strategies 仍然拥有更高优先级。
    for key, aliases in _STRATEGY_ALIASES.items():
        if compact not in {_compact(alias) for alias in aliases}:
            continue
        selected = next(
            (name for name in available if _strategy_key(name) == key),
            None,
        )
        if selected is None:
            return IntentRoute(
                category="explicit",
                label=raw,
                matched_keywords=(raw,),
                recognized=True,
            )
        return IntentRoute(
            category="explicit",
            label=raw,
            strategy_names=(selected,),
            matched_keywords=(raw,),
            score=1,
            recognized=True,
        )

    lowered = raw.casefold()
    scored: list[tuple[int, int, _IntentProfile, tuple[str, ...]]] = []
    for index, profile in enumerate(_PROFILES):
        matches = tuple(keyword for keyword, _ in profile.keywords if keyword.casefold() in lowered)
        if not matches:
            continue
        score = sum(weight for keyword, weight in profile.keywords if keyword.casefold() in lowered)
        scored.append((score, -index, profile, matches))

    if not scored:
        return IntentRoute(None, None)

    _, _, profile, matches = max(scored, key=lambda item: (item[0], item[1]))
    selected = next(
        (
            name
            for preferred in profile.priorities
            for name in available
            if _strategy_key(name) == preferred
        ),
        None,
    )
    selected_names = (selected,) if selected else available
    return IntentRoute(
        category=profile.key,
        label=profile.label,
        strategy_names=selected_names,
        matched_keywords=matches,
        score=sum(weight for keyword, weight in profile.keywords if keyword.casefold() in lowered),
        recognized=True,
    )
