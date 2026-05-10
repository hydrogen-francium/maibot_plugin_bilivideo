"""
MaiBot B站视频插件
功能：自动识别B站链接 → 渲染卡片 → 下载视频 → 合并转发
"""

import asyncio
import base64
import re
import time
from typing import List, Tuple, Optional
from pathlib import Path

from src.plugin_system import (
    BasePlugin,
    register_plugin,
    BaseEventHandler,
    ConfigField,
    EventType,
)
from src.common.logger import get_logger
from bilibili_api import Credential

from .core import (
    BiliLinkResolver,
    BiliVideoDownloader,
    BiliCardRenderer,
    VideoInfo,
)
from .utils import CacheManager

logger = get_logger("bilivideo_plugin")

# 全局缓存清理任务
_cache_clean_task: Optional[asyncio.Task] = None

# 防重复处理：BV号 -> 上次处理时间
_recent_processed: dict = {}
_RECENT_DEDUP_SECONDS = 60  # 60秒内不重复处理同一视频


@register_plugin
class BiliVideoPlugin(BasePlugin):
    """B站视频插件主类"""

    plugin_name: str = "maibot_plugin_bilivideo"
    enable_plugin: bool = True
    dependencies: List[str] = []
    python_dependencies: List[str] = [
        "bilibili-api-python>=16.2.0",
        "httpx>=0.24.0",
        "aiohttp>=3.9.0",
        "jinja2>=3.1.0",
        "playwright>=1.40.0",
    ]
    config_file_name: str = "config.toml"

    config_section_descriptions = {
        "plugin": "插件基础信息",
        "bilibili": "B站账号配置",
        "auto_detect": "自动链接识别配置",
        "download": "视频下载配置",
        "cache": "缓存管理配置",
    }

    config_schema: dict = {
        "plugin": {
            "enabled": ConfigField(type=bool, default=True, description="是否启用插件"),
            "config_version": ConfigField(type=str, default="2.0.0", description="配置版本"),
        },
        "bilibili": {
            "sessdata": ConfigField(type=str, default="", description="B站SESSDATA Cookie"),
            "bili_jct": ConfigField(type=str, default="", description="B站bili_jct Cookie"),
        },
        "auto_detect": {
            "enabled": ConfigField(type=bool, default=True, description="是否启用自动链接识别"),
            "use_image_card": ConfigField(type=bool, default=True, description="使用图片卡片（false为纯文本）"),
            "ignore_replied_messages": ConfigField(type=bool, default=True, description="忽略引用消息中的链接"),
            "card_save_path": ConfigField(type=str, default="./data/bilivideo/cards", description="卡片图片保存路径"),
        },
        "download": {
            "enabled": ConfigField(type=bool, default=True, description="是否启用视频下载"),
            "quality": ConfigField(type=str, default="720P", description="默认画质 360P/480P/720P/1080P/1080P+/4K"),
            "max_size_mb": ConfigField(type=int, default=100, description="最大文件大小(MB)"),
            "max_duration_seconds": ConfigField(type=int, default=600, description="最大视频时长(秒)，0为不限制"),
            "allow_quality_fallback": ConfigField(type=bool, default=True, description="超限时自动降画质"),
            "save_path": ConfigField(type=str, default="./data/bilivideo/videos", description="视频保存路径"),
        },
        "cache": {
            "auto_clean": ConfigField(type=bool, default=True, description="是否自动清理缓存"),
            "max_age_hours": ConfigField(type=int, default=24, description="缓存文件最大保留时间(小时)"),
            "clean_interval_hours": ConfigField(type=int, default=6, description="自动清理间隔(小时)"),
        },
    }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        # 启动缓存自动清理
        self._start_cache_cleaner()

        logger.info(f"{self.log_prefix} B站视频插件已加载")

    def get_plugin_components(self) -> List[Tuple]:
        """获取插件组件列表"""
        components = []

        # 仅注册消息监听器（自动识别链接）
        if self.config.get("auto_detect", {}).get("enabled", True):
            components.append((BiliLinkDetectHandler.get_handler_info(), BiliLinkDetectHandler))

        return components

    def _start_cache_cleaner(self):
        """启动缓存自动清理任务"""
        global _cache_clean_task

        cache_config = self.config.get("cache", {})
        if not cache_config.get("auto_clean", True):
            return

        if _cache_clean_task and not _cache_clean_task.done():
            return

        cache_dirs = [
            self.config.get("download", {}).get("save_path", "./data/bilivideo/videos"),
            self.config.get("auto_detect", {}).get("card_save_path", "./data/bilivideo/cards"),
        ]

        max_age_hours = cache_config.get("max_age_hours", 24)
        clean_interval_hours = cache_config.get("clean_interval_hours", 6)

        cache_manager = CacheManager(cache_dirs=cache_dirs, max_age_hours=max_age_hours)

        try:
            loop = asyncio.get_event_loop()
            _cache_clean_task = loop.create_task(
                self._cache_clean_loop(cache_manager, clean_interval_hours)
            )
            logger.info(f"{self.log_prefix} 缓存清理任务已启动 (间隔{clean_interval_hours}h, 保留{max_age_hours}h)")
        except RuntimeError:
            logger.warning(f"{self.log_prefix} 无法启动缓存清理任务（事件循环未运行）")

    @staticmethod
    async def _cache_clean_loop(cache_manager: CacheManager, interval_hours: int):
        """缓存清理循环任务"""
        interval_seconds = interval_hours * 3600

        try:
            cache_manager.clean_expired_files()
        except Exception as e:
            logger.error(f"缓存清理失败: {e}")

        while True:
            try:
                await asyncio.sleep(interval_seconds)
                cache_manager.clean_expired_files()
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"缓存清理失败: {e}")


