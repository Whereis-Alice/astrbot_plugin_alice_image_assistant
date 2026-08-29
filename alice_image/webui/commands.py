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
        desc="日常最常用的四条入口指令。",
        commands=(
            CommandSpec("/aa", "/aa", "查看插件状态、当前找图来源与溯源引擎。", ("帮助",)),
            CommandSpec(
                "/aa找",
                "/aa找 <关键词> [数量]",
                "自动选源找图：按关键词判断走 Pixiv / 搜图 / SerpApi。",
                ("找图", "自动"),
            ),
            CommandSpec(
                "/aa神",
                "/aa神 <关键词> [数量]",
                "强制走搜图源（Bing 兜底 + VLM 视觉挑图）。",
                ("找图",),
            ),
            CommandSpec(
                "/aaS",
                "/aaS <关键词> [数量]",
                "强制走 SerpApi Google 图片搜索。",
                ("找图",),
            ),
            CommandSpec(
                "/aa溯",
                "/aa溯 [引擎]",
                "以图搜图：附图、回复图片或发完指令后再发图均可。",
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
                "/aaP", "/aaP <标签> [数量]", "按标签搜索插画，支持逗号分隔与排除标签。", ("Pixiv",)
            ),
            CommandSpec(
                "/aaP并",
                "/aaP并 <标签1,标签2> [数量]",
                "多标签 AND 搜索，要求同时命中全部标签。",
                ("Pixiv",),
            ),
            CommandSpec(
                "/aaP深", "/aaP深 <标签> [数量]", "深度搜索：多页抓取后按热度重排。", ("Pixiv",)
            ),
            CommandSpec(
                "/aaP热",
                "/aaP热 <标签> [时间窗] [数量]",
                "热度搜索：在时间窗内按收藏数排序。",
                ("Pixiv",),
            ),
            CommandSpec(
                "/aaP新", "/aaP新 [illust|manga] [起始ID]", "关注画师的最新作品。", ("Pixiv",)
            ),
            CommandSpec("/aaP荐", "/aaP荐 [数量]", "Pixiv 为你推荐的插画。", ("Pixiv",)),
            CommandSpec("/aaPID", "/aaPID <作品ID>", "按作品 ID 直接取图。", ("Pixiv",)),
            CommandSpec("/aaP似", "/aaP似 <作品ID> [数量]", "查看与该作品相关的推荐。", ("Pixiv",)),
            CommandSpec(
                "/aaP榜",
                "/aaP榜 [模式] [日期] [数量]",
                "排行榜：day / week / month / day_r18 等。",
                ("Pixiv",),
            ),
            CommandSpec("/aaP评", "/aaP评 <作品ID> [数量]", "查看作品评论。", ("Pixiv",)),
            CommandSpec(
                "/aaP辑", "/aaP辑 <特辑ID>", "查看 Pixiv 特辑（showcase）内容。", ("Pixiv",)
            ),
            CommandSpec("/aaP趋势", "/aaP趋势", "当前热门标签趋势。", ("Pixiv",)),
        ),
    ),
    CommandGroup(
        key="pixiv_artist",
        label="Pixiv 画师",
        icon="user",
        desc="画师检索、详情与定向找图。",
        commands=(
            CommandSpec("/aaP画师", "/aaP画师 <画师名>", "按名字搜索画师。", ("Pixiv", "画师")),
            CommandSpec(
                "/aaP画师详", "/aaP画师详 <画师ID>", "查看画师资料与统计。", ("Pixiv", "画师")
            ),
            CommandSpec(
                "/aaP画师作",
                "/aaP画师作 <画师ID或名字> [数量]",
                "取该画师的作品列表。",
                ("Pixiv", "画师"),
            ),
            CommandSpec(
                "/aaP画师随",
                "/aaP画师随 <画师ID或名字> [数量]",
                "从该画师作品里随机抽图。",
                ("Pixiv", "画师"),
            ),
            CommandSpec(
                "/aaP画师找",
                "/aaP画师找 <画师>|<关键词> [数量]",
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
            CommandSpec("/aaP文", "/aaP文 <标签> [数量]", "按标签搜索小说。", ("Pixiv", "小说")),
            CommandSpec("/aaP文荐", "/aaP文荐", "Pixiv 推荐小说。", ("Pixiv", "小说")),
            CommandSpec("/aaP文新", "/aaP文新 [起始ID]", "关注作者的最新小说。", ("Pixiv", "小说")),
            CommandSpec("/aaP文系", "/aaP文系 <系列ID>", "查看小说系列目录。", ("Pixiv", "小说")),
            CommandSpec(
                "/aaP文评", "/aaP文评 <小说ID> [数量]", "查看小说评论。", ("Pixiv", "小说")
            ),
            CommandSpec(
                "/aaP文下", "/aaP文下 <小说ID>", "把小说导出为 PDF 发送。", ("Pixiv", "小说")
            ),
        ),
    ),
    CommandGroup(
        key="pixiv_subscribe",
        label="订阅与定时",
        icon="bell",
        desc="画师订阅、随机推送与榜单推送。",
        commands=(
            CommandSpec("/aaP订", "/aaP订 <画师ID>", "订阅画师更新。", ("订阅",)),
            CommandSpec("/aaP退", "/aaP退 <画师ID>", "取消订阅画师。", ("订阅",)),
            CommandSpec("/aaP订阅", "/aaP订阅", "查看本会话的订阅列表。", ("订阅",)),
            CommandSpec(
                "/aaP随加", "/aaP随加 <标签> [时间] [数量]", "新增一条定时随机推送任务。", ("定时",)
            ),
            CommandSpec("/aaP随删", "/aaP随删 <序号>", "删除指定的随机推送任务。", ("定时",)),
            CommandSpec("/aaP随列", "/aaP随列", "列出本会话的随机推送任务。", ("定时",)),
            CommandSpec("/aaP随停", "/aaP随停", "暂停本会话的随机推送。", ("定时",)),
            CommandSpec("/aaP随开", "/aaP随开", "恢复本会话的随机推送。", ("定时",)),
            CommandSpec("/aaP随态", "/aaP随态", "查看随机推送调度器状态。", ("定时",)),
            CommandSpec("/aaP随跑", "/aaP随跑", "立刻手动触发一次随机推送。", ("定时",)),
            CommandSpec(
                "/aaP随榜加", "/aaP随榜加 <模式> [时间] [数量]", "新增榜单定时推送。", ("定时",)
            ),
            CommandSpec("/aaP随榜删", "/aaP随榜删 <序号>", "删除榜单定时推送。", ("定时",)),
            CommandSpec("/aaP随榜列", "/aaP随榜列", "列出榜单定时推送。", ("定时",)),
        ),
    ),
    CommandGroup(
        key="fanbox",
        label="Fanbox",
        icon="gift",
        desc="需要 Pixiv 登录态的 Fanbox 内容。",
        commands=(
            CommandSpec(
                "/aaF主", "/aaF主 <创作者> [数量]", "查看 Fanbox 创作者主页与帖子。", ("Fanbox",)
            ),
            CommandSpec("/aaF帖", "/aaF帖 <帖子ID或链接>", "查看单个 Fanbox 帖子。", ("Fanbox",)),
            CommandSpec("/aaF荐", "/aaF荐 [数量]", "Fanbox 推荐创作者。", ("Fanbox",)),
            CommandSpec("/aaF找", "/aaF找 <画师名>", "按画师名反查 Fanbox 创作者。", ("Fanbox",)),
        ),
    ),
    CommandGroup(
        key="settings",
        label="设置与帮助",
        icon="sliders",
        desc="聊天内改配置；更细的项建议直接用 WebUI 配置页。",
        commands=(
            CommandSpec("/aaP设置", "/aaP设置 [键] [值]", "查看或修改 Pixiv 运行设置。", ("设置",)),
            CommandSpec("/aaPAI", "/aaPAI [模式]", "切换 AI 作品过滤模式。", ("设置",)),
            CommandSpec("/aaP帮助", "/aaP帮助 [分类]", "查看 Pixiv 全部指令说明。", ("帮助",)),
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
