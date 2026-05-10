"""
缓存管理工具
自动清理过期的视频文件和图片
"""

import os
import time
from pathlib import Path
from typing import List
from src.common.logger import get_logger

logger = get_logger("bilivideo_cache")


class CacheManager:
    """缓存管理器"""

    def __init__(self, cache_dirs: List[str], max_age_hours: int = 24):
        """
        初始化缓存管理器

        Args:
            cache_dirs: 缓存目录列表
            max_age_hours: 文件最大保留时间（小时）
        """
        self.cache_dirs = [Path(d) for d in cache_dirs]
        self.max_age_seconds = max_age_hours * 3600

    def clean_expired_files(self) -> int:
        """
        清理过期文件

        Returns:
            清理的文件数量
        """
        cleaned_count = 0
        current_time = time.time()

        for cache_dir in self.cache_dirs:
            if not cache_dir.exists():
                continue

            try:
                for file_path in cache_dir.iterdir():
                    if not file_path.is_file():
                        continue

                    # 检查文件年龄
                    file_age = current_time - file_path.stat().st_mtime
                    if file_age > self.max_age_seconds:
                        try:
                            file_path.unlink()
                            cleaned_count += 1
                            logger.debug(f"已清理过期文件: {file_path.name}")
                        except Exception as e:
                            logger.warning(f"清理文件失败 {file_path}: {e}")

            except Exception as e:
                logger.error(f"清理目录失败 {cache_dir}: {e}")

        if cleaned_count > 0:
            logger.info(f"缓存清理完成，共清理 {cleaned_count} 个文件")

        return cleaned_count

    def get_cache_size(self) -> int:
        """
        获取缓存总大小（字节）

        Returns:
            缓存大小
        """
        total_size = 0

        for cache_dir in self.cache_dirs:
            if not cache_dir.exists():
                continue

            try:
                for file_path in cache_dir.iterdir():
                    if file_path.is_file():
                        total_size += file_path.stat().st_size
            except Exception as e:
                logger.error(f"计算缓存大小失败 {cache_dir}: {e}")

        return total_size

    def get_cache_info(self) -> dict:
        """
        获取缓存信息

        Returns:
            缓存信息字典
        """
        file_count = 0
        total_size = 0

        for cache_dir in self.cache_dirs:
            if not cache_dir.exists():
                continue

            try:
                for file_path in cache_dir.iterdir():
                    if file_path.is_file():
                        file_count += 1
                        total_size += file_path.stat().st_size
            except Exception as e:
                logger.error(f"获取缓存信息失败 {cache_dir}: {e}")

        return {
            "file_count": file_count,
            "total_size_bytes": total_size,
            "total_size_mb": round(total_size / 1024 / 1024, 2),
        }

    def clear_all_cache(self) -> int:
        """
        清空所有缓存

        Returns:
            清理的文件数量
        """
        cleaned_count = 0

        for cache_dir in self.cache_dirs:
            if not cache_dir.exists():
                continue

            try:
                for file_path in cache_dir.iterdir():
                    if file_path.is_file():
                        try:
                            file_path.unlink()
                            cleaned_count += 1
                        except Exception as e:
                            logger.warning(f"清理文件失败 {file_path}: {e}")
            except Exception as e:
                logger.error(f"清空缓存目录失败 {cache_dir}: {e}")

        logger.info(f"已清空所有缓存，共清理 {cleaned_count} 个文件")
        return cleaned_count