def _strip_replied_content(plain_text: str) -> str:
    """从消息文本中移除引用消息内容"""
    pattern = r"\[回复<[^>]*>\s*的消息：.*?\]"
    cleaned = re.sub(pattern, "", plain_text, flags=re.DOTALL)
    return cleaned.strip()


def _extract_text_from_segments(message_segments) -> str:
    """从消息段中提取非引用部分的纯文本"""
    texts = []
    if not message_segments:
        return ""

    for seg in message_segments:
        if not hasattr(seg, "type"):
            continue
        if seg.type == "reply":
            continue
        if seg.type == "text" and isinstance(seg.data, str):
            texts.append(seg.data)
        elif seg.type == "seglist" and isinstance(seg.data, list):
            texts.append(_extract_text_from_segments(seg.data))

    return " ".join(t for t in texts if t).strip()


def _recent_processed_cleanup():
    """清理过老的处理记录"""
    now = time.time()
    expired_keys = [
        k for k, v in _recent_processed.items()
        if now - v > _RECENT_DEDUP_SECONDS * 2
    ]
    for k in expired_keys:
        _recent_processed.pop(k, None)


def _render_text_fallback(info: VideoInfo) -> str:
    """纯文本卡片兜底"""
    lines = [
        f"📺 {info.title}",
        f"👤 UP主: {info.uploader}",
    ]
    minutes = info.duration // 60
    seconds = info.duration % 60
    lines.append(f"⏱️ 时长: {minutes}:{seconds:02d}")
    lines.append(
        f"▶️ {_format_count(info.view_count)}播放  "
        f"💬 {_format_count(info.danmaku_count)}弹幕  "
        f"👍 {_format_count(info.like_count)}点赞"
    )
    lines.append(f"🔗 {info.url}")
    return "\n".join(lines)


def _format_count(count: int) -> str:
    if count is None:
        return "0"
    if count >= 100000000:
        return f"{count / 100000000:.1f}亿"
    if count >= 10000:
        return f"{count / 10000:.1f}万"
    return str(count)


