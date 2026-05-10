"""
B站链接解析器
整合自 astrbot_plugin_link_resolver
功能：识别B站链接、解析短链、获取视频信息（含弹幕和评论）
"""

import re
import asyncio
from typing import Optional, Dict, Any, List
from dataclasses import dataclass, field
from urllib.parse import urlparse, parse_qs
import httpx
from bilibili_api import video, Credential, comment
from bilibili_api.comment import CommentResourceType, OrderType


# 正则表达式
BILI_VIDEO_URL_PATTERN = r"(https?://)?(?:(?:www|m)\.)?bilibili\.com/video/(BV[0-9A-Za-z]{10}|av\d+)"
BILI_SHORT_LINK_PATTERN = r"https?://(?:b23\.tv|bili2233\.cn)/[A-Za-z\d._?%&+\-=/#]+"
BILI_BV_PATTERN = r"\bBV[0-9A-Za-z]{10}\b"
BILI_AV_PATTERN = r"\bav\d+\b"

_BILI_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/146.0.7680.31 Safari/537.36"
)
_BILI_HEADERS = {
    "User-Agent": _BILI_UA,
    "Referer": "https://www.bilibili.com/",
}


@dataclass
class HotComment:
    """热门评论"""
    user_name: str
    user_avatar: str
    user_level: int
    content: str
    like_count: int
    pubdate: int  # 时间戳


@dataclass
class VideoInfo:
    """视频信息数据类"""
    bvid: str
    avid: Optional[int]
    title: str
    uploader: str
    uploader_mid: int
    uploader_avatar: str  # UP主头像
    uploader_fans: int  # UP主粉丝数
    cover_url: str
    duration: int  # 秒
    view_count: int
    like_count: int
    coin_count: int
    share_count: int
    danmaku_count: int
    favorite_count: int  # 收藏数
    comment_count: int
    description: str
    pubdate: int  # 时间戳
    page_count: int
    url: str
    danmaku_samples: List[str] = field(default_factory=list)  # 弹幕样本
    hot_comments: List[HotComment] = field(default_factory=list)  # 热门评论


