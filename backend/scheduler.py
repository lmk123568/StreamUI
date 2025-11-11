import re
import os
from datetime import datetime
from pathlib import Path


def parse_filename_time(filename: str) -> datetime:
    """
    从文件名如 2025-09-22-17-31-15-0.mp4 提取时间
    返回 datetime 对象用于排序
    """
    match = re.match(
        r"(\d{4})-(\d{1,2})-(\d{1,2})-(\d{1,2})-(\d{1,2})-(\d{1,2})", filename
    )
    if match:
        year, month, day, hour, minute, second = map(int, match.groups())
        try:
            return datetime(year, month, day, hour, minute, second)
        except ValueError:
            return datetime.min
    return datetime.min


def cleanup_old_videos(path: Path, keep_videos: int):
    """
    扫描 path 下所有 app/stream，保留最新的 keep_videos 个 .mp4 文件，删除旧的
    """
    print(
        f"[Scheduler {datetime.now()}] 开始扫描 {path} 下所有 app/stream 的视频片段..."
    )

    if not path.exists():
        print(f"[Scheduler Error] ❌ 录像根目录不存在: {path}")
        return

    if not path.is_dir():
        print(f"[Scheduler Error] ❌ 路径不是目录: {path}")
        return

    total_deleted = 0  # 统计总共删除的文件数

    date_pattern = re.compile(r"^\d{4}-\d{2}-\d{2}$")  # 正则匹配

    for app_name in os.listdir(path):
        app_path = path / app_name
        if not app_path.is_dir():
            continue

        for stream_name in os.listdir(app_path):
            stream_path = app_path / stream_name
            if not stream_path.is_dir():
                continue

            for item in os.listdir(stream_path):
                item_path = stream_path / item

                if not item_path.is_dir():
                    continue

                # 使用正则匹配 YYYY-MM-DD
                match = date_pattern.match(item)
                if not match:
                    continue

                video_files = []
                for file_path in stream_path.rglob("*.mp4"):
                    video_files.append(file_path)

                if len(video_files) <= keep_videos:
                    continue

                # 按文件名中的时间排序（新 → 旧）
                sorted_files = sorted(
                    video_files,
                    key=lambda f: parse_filename_time(f.name),
                    reverse=True,
                )

                # 要删除的是：从第 keep_videos 个开始的所有文件
                files_to_delete = sorted_files[keep_videos:]

                for file_path in files_to_delete:
                    try:
                        file_path.unlink()
                        relative_path = file_path.relative_to(path)
                        print(
                            f"[Scheduler {datetime.now()}] 🗑️ 删除旧片段: {relative_path}"
                        )
                        total_deleted += 1

                    except Exception as e:
                        print(f"[Scheduler Error] ❌ 删除失败 {file_path}: {e}")

    print(
        f"[Scheduler {datetime.now()}] ✅ 扫描与清理完成，共删除 {total_deleted} 个旧视频片段。"
    )
