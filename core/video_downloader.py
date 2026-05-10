"""
B站视频下载器
整合自 astrbot_plugin_link_resolver
功能：下载视频、合并音视频流
"""

import asyncio
import os
import re
import shutil
import sys
import uuid
from pathlib import Path
from typing import Optional, Tuple
import httpx
from bilibili_api import Credential, video
from bilibili_api.video import (
    AudioStreamDownloadURL,
    VideoCodecs,
    VideoDownloadURLDataDetecter,
    VideoQuality,
    VideoStreamDownloadURL,
)

from src.common.logger import get_logger

logger = get_logger("bilivideo_downloader")

_BILI_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/146.0.7680.31 Safari/537.36"
)
_BILI_HEADERS = {
    "User-Agent": _BILI_UA,
    "Referer": "https://www.bilibili.com/",
}

# ffmpeg 可执行文件路径缓存
_FFMPEG_PATH: Optional[str] = None


def _resolve_ffmpeg_path() -> Optional[str]:
    """
    查找 ffmpeg 可执行文件路径

    顺序：
    1. 环境变量 BILIVIDEO_FFMPEG
    2. 系统 PATH
    3. MaiBot 自带的 napcat ffmpeg
    """
    global _FFMPEG_PATH
    if _FFMPEG_PATH is not None:
        return _FFMPEG_PATH or None

    # 1. 环境变量
    env_path = os.environ.get("BILIVIDEO_FFMPEG", "").strip()
    if env_path and Path(env_path).exists():
        _FFMPEG_PATH = env_path
        logger.info(f"使用环境变量指定的ffmpeg: {env_path}")
        return _FFMPEG_PATH

    # 2. 系统PATH
    found = shutil.which("ffmpeg")
    if found:
        _FFMPEG_PATH = found
        logger.info(f"使用系统PATH中的ffmpeg: {found}")
        return _FFMPEG_PATH

    # 3. 在 MaiBot OneKey 目录树中查找 napcat 自带的 ffmpeg
    candidates = []
    # 从 sys.executable 反推 MaiBotOneKey 根目录
    try:
        cwd = Path.cwd()
        # 向上找 MaiBotOneKey
        for parent in [cwd, *cwd.parents]:
            if (parent / "modules").exists():
                # 找 napcat / napcatframework
                for sub in ("napcat", "napcatframework"):
                    base = parent / "modules" / sub / "versions"
                    if base.exists():
                        # 取最新版本
                        versions = sorted([v for v in base.iterdir() if v.is_dir()], reverse=True)
                        for v in versions:
                            ff = v / "resources" / "app" / "napcat" / "ffmpeg" / "ffmpeg.exe"
                            if ff.exists():
                                candidates.append(str(ff))
                                break
                if candidates:
                    break
    except Exception as e:
        logger.debug(f"搜索napcat ffmpeg失败: {e}")

    if candidates:
        _FFMPEG_PATH = candidates[0]
        logger.info(f"使用napcat自带的ffmpeg: {candidates[0]}")
        return _FFMPEG_PATH

    _FFMPEG_PATH = ""  # 标记已搜索过
    logger.warning(
        "找不到 ffmpeg，DASH流视频无法合并。"
        "解决方案：1) 安装 ffmpeg 并加入PATH；"
        "2) 设置环境变量 BILIVIDEO_FFMPEG=ffmpeg.exe完整路径"
    )
    return None


class SizeLimitExceeded(Exception):
    """文件大小超过限制"""


QUALITY_MAP = {
    "360P": VideoQuality._360P,
    "480P": VideoQuality._480P,
    "720P": VideoQuality._720P,
    "1080P": VideoQuality._1080P,
    "1080P+": VideoQuality._1080P_PLUS,
    "1080P60": VideoQuality._1080P_60,
    "4K": VideoQuality._4K,
}