class BiliLinkResolver:
    """B站链接解析器"""

    def __init__(self, credential: Optional[Credential] = None):
        self.credential = credential or Credential()

    async def detect_bili_link(self, text: str) -> Optional[VideoInfo]:
        """
        从文本中检测B站链接并返回视频信息

        Args:
            text: 要检测的文本

        Returns:
            VideoInfo对象，如果没有检测到则返回None
        """
        # 1. 尝试提取BV号
        bvid = self._extract_bvid(text)
        if bvid:
            return await self.get_video_info(bvid)

        # 2. 尝试提取AV号
        avid = self._extract_avid(text)
        if avid:
            return await self.get_video_info_by_aid(avid)

        # 3. 尝试解析短链
        short_url = self._extract_short_url(text)
        if short_url:
            resolved_url = await self.resolve_short_url(short_url)
            if resolved_url:
                bvid = self._extract_bvid(resolved_url)
                if bvid:
                    return await self.get_video_info(bvid)

        return None

    async def parse_json_card(self, json_data: Dict[str, Any]) -> Optional[VideoInfo]:
        """
        解析QQ小程序JSON卡片

        Args:
            json_data: JSON卡片数据

        Returns:
            VideoInfo对象，如果解析失败则返回None
        """
        try:
            # 提取 qqdocurl
            meta = json_data.get("meta", {})
            if not isinstance(meta, dict):
                return None

            url = ""
            for key, val in meta.items():
                if isinstance(val, dict):
                    url = val.get("qqdocurl", "") or val.get("jumpUrl", "") or val.get("url", "")
                    if url and self._is_bili_domain(url):
                        break

            if not url:
                return None

            # 从URL中提取BV号
            bvid = self._extract_bvid(url)
            if bvid:
                return await self.get_video_info(bvid)

            # 如果是短链，先解析
            if "b23.tv" in url or "bili" in url:
                resolved_url = await self.resolve_short_url(url)
                if resolved_url:
                    bvid = self._extract_bvid(resolved_url)
                    if bvid:
                        return await self.get_video_info(bvid)

        except Exception as e:
            print(f"解析JSON卡片失败: {e}")

        return None

    async def resolve_short_url(self, short_url: str) -> Optional[str]:
        """
        解析b23.tv短链

        Args:
            short_url: 短链接

        Returns:
            解析后的长链接，失败返回None
        """
        try:
            async with httpx.AsyncClient(
                follow_redirects=True,
                timeout=10.0,
                headers=_BILI_HEADERS
            ) as client:
                response = await client.head(short_url)
                final_url = str(response.url)
                return final_url
        except Exception as e:
            print(f"解析短链失败 {short_url}: {e}")
            return None

    async def get_video_info(self, bvid: str) -> Optional[VideoInfo]:
        """
        获取视频详细信息（含弹幕样本和热门评论）

        Args:
            bvid: 视频BV号

        Returns:
            VideoInfo对象，失败返回None
        """
        try:
            v = video.Video(bvid=bvid, credential=self.credential)
            info = await v.get_info()

            stat = info.get("stat", {})
            owner = info.get("owner", {})
            avid = info.get("aid")

            # 并行拉取弹幕、评论、UP主粉丝数
            danmaku_samples_task = self._fetch_danmaku_samples(v)
            hot_comments_task = self._fetch_hot_comments(avid) if avid else asyncio.sleep(0, result=[])
            uploader_fans_task = self._fetch_uploader_fans(owner.get("mid"))

            danmaku_samples, hot_comments, uploader_fans = await asyncio.gather(
                danmaku_samples_task,
                hot_comments_task,
                uploader_fans_task,
                return_exceptions=True,
            )

            # 处理异常返回值
            if isinstance(danmaku_samples, Exception):
                danmaku_samples = []
            if isinstance(hot_comments, Exception):
                hot_comments = []
            if isinstance(uploader_fans, Exception):
                uploader_fans = 0

            return VideoInfo(
                bvid=info.get("bvid", bvid),
                avid=avid,
                title=info.get("title", "未知标题"),
                uploader=owner.get("name", "未知UP主"),
                uploader_mid=owner.get("mid", 0),
                uploader_avatar=owner.get("face", ""),
                uploader_fans=uploader_fans or 0,
                cover_url=info.get("pic", ""),
                duration=info.get("duration", 0),
                view_count=stat.get("view", 0),
                like_count=stat.get("like", 0),
                coin_count=stat.get("coin", 0),
                share_count=stat.get("share", 0),
                danmaku_count=stat.get("danmaku", 0),
                favorite_count=stat.get("favorite", 0),
                comment_count=stat.get("reply", 0),
                description=info.get("desc", ""),
                pubdate=info.get("pubdate", 0),
                page_count=len(info.get("pages", [])),
                url=f"https://www.bilibili.com/video/{bvid}",
                danmaku_samples=danmaku_samples or [],
                hot_comments=hot_comments or [],
            )
        except Exception as e:
            print(f"获取视频信息失败 {bvid}: {e}")
            return None

    async def _fetch_danmaku_samples(self, v: "video.Video", limit: int = 6) -> List[str]:
        """获取弹幕样本（去重去短）"""
        try:
            # 只取前6分钟的弹幕足够采样
            danmakus = await v.get_danmakus(page_index=0, from_seg=0, to_seg=0)
            if not danmakus:
                return []

            seen = set()
            samples: List[str] = []
            for dm in danmakus:
                text = (dm.text or "").strip()
                if not text or text in seen:
                    continue
                if len(text) < 2:
                    continue
                seen.add(text)
                samples.append(text)
                if len(samples) >= limit:
                    break
            return samples
        except Exception as e:
            print(f"获取弹幕失败: {e}")
            return []

    async def _fetch_hot_comments(self, avid: int, limit: int = 5) -> List[HotComment]:
        """获取热门评论（按点赞排序）"""
        try:
            data = await comment.get_comments(
                oid=avid,
                type_=CommentResourceType.VIDEO,
                page_index=1,
                order=OrderType.LIKE,
                credential=self.credential,
            )
            replies = data.get("replies") or []
            results: List[HotComment] = []
            for r in replies[:limit]:
                if not isinstance(r, dict):
                    continue
                member = r.get("member") or {}
                content = r.get("content") or {}
                results.append(HotComment(
                    user_name=member.get("uname", "匿名"),
                    user_avatar=member.get("avatar", ""),
                    user_level=int((member.get("level_info") or {}).get("current_level", 0) or 0),
                    content=(content.get("message") or "").strip(),
                    like_count=int(r.get("like", 0) or 0),
                    pubdate=int(r.get("ctime", 0) or 0),
                ))
            return results
        except Exception as e:
            print(f"获取热评失败: {e}")
            return []

    async def _fetch_uploader_fans(self, mid: Optional[int]) -> int:
        """获取UP主粉丝数"""
        if not mid:
            return 0
        try:
            url = "https://api.bilibili.com/x/relation/stat"
            async with httpx.AsyncClient(timeout=8.0, headers=_BILI_HEADERS) as client:
                resp = await client.get(url, params={"vmid": mid})
                if resp.status_code != 200:
                    return 0
                data = resp.json()
                if data.get("code") != 0:
                    return 0
                return int((data.get("data") or {}).get("follower", 0) or 0)
        except Exception:
            return 0

    async def get_video_info_by_aid(self, aid: int) -> Optional[VideoInfo]:
        """
        通过AV号获取视频信息

        Args:
            aid: 视频AV号

        Returns:
            VideoInfo对象，失败返回None
        """
        try:
            v = video.Video(aid=aid, credential=self.credential)
            info = await v.get_info()
            bvid = info.get("bvid")
            if bvid:
                return await self.get_video_info(bvid)
        except Exception as e:
            print(f"获取视频信息失败 av{aid}: {e}")
        return None

    @staticmethod
    def _extract_bvid(text: str) -> Optional[str]:
        """从文本中提取BV号"""
        match = re.search(BILI_BV_PATTERN, text)
        if match:
            bvid = match.group(0)
            # 规范化BV号
            if len(bvid) == 12 and bvid.startswith("BV"):
                return bvid
        return None

    @staticmethod
    def _extract_avid(text: str) -> Optional[int]:
        """从文本中提取AV号"""
        match = re.search(BILI_AV_PATTERN, text, re.IGNORECASE)
        if match:
            av_str = match.group(0)
            return int(av_str[2:])  # 去掉 "av" 前缀
        return None

    @staticmethod
    def _extract_short_url(text: str) -> Optional[str]:
        """从文本中提取短链接"""
        match = re.search(BILI_SHORT_LINK_PATTERN, text)
        return match.group(0) if match else None

    @staticmethod
    def _is_bili_domain(url: str) -> bool:
        """检查URL是否属于B站域名"""
        try:
            host = urlparse(url).hostname or ""
            host = host.lower().rstrip(".")
            bili_domains = ("bilibili.com", "b23.tv", "bili2233.cn")
            return any(host == d or host.endswith("." + d) for d in bili_domains)
        except Exception:
            return False

    @staticmethod
    def format_count(count: int) -> str:
        """格式化数字为易读形式"""
        if count >= 100000000:
            return f"{count / 100000000:.1f}亿"
        if count >= 10000:
            return f"{count / 10000:.1f}万"
        return str(count)

    def render_video_card(self, info: VideoInfo, show_desc: bool = True, max_desc_len: int = 100) -> str:
        """
        渲染视频信息卡片（纯文本）

        Args:
            info: 视频信息
            show_desc: 是否显示简介
            max_desc_len: 简介最大长度

        Returns:
            格式化的文本卡片
        """
        lines = [
            f"📺 {info.title}",
            f"👤 UP主: {info.uploader}",
        ]

        if show_desc and info.description:
            desc = info.description
            if len(desc) > max_desc_len:
                desc = desc[:max_desc_len] + "..."
            lines.append(f"📝 简介: {desc}")

        # 格式化时长
        minutes = info.duration // 60
        seconds = info.duration % 60
        lines.append(f"⏱️ 时长: {minutes}:{seconds:02d}")

        # 统计数据
        lines.append(
            f"▶️ {self.format_count(info.view_count)}播放  "
            f"💬 {self.format_count(info.danmaku_count)}弹幕  "
            f"👍 {self.format_count(info.like_count)}点赞"
        )

        lines.append(f"🔗 {info.url}")

        return "\n".join(lines)
