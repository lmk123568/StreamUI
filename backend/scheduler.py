import re
from datetime import datetime
from pathlib import Path

from db import list_record_policies


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


def cleanup_old_videos(path: Path):
    """
    扫描 path 下所有 app/stream，按数据库中配置的保留天数删除旧的 .mp4 片段
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

    try:
        rows = list_record_policies(enabled_only=True)
    except Exception:
        rows = []
    if not rows:
        print(f"[Scheduler {datetime.now()}] 未发现启用的录像保留策略，跳过清理。")
        return

    total_deleted = 0

    date_pattern = re.compile(r"^\d{4}-\d{2}-\d{2}$")  # 正则匹配
    safe_seconds = 15 * 60

    for row in rows:
        app_name = str(row.get("app", "")).strip()
        stream_name = str(row.get("stream", "")).strip()
        if not (app_name and stream_name):
            continue

        try:
            retention_days = int(row.get("retention_days", 0) or 0)
        except Exception:
            retention_days = 0
        if retention_days <= 0:
            continue

        keep_videos = retention_days * 24 * 12
        stream_path = path / app_name / stream_name
        if not stream_path.exists() or not stream_path.is_dir():
            continue

        video_files: list[Path] = []
        now_ts = datetime.now().timestamp()
        for p in stream_path.rglob("*.mp4"):
            if not p.is_file():
                continue
            if p.name.startswith("."):
                continue
            try:
                if now_ts - p.stat().st_mtime < safe_seconds:
                    continue
            except Exception:
                continue
            if parse_filename_time(p.name) == datetime.min:
                continue
            video_files.append(p)
        if len(video_files) <= keep_videos:
            continue

        sorted_files = sorted(
            video_files,
            key=lambda f: parse_filename_time(f.name),
            reverse=True,
        )
        for file_path in sorted_files[keep_videos:]:
            try:
                file_path.unlink()
                relative_path = file_path.relative_to(path)
                print(f"[Scheduler {datetime.now()}] 🗑️ 删除旧片段: {relative_path}")
                total_deleted += 1
            except Exception as e:
                print(f"[Scheduler Error] ❌ 删除失败 {file_path}: {e}")

        for item in stream_path.iterdir():
            if not item.is_dir():
                continue
            if not date_pattern.match(item.name):
                continue
            try:
                has_mp4 = any(
                    p.is_file() and p.suffix.lower() == ".mp4" for p in item.iterdir()
                )
                if not has_mp4:
                    item.rmdir()
            except Exception:
                continue

    print(
        f"[Scheduler {datetime.now()}] ✅ 扫描与清理完成，共删除 {total_deleted} 个旧视频片段。"
    )