class BiliLinkDetectHandler(BaseEventHandler):
    """B站链接自动识别事件处理器"""

    event_type = EventType.ON_MESSAGE
    handler_name = "bili_link_detect"
    handler_description = "自动识别消息中的B站视频链接，渲染卡片并下载视频"
    weight = 100
    intercept_message = False

    async def execute(self, message) -> Tuple[bool, bool, Optional[str], None, None]:
        """执行链接识别"""
        if message is None:
            return True, True, None, None, None

        try:
            # 1. 提取文本（排除引用消息中的内容）
            ignore_replied = self.get_config("auto_detect.ignore_replied_messages", True)

            if ignore_replied:
                detect_text = _extract_text_from_segments(message.message_segments)
                if not detect_text:
                    detect_text = _strip_replied_content(message.plain_text or "")
            else:
                detect_text = message.plain_text or ""

            if not detect_text:
                return True, True, None, None, None

            # 2. 跳过指令
            if detect_text.strip().startswith("/"):
                return True, True, None, None, None

            # 3. 解析链接
            sessdata = self.get_config("bilibili.sessdata", "")
            bili_jct = self.get_config("bilibili.bili_jct", "")
            credential = Credential(sessdata=sessdata, bili_jct=bili_jct)
            resolver = BiliLinkResolver(credential=credential)

            video_info = await resolver.detect_bili_link(detect_text)

            if not video_info:
                return True, True, None, None, None

            # 4. 防重复
            now = time.time()
            last = _recent_processed.get(video_info.bvid, 0)
            if now - last < _RECENT_DEDUP_SECONDS:
                logger.debug(f"跳过最近已处理的视频: {video_info.bvid}")
                return True, True, None, None, None
            _recent_processed[video_info.bvid] = now
            _recent_processed_cleanup()

            # 5. 流ID
            stream_id = message.stream_id
            if not stream_id:
                logger.warning(f"{self.log_prefix} 消息缺少stream_id")
                return True, True, None, None, None

            logger.info(
                f"{self.log_prefix} 识别到B站视频: {video_info.title[:30]} "
                f"(BV={video_info.bvid}, 时长={video_info.duration}s, "
                f"弹幕样本={len(video_info.danmaku_samples)}, 热评={len(video_info.hot_comments)})"
            )

            # 6. 渲染卡片
            use_image = self.get_config("auto_detect.use_image_card", True)
            card_save_path = self.get_config("auto_detect.card_save_path", "./data/bilivideo/cards")
            card_image_b64 = None

            if use_image:
                try:
                    card_renderer = BiliCardRenderer(save_path=card_save_path)
                    card_path = await card_renderer.render_card(video_info)
                    if card_path and card_path.exists():
                        card_image_b64 = base64.b64encode(card_path.read_bytes()).decode("utf-8")
                        logger.info(f"{self.log_prefix} 卡片渲染成功: {card_path.name}")
                    else:
                        logger.warning(f"{self.log_prefix} 卡片渲染失败，回退纯文本")
                except Exception as e:
                    logger.error(f"{self.log_prefix} 卡片渲染异常: {e}")
                    import traceback
                    logger.error(traceback.format_exc())

            # 7. 下载视频
            download_enabled = self.get_config("download.enabled", True)
            max_duration = self.get_config("download.max_duration_seconds", 600)
            duration_ok = (max_duration <= 0) or (video_info.duration <= max_duration)

            video_path = None
            if download_enabled and duration_ok:
                quality = self.get_config("download.quality", "720P")
                max_size_mb = self.get_config("download.max_size_mb", 100)
                allow_fallback = self.get_config("download.allow_quality_fallback", True)
                save_path = self.get_config("download.save_path", "./data/bilivideo/videos")

                logger.info(
                    f"{self.log_prefix} 开始下载: BV={video_info.bvid}, 画质={quality}, 上限={max_size_mb}MB"
                )

                downloader = BiliVideoDownloader(
                    credential=credential,
                    save_path=save_path,
                    max_size_mb=max_size_mb,
                    allow_quality_fallback=allow_fallback,
                )
                try:
                    video_path = await downloader.download_video(
                        bvid=video_info.bvid,
                        quality=quality,
                        page_index=0,
                    )
                    if video_path and video_path.exists():
                        size_mb = video_path.stat().st_size / 1024 / 1024
                        logger.info(f"{self.log_prefix} 视频下载完成: {video_path.name} ({size_mb:.2f}MB)")
                except Exception as e:
                    logger.error(f"{self.log_prefix} 视频下载失败: {e}")
                    import traceback
                    logger.error(traceback.format_exc())
            elif download_enabled and not duration_ok:
                logger.info(f"{self.log_prefix} 视频时长 {video_info.duration}s 超过 {max_duration}s，跳过下载")

            # 8. 发送
            await self._send_response(
                stream_id=stream_id,
                video_info=video_info,
                card_image_b64=card_image_b64,
                video_path=video_path,
            )

            return True, True, None, None, None

        except Exception as e:
            logger.error(f"{self.log_prefix} 链接识别失败: {e}")
            import traceback
            logger.error(traceback.format_exc())
            return True, True, None, None, None

    async def _send_response(
        self,
        stream_id: str,
        video_info: VideoInfo,
        card_image_b64: Optional[str],
        video_path: Optional[Path],
    ):
        """发送响应消息（卡片+视频，合并转发）"""
        # 准备视频base64
        video_b64 = None
        if video_path and video_path.exists():
            try:
                video_b64 = base64.b64encode(video_path.read_bytes()).decode("utf-8")
            except Exception as e:
                logger.error(f"{self.log_prefix} 读取视频失败: {e}")

        # 卡片内容
        if card_image_b64:
            card_type, card_content = "image", card_image_b64
        else:
            card_type, card_content = "text", _render_text_fallback(video_info)

        try:
            if video_b64:
                # 合并转发：卡片+视频
                logger.info(f"{self.log_prefix} 通过合并转发发送")
                sender_id = "10000"
                sender_name = "B站解析"

                ok = await self.send_forward(
                    stream_id=stream_id,
                    messages_list=[
                        (sender_id, sender_name, [(card_type, card_content)]),
                        (sender_id, sender_name, [("video", video_b64)]),
                    ],
                )
                if not ok:
                    logger.warning(f"{self.log_prefix} 合并转发失败，回退分开发送")
                    raise RuntimeError("forward failed")
            else:
                # 仅卡片
                logger.info(f"{self.log_prefix} 仅发送卡片")
                if card_type == "image":
                    await self.send_image(stream_id=stream_id, image_base64=card_content)
                else:
                    await self.send_text(stream_id=stream_id, text=card_content)

        except Exception as e:
            logger.error(f"{self.log_prefix} 发送失败: {e}")
            import traceback
            logger.error(traceback.format_exc())

            # 兜底：分开发送
            try:
                if card_type == "image":
                    await self.send_image(stream_id=stream_id, image_base64=card_content)
                else:
                    await self.send_text(stream_id=stream_id, text=card_content)

                if video_b64:
                    await self.send_custom(
                        stream_id=stream_id,
                        message_type="video",
                        content=video_b64,
                    )
                logger.info(f"{self.log_prefix} 兜底分开发送成功")
            except Exception as e2:
                logger.error(f"{self.log_prefix} 兜底也失败: {e2}")
                try:
                    await self.send_text(stream_id=stream_id, text=_render_text_fallback(video_info))
                except Exception:
                    pass
