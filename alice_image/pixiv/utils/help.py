"""
help.py
帮助消息管理模块
"""

import json
import re
from pathlib import Path
from typing import Dict, Optional
from astrbot.api import logger


_PUBLIC_COMMAND_NAMES = {
    "/pixiv": "/aaP",
    "/pixiv_ai_show_settings": "/aaPAI",
    "/pixiv_and": "/aaP并",
    "/pixiv_config": "/aaP设置",
    "/pixiv_deepsearch": "/aaP深",
    "/pixiv_fanbox_artist": "/aaF找",
    "/pixiv_fanbox_creator": "/aaF主",
    "/pixiv_fanbox_post": "/aaF帖",
    "/pixiv_fanbox_recommended": "/aaF荐",
    "/pixiv_help": "/aaP帮助",
    "/pixiv_hot": "/aaP热",
    "/pixiv_illust_comments": "/aaP评",
    "/pixiv_illust_new": "/aaP新",
    "/pixiv_novel": "/aaP文",
    "/pixiv_novel_comments": "/aaP文评",
    "/pixiv_novel_download": "/aaP文下",
    "/pixiv_novel_new": "/aaP文新",
    "/pixiv_novel_recommended": "/aaP文荐",
    "/pixiv_novel_series": "/aaP文系",
    "/pixiv_random_add": "/aaP随加",
    "/pixiv_random_del": "/aaP随删",
    "/pixiv_random_force": "/aaP随跑",
    "/pixiv_random_list": "/aaP随列",
    "/pixiv_random_ranking_add": "/aaP随榜加",
    "/pixiv_random_ranking_del": "/aaP随榜删",
    "/pixiv_random_ranking_list": "/aaP随榜列",
    "/pixiv_random_resume": "/aaP随开",
    "/pixiv_random_status": "/aaP随态",
    "/pixiv_random_suspend": "/aaP随停",
    "/pixiv_ranking": "/aaP榜",
    "/pixiv_recommended": "/aaP荐",
    "/pixiv_related": "/aaP似",
    "/pixiv_showcase_article": "/aaP辑",
    "/pixiv_specific": "/aaPID",
    "/pixiv_subscribe_add": "/aaP订",
    "/pixiv_subscribe_list": "/aaP订阅",
    "/pixiv_subscribe_remove": "/aaP退",
    "/pixiv_trending_tags": "/aaP趋势",
    "/pixiv_user_detail": "/aaP画师详",
    "/pixiv_user_illusts": "/aaP画师作",
    "/pixiv_user_search": "/aaP画师",
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
        self._help_messages: Dict[str, str] = {}
        self._load_help_messages()

    def _load_help_messages(self):
        """加载帮助消息"""
        try:
            if self.help_file.exists():
                with open(self.help_file, "r", encoding="utf-8") as f:
                    self._help_messages = json.load(f)
                logger.info(f"Pixiv 插件：成功加载帮助消息文件 {self.help_file}")
            else:
                logger.warning(f"Pixiv 插件：帮助消息文件不存在: {self.help_file}")
                self._help_messages = {}
        except Exception as e:
            logger.error(f"Pixiv 插件：加载帮助消息文件失败 - {e}")
            self._help_messages = {}

    def get_help_message(self, key: str, default: Optional[str] = None) -> str:
        """获取帮助消息

        Args:
            key: 帮助消息的键
            default: 默认消息（如果键不存在）

        Returns:
            str: 帮助消息
        """
        if key in self._help_messages:
            return replace_public_command_names(self._help_messages[key])
        else:
            logger.warning(f"Pixiv 插件：未找到帮助消息键: {key}")
            return default or f"帮助消息 '{key}' 未找到"

    def reload_help_messages(self):
        """重新加载帮助消息"""
        self._load_help_messages()


# 全局帮助管理器实例
_help_manager: Optional[HelpManager] = None


def init_help_manager(data_dir: Path):
    """初始化帮助管理器

    Args:
        data_dir: 数据目录路径
    """
    global _help_manager
    _help_manager = HelpManager(data_dir)


def get_help_message(key: str, default: Optional[str] = None) -> str:
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
