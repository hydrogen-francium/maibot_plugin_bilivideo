"""
MaiBot B站视频插件
功能：自动识别B站链接 → 渲染卡片 → 下载视频 → 合并转发
"""

from .plugin import BiliVideoPlugin

__version__ = "2.0.0"
__plugin_name__ = "maibot_plugin_bilivideo"
__author__ = "hydrogen-francium"

__all__ = ["BiliVideoPlugin"]
