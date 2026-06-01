"""
MaiBot B站视频插件
整合了链接识别、视频下载、字幕读取和AI总结功能
"""

from .plugin import BiliVideoPlugin

__version__ = "1.0.0"
__plugin_name__ = "maibot_plugin_bilivideo"
__author__ = "Integrated from link_resolver, biliread, biliVideo"

__all__ = ["BiliVideoPlugin"]
