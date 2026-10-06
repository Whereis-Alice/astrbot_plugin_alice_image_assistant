"""指令目录（WebUI `/commands` 与 README 共用的唯一数据源）。

这里只描述"插件对外暴露了哪些指令"，不含任何执行逻辑；`main.py` 里
`@filter.command` 的名字若有增删，请同步本表，并跑 `tests/test_webapi.py`
里的一致性校验。
"""

from __future__ import annotations

from typing import Any, Final, NamedTuple


class CommandSpec(NamedTuple):
    """单条指令的展示信息。"""

    cmd: str
    usage: str
    desc: str
    tags: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "cmd": self.cmd,
            "usage": self.usage,
            "desc": self.desc,
            "tags": list(self.tags),
        }


class CommandGroup(NamedTuple):
    """一组同类指令。"""

    key: str
    label: str
    icon: str
    desc: str
    commands: tuple[CommandSpec, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "label": self.label,
            "icon": self.icon,
            "desc": self.desc,
            "commands": [item.to_dict() for item in self.commands],
        }


_GROUPS: Final[tuple[CommandGroup, ...]] = (
    CommandGroup(
        key="core",
        label="核心",
        icon="compass",
        desc="帮助、文字找图与图片溯源。",
        commands=(
            CommandSpec(
                "/图片帮助", "/图片帮助", "查看插件状态、当前找图来源与识图引擎。", ("帮助",)
            ),
            CommandSpec(
                "/找图",
                "/找图 <关键词>",
                "自动选源找图：按关键词判断走 Pixiv / 搜图 / SerpApi。",
                ("找图", "自动"),
            ),
            CommandSpec(
                "/搜图",
                "/搜图 <关键词>",
                "优先使用搜图神器来源，结合 Bing 补充和视觉挑图；按配置回退。",
                ("找图",),
            ),
            CommandSpec(
                "/谷歌搜图",
                "/谷歌搜图 <关键词>",
                "优先使用 SerpApi Google 图片搜索；按配置回退。",
                ("找图",),
            ),
            CommandSpec(
                "/识图",
                "/识图 [引擎/意图]",
                "以图搜图：可指定引擎，也可写‘出处’‘相似图’等意图；附图、回复图片或随后补图均可。",
                ("溯源",),
            ),
        ),
    ),
    CommandGroup(
        key="pixiv_illust",
        label="Pixiv 插画",
        icon="palette",
        desc="标签搜索、榜单、相关作品与合辑。",
        commands=(
            CommandSpec(
                "/插画",
                "/插画 <标签> [数量]",
                "按标签搜索插画，支持逗号分隔与排除标签。",
                ("Pixiv",),
            ),
            CommandSpec(
                "/插画并搜",
                "/插画并搜 <标签1,标签2>",
                "多标签 AND 搜索，要求同时命中全部标签。",
                ("Pixiv",),
            ),
            CommandSpec(
                "/深度插画", "/深度插画 <标签>", "跨多页搜索插画，匹配任一指定标签。", ("Pixiv",)
            ),
            CommandSpec(
                "/热门插画",
                "/热门插画 <标签> [时间范围] [页数]",
                "热度搜索：在时间窗内按收藏数排序。",
                ("Pixiv",),
            ),
            CommandSpec(
                "/最新插画",
                "/最新插画 [类型] [最大作品ID]",
                "获取最新作品；类型为 illust（插画）或 manga（漫画）。",
                ("Pixiv",),
            ),
            CommandSpec("/推荐插画", "/推荐插画", "Pixiv 为你推荐的插画。", ("Pixiv",)),
            CommandSpec("/作品详情", "/作品详情 <作品ID>", "按作品 ID 直接取图。", ("Pixiv",)),
            CommandSpec("/相关插画", "/相关插画 <作品ID>", "查看与该作品相关的推荐。", ("Pixiv",)),
            CommandSpec(
                "/插画榜",
                "/插画榜 [模式] [日期]",
                "排行榜：day / week / month / day_r18 等。",
                ("Pixiv",),
            ),
            CommandSpec("/插画评论", "/插画评论 <作品ID> [偏移量]", "查看作品评论。", ("Pixiv",)),
            CommandSpec(
                "/插画特辑", "/插画特辑 <特辑ID>", "查看 Pixiv 特辑（showcase）内容。", ("Pixiv",)
            ),
            CommandSpec("/趋势标签", "/趋势标签", "查看当前热门标签趋势。", ("Pixiv",)),
        ),
    ),
    CommandGroup(
        key="pixiv_artist",
        label="Pixiv 画师",
        icon="user",
        desc="画师检索、详情与定向找图。",
        commands=(
            CommandSpec("/画师", "/画师 <画师名>", "按名字搜索画师。", ("Pixiv", "画师")),
            CommandSpec(
                "/画师详情", "/画师详情 <画师ID>", "查看画师资料与统计。", ("Pixiv", "画师")
            ),
            CommandSpec(
                "/画师作品",
                "/画师作品 <画师ID或名字> [数量]",
                "取该画师的作品列表。",
                ("Pixiv", "画师"),
            ),
            CommandSpec(
                "/随机插画",
                "/随机插画 <画师ID或名字> [数量]",
                "从该画师作品里随机抽图。",
                ("Pixiv", "画师"),
            ),
            CommandSpec(
                "/画师找图",
                "/画师找图 <画师名或ID> [| 关键词] [数量]",
                "先锁定画师，再在其作品内按关键词做视觉挑图。",
                ("Pixiv", "画师", "精准"),
            ),
        ),
    ),
    CommandGroup(
        key="pixiv_novel",
        label="Pixiv 小说",
        icon="book",
        desc="小说搜索、系列与下载。",
        commands=(
            CommandSpec("/小说", "/小说 <标签>", "按标签搜索小说。", ("Pixiv", "小说")),
            CommandSpec("/推荐小说", "/推荐小说", "Pixiv 推荐小说。", ("Pixiv", "小说")),
            CommandSpec("/最新小说", "/最新小说 [最大小说ID]", "获取最新小说。", ("Pixiv", "小说")),
            CommandSpec("/小说系列", "/小说系列 <系列ID>", "查看小说系列目录。", ("Pixiv", "小说")),
            CommandSpec(
                "/小说评论", "/小说评论 <小说ID> [偏移量]", "查看小说评论。", ("Pixiv", "小说")
            ),
            CommandSpec(
                "/下载小说", "/下载小说 <小说ID>", "把小说导出为 PDF 发送。", ("Pixiv", "小说")
            ),
        ),
    ),
    CommandGroup(
        key="pixiv_subscribe",
        label="订阅与定时",
        icon="bell",
        desc="画师订阅、随机推送与榜单推送。",
        commands=(
            CommandSpec("/订阅画师", "/订阅画师 <画师ID>", "订阅画师更新。", ("订阅",)),
            CommandSpec("/退订画师", "/退订画师 <画师ID>", "取消订阅画师。", ("订阅",)),
            CommandSpec("/画师订阅", "/画师订阅", "查看本会话的订阅列表。", ("订阅",)),
            CommandSpec(
                "/随机添加",
                "/随机添加 <标签>",
                "添加随机推送标签；推送间隔和数量使用配置值。",
                ("定时",),
            ),
            CommandSpec("/随机删除", "/随机删除 <序号>", "删除指定的随机推送任务。", ("定时",)),
            CommandSpec("/随机列表", "/随机列表", "列出本会话的随机推送任务。", ("定时",)),
            CommandSpec("/随机暂停", "/随机暂停", "暂停本会话的随机推送。", ("定时",)),
            CommandSpec("/随机开启", "/随机开启", "恢复本会话的随机推送。", ("定时",)),
            CommandSpec("/随机状态", "/随机状态", "查看随机推送调度器状态。", ("定时",)),
            CommandSpec("/随机执行", "/随机执行", "立刻手动触发一次随机推送。", ("定时",)),
            CommandSpec("/榜单添加", "/榜单添加 <模式> [日期]", "新增榜单定时推送。", ("定时",)),
            CommandSpec("/榜单删除", "/榜单删除 <序号>", "删除榜单定时推送。", ("定时",)),
            CommandSpec("/榜单列表", "/榜单列表", "列出榜单定时推送。", ("定时",)),
        ),
    ),
    CommandGroup(
        key="fanbox",
        label="Fanbox",
        icon="gift",
        desc="需要 Pixiv 登录态的 Fanbox 内容。",
        commands=(
            CommandSpec(
                "/赞助画师",
                "/赞助画师 <创作者> [数量]",
                "查看 Fanbox 创作者主页与帖子。",
                ("Fanbox",),
            ),
            CommandSpec(
                "/赞助帖子", "/赞助帖子 <帖子ID或链接>", "查看单个 Fanbox 帖子。", ("Fanbox",)
            ),
            CommandSpec("/赞助推荐", "/赞助推荐 [数量]", "Fanbox 推荐创作者。", ("Fanbox",)),
            CommandSpec(
                "/赞助搜索",
                "/赞助搜索 [关键词] [数量]",
                "按画师名反查 Fanbox 创作者。",
                ("Fanbox",),
            ),
        ),
    ),
    CommandGroup(
        key="settings",
        label="设置与帮助",
        icon="sliders",
        desc="聊天内改配置；更细的项建议直接用 WebUI 配置页。",
        commands=(
            CommandSpec(
                "/插画设置", "/插画设置 [键] [值]", "查看或修改 Pixiv 运行设置。", ("设置",)
            ),
            CommandSpec(
                "/生成图设置",
                "/生成图设置 <true/false>",
                "设置 Pixiv AI 作品显示偏好并同步本地过滤设置。",
                ("设置",),
            ),
            CommandSpec("/插画帮助", "/插画帮助", "查看 Pixiv 全部指令说明。", ("帮助",)),
        ),
    ),
)


def command_catalog() -> list[dict[str, Any]]:
    """供 `/commands` 返回的分组指令目录。"""

    return [group.to_dict() for group in _GROUPS]


def command_names() -> list[str]:
    """全部指令名（含前导斜杠），用于一致性校验。"""

    return [spec.cmd for group in _GROUPS for spec in group.commands]


def command_count() -> int:
    """指令总条数。"""

    return sum(len(group.commands) for group in _GROUPS)
