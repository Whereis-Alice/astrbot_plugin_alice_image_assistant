"""找图来源判定（表驱动纯函数，不依赖网络与 provider）。

原有实现只看"正向关键词 or 含假名"，导致"真人 cos 动漫角色""壁纸照片"这类
明显想要真实照片的请求被误送到 Pixiv，返回一堆二次元插画。
引入优先级更高的反向关键词表后，来源选择本身的正确率提升，
后续视觉复核也就不必在"整批都是错方向的候选"里硬挑一张。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

# 正向信号：出现这些词说明用户想要的是二次元 / 插画类图像，Pixiv 召回更好。
PIXIV_POSITIVE_KEYWORDS: tuple[str, ...] = (
    "pixiv",
    "p站",
    "二次元",
    "动漫",
    "动画",
    "插画",
    "插图",
    "角色",
    "同人",
    "同人志",
    "壁纸",
    "立绘",
    "手绘",
    "厚涂",
    "萌图",
    "vtuber",
    "vocaloid",
    "anime",
    "manga",
    "illustration",
    "illust",
    "fanart",
    "waifu",
    "doujin",
)

# 反向信号：出现这些词说明用户想要真实照片 / 实拍素材，必须走真实图片源。
# 反向优先级高于正向，因为"真人 cos 动漫角色"里的"动漫/角色"只是描述对象，不是想要插画。
REAL_IMAGE_NEGATIVE_KEYWORDS: tuple[str, ...] = (
    "照片",
    "相片",
    "写真",
    "寫真",
    "实拍",
    "實拍",
    "真人",
    "真实",
    "真實",
    "摄影",
    "攝影",
    "实景",
    "实物",
    "现实",
    "現實",
    "风景照",
    "風景照",
    "截图",
    "截圖",
    "新闻",
    "纪实",
    "紀實",
    "航拍",
    "自拍",
    "cos",
    "cosplay",
    "photo",
    "photos",
    "photograph",
    "photography",
    "irl",
    "real life",
    "live action",
    "screenshot",
)

# 假名区间（平假名 + 片假名）：日文查询在 Pixiv 上召回明显更好。
_KANA_PATTERN = re.compile(r"[\u3040-\u309f\u30a0-\u30ff]")
# ASCII 关键词必须做"非字母数字边界"判定：Python 的 \b 会把"真人cos"里的中文当词字符而漏判，
# 而裸子串匹配又会让 cosmos 命中 cos，两者都会造成来源误选。
_ASCII_ONLY = re.compile(r"^[a-z0-9 ]+$")


def _matched_keywords(lowered: str, keywords: tuple[str, ...]) -> list[str]:
    """返回查询中命中的关键词列表；ASCII 词做边界判定，CJK 词按子串判定。"""
    hits: list[str] = []
    for keyword in keywords:
        if _ASCII_ONLY.match(keyword):
            pattern = r"(?<![a-z0-9])" + re.escape(keyword) + r"(?![a-z0-9])"
            if re.search(pattern, lowered):
                hits.append(keyword)
        elif keyword in lowered:
            hits.append(keyword)
    return hits


@dataclass(slots=True)
class QueryAnalysis:
    """一次来源判定的完整依据，便于日志与测试断言。"""

    query: str = ""
    prefer_pixiv: bool = False
    positive_hits: list[str] = field(default_factory=list)
    negative_hits: list[str] = field(default_factory=list)
    kana: bool = False
    reason: str = ""


def analyze_query(query: str) -> QueryAnalysis:
    """表驱动判定查询更适合 Pixiv 还是真实图片源。

    判定顺序：反向关键词 > 正向关键词 > 假名 > 默认真实图片源。
    """
    text = str(query or "")
    lowered = text.lower()
    negative_hits = _matched_keywords(lowered, REAL_IMAGE_NEGATIVE_KEYWORDS)
    positive_hits = _matched_keywords(lowered, PIXIV_POSITIVE_KEYWORDS)
    kana = bool(_KANA_PATTERN.search(text))

    if negative_hits:
        # 反向信号一票否决：宁可走真实图片源，也不要把"要照片"的请求送进插画站。
        return QueryAnalysis(
            query=text,
            prefer_pixiv=False,
            positive_hits=positive_hits,
            negative_hits=negative_hits,
            kana=kana,
            reason="命中真实图片反向关键词：" + "、".join(negative_hits),
        )
    if positive_hits:
        return QueryAnalysis(
            query=text,
            prefer_pixiv=True,
            positive_hits=positive_hits,
            negative_hits=[],
            kana=kana,
            reason="命中二次元正向关键词：" + "、".join(positive_hits),
        )
    if kana:
        return QueryAnalysis(
            query=text,
            prefer_pixiv=True,
            positive_hits=[],
            negative_hits=[],
            kana=True,
            reason="查询包含日文假名，Pixiv 标签召回更好。",
        )
    return QueryAnalysis(
        query=text,
        prefer_pixiv=False,
        positive_hits=[],
        negative_hits=[],
        kana=False,
        reason="无明确二次元信号，默认走真实图片源。",
    )


def looks_like_pixiv(query: str) -> bool:
    """判定查询是否更适合 Pixiv；等价于 analyze_query(query).prefer_pixiv。"""
    return analyze_query(query).prefer_pixiv