class BiliVideoDownloader:
    """B站视频下载器"""

    def __init__(
        self,
        credential: Optional[Credential] = None,
        save_path: str = "./data/bilivideo/videos",
        max_size_mb: int = 100,
        allow_quality_fallback: bool = True,
    ):
        self.credential = credential or Credential()
        self.save_path = Path(save_path)
        self.save_path.mkdir(parents=True, exist_ok=True)
        self.max_size_mb = max_size_mb
        self.max_bytes = max_size_mb * 1024 * 1024 if max_size_mb > 0 else None
        self.allow_quality_fallback = allow_quality_fallback

    async def download_video(
        self,
        bvid: str,
        quality: str = "720P",
        page_index: int = 0,
    ) -> Optional[Path]:
        """
        下载视频

        Args:
            bvid: 视频BV号
            quality: 画质
            page_index: 分P索引

        Returns:
            视频文件路径，失败返回None
        """
        try:
            v = video.Video(bvid=bvid, credential=self.credential)
            target_quality = QUALITY_MAP.get(quality.upper(), VideoQuality._720P)

            # 选择视频流
            current_quality = target_quality
            request_id = uuid.uuid4().hex[:8]
            output_path = self.save_path / f"{bvid}_{request_id}.mp4"

            while True:
                try:
                    video_stream, audio_stream, size_mb = await self._select_streams(
                        v, page_index, current_quality
                    )
                except RuntimeError as e:
                    logger.error(f"选择视频流失败: {e}")
                    return None

                # 检查大小
                if (
                    size_mb is not None
                    and self.max_bytes is not None
                    and size_mb > self.max_size_mb
                ):
                    if self.allow_quality_fallback:
                        lower = self._get_lower_quality(current_quality)
                        if lower:
                            logger.warning(
                                f"画质 {current_quality.name} 超限 ({size_mb:.2f}MB > {self.max_size_mb}MB)，降至 {lower.name}"
                            )
                            current_quality = lower
                            continue
                    logger.warning(f"视频大小超限: {size_mb:.2f}MB > {self.max_size_mb}MB")
                    return None

                break

            # 下载
            video_url = video_stream.url
            audio_url = audio_stream.url if audio_stream else None

            if audio_url:
                # DASH 流，需要分别下载并合并
                temp_video = output_path.with_suffix(".video")
                temp_audio = output_path.with_suffix(".audio")
                try:
                    await self._download_stream(video_url, temp_video)
                    await self._download_stream(audio_url, temp_audio)
                    await self._merge_av(temp_video, temp_audio, output_path)
                except Exception as e:
                    logger.error(f"下载/合并失败: {e}")
                    for p in [temp_video, temp_audio, output_path]:
                        try:
                            p.unlink(missing_ok=True)
                        except Exception:
                            pass
                    return None
            else:
                # FLV 单流
                try:
                    await self._download_stream(video_url, output_path)
                except Exception as e:
                    logger.error(f"下载失败: {e}")
                    output_path.unlink(missing_ok=True)
                    return None

            if not output_path.exists() or output_path.stat().st_size == 0:
                return None

            logger.info(f"视频下载完成: {output_path.name} ({output_path.stat().st_size / 1024 / 1024:.2f}MB)")
            return output_path

        except Exception as e:
            logger.error(f"下载视频失败 {bvid}: {e}")
            import traceback
            logger.error(traceback.format_exc())
            return None

    async def _select_streams(
        self,
        video_obj: video.Video,
        page_index: int,
        target_quality: VideoQuality,
    ) -> Tuple[VideoStreamDownloadURL, Optional[AudioStreamDownloadURL], Optional[float]]:
        """选择视频和音频流"""
        download_url_data = await video_obj.get_download_url(page_index=page_index)
        detecter = VideoDownloadURLDataDetecter(download_url_data)
        streams = detecter.detect_best_streams(
            video_max_quality=target_quality,
            codecs=[VideoCodecs.AVC],
            no_dolby_video=True,
            no_hdr=True,
        )
        if not streams:
            raise RuntimeError("未找到可下载的视频流")

        video_stream = streams[0]
        if not isinstance(video_stream, VideoStreamDownloadURL):
            raise RuntimeError("未找到有效的视频流")

        audio_stream = None
        if len(streams) > 1 and isinstance(streams[1], AudioStreamDownloadURL):
            audio_stream = streams[1]

        # 估算大小
        size_mb = self._estimate_size(download_url_data, video_stream, audio_stream)

        return video_stream, audio_stream, size_mb

    def _estimate_size(
        self,
        download_url_data: dict,
        video_stream: VideoStreamDownloadURL,
        audio_stream: Optional[AudioStreamDownloadURL],
    ) -> Optional[float]:
        """从API数据估算文件大小（MB）"""
        try:
            dash = download_url_data.get("dash")
            if not dash:
                return None

            timelength_ms = download_url_data.get("timelength")
            if not timelength_ms:
                return None
            timelength_sec = timelength_ms / 1000

            total_bandwidth = 0

            video_url = video_stream.url
            for v in dash.get("video", []):
                v_url = v.get("baseUrl") or v.get("base_url", "")
                if v_url == video_url:
                    total_bandwidth += v.get("bandwidth", 0)
                    break

            if audio_stream:
                audio_url = audio_stream.url
                for a in dash.get("audio", []):
                    a_url = a.get("baseUrl") or a.get("base_url", "")
                    if a_url == audio_url:
                        total_bandwidth += a.get("bandwidth", 0)
                        break

            if total_bandwidth == 0:
                return None

            size_bytes = total_bandwidth * timelength_sec / 8
            return size_bytes / 1024 / 1024
        except Exception:
            return None

    @staticmethod
    def _get_lower_quality(current: VideoQuality) -> Optional[VideoQuality]:
        """获取更低画质"""
        candidates = sorted(
            [q for q in VideoQuality if q.value < current.value],
            key=lambda q: q.value,
            reverse=True,
        )
        return candidates[0] if candidates else None

    async def _download_stream(
        self,
        url: str,
        output_path: Path,
        retries: int = 3,
    ) -> int:
        """下载流到文件"""
        temp_path = output_path.with_suffix(output_path.suffix + ".part")
        last_error: Optional[Exception] = None

        for attempt in range(retries):
            try:
                async with httpx.AsyncClient(
                    timeout=None,
                    headers=_BILI_HEADERS,
                ) as client:
                    async with client.stream("GET", url, follow_redirects=True) as response:
                        response.raise_for_status()
                        content_length = response.headers.get("Content-Length")
                        if content_length and self.max_bytes and int(content_length) > self.max_bytes:
                            raise SizeLimitExceeded("超过大小限制")

                        bytes_written = 0
                        with open(temp_path, "wb") as f:
                            async for chunk in response.aiter_bytes(1024 * 1024):
                                if not chunk:
                                    continue
                                bytes_written += len(chunk)
                                if self.max_bytes and bytes_written > self.max_bytes:
                                    raise SizeLimitExceeded("超过大小限制")
                                await asyncio.to_thread(f.write, chunk)

                await asyncio.to_thread(temp_path.replace, output_path)
                return bytes_written

            except SizeLimitExceeded:
                if temp_path.exists():
                    await asyncio.to_thread(temp_path.unlink, missing_ok=True)
                raise
            except Exception as e:
                last_error = e
                if temp_path.exists():
                    await asyncio.to_thread(temp_path.unlink, missing_ok=True)
                if attempt < retries - 1:
                    await asyncio.sleep(2**attempt)

        if last_error:
            raise last_error
        raise RuntimeError("下载失败")

    @staticmethod
    async def _merge_av(v_path: Path, a_path: Path, output_path: Path) -> None:
        """使用ffmpeg合并音视频"""
        ffmpeg = _resolve_ffmpeg_path()
        if not ffmpeg:
            raise RuntimeError("未找到ffmpeg可执行文件，无法合并音视频")

        cmd = [
            ffmpeg,
            "-y",
            "-i", str(v_path),
            "-i", str(a_path),
            "-c", "copy",
            "-map", "0:v:0",
            "-map", "1:a:0",
            str(output_path),
        ]
        try:
            process = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            _, stderr = await process.communicate()
            if process.returncode != 0:
                err = stderr.decode(errors='ignore').strip()
                # 截短防止刷屏
                if len(err) > 500:
                    err = "..." + err[-500:]
                raise RuntimeError(f"ffmpeg合并失败 (returncode={process.returncode}): {err}")
        finally:
            await asyncio.to_thread(v_path.unlink, missing_ok=True)
            await asyncio.to_thread(a_path.unlink, missing_ok=True)

    @staticmethod
    async def download_cover(cover_url: str, save_path: Path) -> Optional[Path]:
        """下载视频封面"""
        if not cover_url:
            return None
        try:
            async with httpx.AsyncClient(timeout=15.0, headers=_BILI_HEADERS) as client:
                response = await client.get(cover_url)
                if response.status_code == 200:
                    save_path.parent.mkdir(parents=True, exist_ok=True)
                    await asyncio.to_thread(save_path.write_bytes, response.content)
                    return save_path
        except Exception as e:
            logger.warning(f"下载封面失败: {e}")
        return None
