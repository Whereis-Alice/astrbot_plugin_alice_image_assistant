"""
help.py
帮助消息管理模块
"""

import json
import re
from pathlib import Path

from astrbot.api import logger

_PUBLIC_COMMAND_NAMES = {
    "/pixiv": "/插画",
    "/pixiv_ai_show_settings": "/生成图设置",
    "/pixiv_and": "/插画并搜",
    "/pixiv_config": "/插画设置",
    "/pixiv_deepsearch": "/深度插画",
    "/pixiv_fanbox_artist": "/赞助搜索",
    "/pixiv_fanbox_creator": "/赞助画师",
    "/pixiv_fanbox_post": "/赞助帖子",
    "/pixiv_fanbox_recommended": "/赞助推荐",
    "/pixiv_help": "/插画帮助",
    "/pixiv_hot": "/热门插画",
    "/pixiv_illust_comments": "/插画评论",
    "/pixiv_illust_new": "/最新插画",
    "/pixiv_novel": "/小说",
    "/pixiv_novel_comments": "/小说评论",
    "/pixiv_novel_download": "/下载小说",
    "/pixiv_novel_new": "/最新小说",
    "/pixiv_novel_recommended": "/推荐小说",
    "/pixiv_novel_series": "/小说系列",
    "/pixiv_random_add": "/随机添加",
    "/pixiv_random_del": "/随机删除",
    "/pixiv_random_force": "/随机执行",
    "/pixiv_random_list": "/随机列表",
    "/pixiv_random_ranking_add": "/榜单添加",
    "/pixiv_random_ranking_del": "/榜单删除",
    "/pixiv_random_ranking_list": "/榜单列表",
    "/pixiv_random_resume": "/随机开启",
    "/pixiv_random_status": "/随机状态",
    "/pixiv_random_suspend": "/随机暂停",
    "/pixiv_ranking": "/插画榜",
    "/pixiv_recommended": "/推荐插画",
    "/pixiv_related": "/相关插画",
    "/pixiv_showcase_article": "/插画特辑",
    "/pixiv_specific": "/作品详情",
    "/pixiv_subscribe_add": "/订阅画师",
    "/pixiv_subscribe_list": "/画师订阅",
    "/pixiv_subscribe_remove": "/退订画师",
    "/pixiv_trending_tags": "/趋势标签",
    "/pixiv_user_detail": "/画师详情",
    "/pixiv_user_illusts": "/画师作品",
    "/pixiv_user_random": "/随机插画",
    "/pixiv_user_search": "/画师",
    # /画师找图 是本插件自有的复合命令（正向搜索入口），上游没有同名命令；
    # 这里补一条映射，保证帮助文案里出现 /pixiv_artist_find 时也能被正确替换。
    "/pixiv_artist_find": "/画师找图",
}
_UPSTREAM_COMMAND_PATTERN = re.compile(r"/pixiv(?:_[a-z_]+)?(?![a-zA-Z0-9_])")


def replace_public_command_names(message: str) -> str:
    """Replace upstream command examples with this plugin's public commands."""
    return _UPSTREAM_COMMAND_PATTERN.sub(
        lambda match: _PUBLIC_COMMAND_NAMES.get(match.group(0), match.group(0)),
        message,
    )


class HelpManager:
    """帮助消息管理器"""

    def __init__(self, data_dir: Path):
        """初始化帮助管理器

        Args:
            data_dir: 数据目录路径
        """
        self.data_dir = data_dir
        # 使用插件目录下的帮助文件
        self.help_file = Path(__file__).parent.parent / "data" / "helpmsg.json"
        self._help_messages: dict[str, str] = {}
        self._load_help_messages()

    def _load_help_messages(self):
        """加载帮助消息"""
        try:
            if self.help_file.exists():
                with self.help_file.open(encoding="utf-8") as f:
                    self._help_messages = json.load(f)
                logger.info(f"Pixiv 插件：成功加载帮助消息文件 {self.help_file}")
            else:
                logger.warning(f"Pixiv 插件：帮助消息文件不存在: {self.help_file}")
                self._help_messages = {}
        except Exception as e:
            logger.error(f"Pixiv 插件：加载帮助消息文件失败 - {e}")
            self._help_messages = {}

    def get_help_message(self, key: str, default: str | None = None) -> str:
        """获取帮助消息

        Args:
            key: 帮助消息的键
            default: 默认消息（如果键不存在）

        Returns:
            str: 帮助消息
        """
        if key in self._help_messages:
            return replace_public_command_names(self._help_messages[key])
        logger.warning(f"Pixiv 插件：未找到帮助消息键: {key}")
        return default or f"帮助消息 '{key}' 未找到"

    def reload_help_messages(self):
        """重新加载帮助消息"""
        self._load_help_messages()


# 全局帮助管理器实例
_help_manager: HelpManager | None = None


def init_help_manager(data_dir: Path):
    """初始化帮助管理器

    Args:
        data_dir: 数据目录路径
    """
    global _help_manager
    _help_manager = HelpManager(data_dir)


def get_help_message(key: str, default: str | None = None) -> str:
    """获取帮助消息

    Args:
        key: 帮助消息的键
        default: 默认消息（如果键不存在）

    Returns:
        str: 帮助消息
    """
    if _help_manager is None:
        return default or f"帮助管理器未初始化，无法获取消息 '{key}'"
    return _help_manager.get_help_message(key, default)
