"""
B站视频信息卡片渲染器
使用 Jinja2 + Playwright 渲染 HTML 卡片为图片
样式参考完整B站分享卡片：标头+封面+统计胶囊+UP主+简介+弹幕+热评
"""

import asyncio
import base64
import os
import uuid
from datetime import datetime
from pathlib import Path
from typing import Optional, List
import httpx

from src.common.logger import get_logger
from .link_resolver import VideoInfo, HotComment

logger = get_logger("bilivideo_card")

_BILI_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/146.0",
    "Referer": "https://www.bilibili.com/",
}

# 模板目录
_TEMPLATE_DIR = Path(__file__).resolve().parent.parent / "render"
_TEMPLATE_FILE = "bili_card.html"

# 全局 playwright 单例（避免每次启动浏览器开销）
_pw_lock = asyncio.Lock()
_pw_instance = None
_pw_browser = None


async def _ensure_browser():
    """启动并维护 playwright 浏览器单例"""
    global _pw_instance, _pw_browser
    from playwright.async_api import async_playwright

    if _pw_instance is None:
        _pw_instance = await async_playwright().start()
    if _pw_browser is None or not _pw_browser.is_connected():
        _pw_browser = await _pw_instance.chromium.launch()
    return _pw_browser


async def shutdown_browser():
    """关闭浏览器（插件卸载时调用）"""
    global _pw_instance, _pw_browser
    try:
        if _pw_browser:
            await _pw_browser.close()
    except Exception:
        pass
    try:
        if _pw_instance:
            await _pw_instance.stop()
    except Exception:
        pass
    _pw_browser = None
    _pw_instance = None


class BiliCardRenderer:
    """B站视频卡片渲染器（HTML转图片）"""

    def __init__(self, save_path: str = "./data/bilivideo/cards"):
        self.save_path = Path(save_path)
        self.save_path.mkdir(parents=True, exist_ok=True)
        self._template_str: Optional[str] = None

    def _load_template(self) -> str:
        """加载HTML模板"""
        if self._template_str is not None:
            return self._template_str
        path = _TEMPLATE_DIR / _TEMPLATE_FILE
        if not path.exists():
            raise FileNotFoundError(f"卡片模板不存在: {path}")
        with open(path, "r", encoding="utf-8") as f:
            self._template_str = f.read()
        return self._template_str

    async def render_card(self, info: VideoInfo) -> Optional[Path]:
        """
        渲染视频信息卡片为图片

        Args:
            info: 视频信息

        Returns:
            图片路径，失败返回None
        """
        try:
            # 1. 并行下载所有图片资源
            cover_task = self._fetch_image_data_uri(info.cover_url)
            uploader_avatar_task = self._fetch_image_data_uri(info.uploader_avatar)
            comment_avatar_tasks = [
                self._fetch_image_data_uri(c.user_avatar) for c in info.hot_comments
            ]

            tasks = [cover_task, uploader_avatar_task] + comment_avatar_tasks
            results = await asyncio.gather(*tasks, return_exceptions=True)

            cover_data_uri = results[0] if not isinstance(results[0], Exception) else None
            uploader_avatar_uri = results[1] if not isinstance(results[1], Exception) else None
            comment_avatar_uris = []
            for r in results[2:]:
                comment_avatar_uris.append(r if not isinstance(r, Exception) else None)

            # 2. 准备模板数据
            template_data = self._build_template_data(
                info, cover_data_uri, uploader_avatar_uri, comment_avatar_uris
            )

            # 3. 渲染HTML
            html = self._render_html(template_data)

            # 4. 截图
            png_bytes = await self._screenshot(html)
            if not png_bytes:
                return None

            # 5. 保存
            card_path = self.save_path / f"{info.bvid}_card_{uuid.uuid4().hex[:6]}.png"
            await asyncio.to_thread(card_path.write_bytes, png_bytes)
            return card_path

        except Exception as e:
            logger.error(f"渲染卡片失败: {e}")
            import traceback
            logger.error(traceback.format_exc())
            return None

    def _render_html(self, data: dict) -> str:
        """用Jinja2渲染HTML"""
        try:
            import jinja2
            env = jinja2.Environment(autoescape=True)
            template = env.from_string(self._load_template())
            return template.render(**data)
        except ImportError:
            raise RuntimeError("缺少jinja2，请安装: pip install jinja2")

    def _build_template_data(
        self,
        info: VideoInfo,
        cover_data_uri: Optional[str],
        uploader_avatar_uri: Optional[str],
        comment_avatar_uris: List[Optional[str]],
    ) -> dict:
        """构造模板渲染参数"""
        # 时长
        duration_text = ""
        if info.duration > 0:
            mins = info.duration // 60
            secs = info.duration % 60
            duration_text = f"{mins}:{secs:02d}"

        # 发布日期
        pubdate_text = ""
        if info.pubdate:
            try:
                pubdate_text = datetime.fromtimestamp(info.pubdate).strftime("%Y-%m-%d %H:%M:%S")
            except Exception:
                pubdate_text = ""

        # 简介
        desc = (info.description or "").strip().replace("\r\n", "\n")
        if desc == "-":
            desc = ""

        # 处理热评
        hot_comments_data = []
        for idx, c in enumerate(info.hot_comments):
            content = (c.content or "").strip()
            if not content:
                continue
            # 截断过长的评论
            if len(content) > 120:
                content = content[:120] + "..."

            comment_pubdate = ""
            if c.pubdate:
                try:
                    comment_pubdate = datetime.fromtimestamp(c.pubdate).strftime("%Y-%m-%d %H:%M:%S")
                except Exception:
                    pass

            avatar_uri = comment_avatar_uris[idx] if idx < len(comment_avatar_uris) else None

            hot_comments_data.append({
                "user_name": c.user_name,
                "name_short": (c.user_name[:1] if c.user_name else "U").upper(),
                "user_level": min(max(c.user_level, 0), 6),
                "content": content,
                "like_text": self._format_count(c.like_count),
                "pubdate_text": comment_pubdate,
                "avatar_uri": avatar_uri,
            })

        return {
            "bvid": info.bvid,
            "title": info.title or "未知标题",
            "uploader": info.uploader or "未知UP主",
            "uploader_fans_text": self._format_count(info.uploader_fans) if info.uploader_fans else "",
            "uploader_avatar_uri": uploader_avatar_uri or "",
            "desc": desc,
            "duration_text": duration_text,
            "pubdate_text": pubdate_text,
            "cover_data_uri": cover_data_uri or "",
            "view_count": self._format_count(info.view_count) + "播放",
            "danmaku_count": self._format_count(info.danmaku_count),
            "like_count": self._format_count(info.like_count),
            "coin_count": self._format_count(info.coin_count),
            "favorite_count": self._format_count(info.favorite_count),
            "share_count": self._format_count(info.share_count),
            "danmaku_samples": info.danmaku_samples or [],
            "hot_comments": hot_comments_data,
        }

    async def _fetch_image_data_uri(self, image_url: str) -> Optional[str]:
        """下载图片并转为data URI"""
        if not image_url:
            return None
        if not image_url.startswith("http"):
            image_url = "https:" + image_url
        try:
            async with httpx.AsyncClient(timeout=10.0, headers=_BILI_HEADERS) as client:
                response = await client.get(image_url)
                if response.status_code != 200:
                    return None
                content_type = response.headers.get("content-type", "image/jpeg").split(";")[0].strip()
                # 兜底
                if "image" not in content_type:
                    content_type = "image/jpeg"
                b64 = base64.b64encode(response.content).decode("ascii")
                return f"data:{content_type};base64,{b64}"
        except Exception as e:
            logger.warning(f"下载图片失败 {image_url[:80]}: {e}")
            return None

    async def _screenshot(self, html: str) -> Optional[bytes]:
        """使用playwright截图"""
        try:
            async with _pw_lock:
                browser = await _ensure_browser()

            context = await browser.new_context(
                device_scale_factor=2.0,
                viewport={"width": 900, "height": 1600},
            )
            page = await context.new_page()
            try:
                await page.set_content(html, wait_until="networkidle", timeout=15000)

                # 等待图片加载完成
                try:
                    await page.evaluate(
                        """
                        Promise.all(Array.from(document.images).map(img => {
                            if (img.complete) return Promise.resolve();
                            return new Promise(resolve => {
                                img.onload = resolve;
                                img.onerror = resolve;
                            });
                        }))
                        """
                    )
                except Exception:
                    pass

                await page.wait_for_timeout(200)

                # 截取卡片元素
                card = await page.query_selector(".bili-card")
                if card:
                    return await card.screenshot(type="png")
                else:
                    return await page.screenshot(full_page=True, type="png")
            finally:
                await page.close()
                await context.close()
        except Exception as e:
            logger.error(f"playwright截图失败: {e}")
            import traceback
            logger.error(traceback.format_exc())
            return None

    @staticmethod
    def _format_count(count) -> str:
        """格式化数字"""
        if count is None:
            return "0"
        try:
            count = int(count)
        except (ValueError, TypeError):
            return "0"
        if count >= 100000000:
            return f"{count / 100000000:.1f}亿"
        if count >= 10000:
            return f"{count / 10000:.1f}万"
        return str(count)
