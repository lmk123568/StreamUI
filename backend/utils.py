import json
import os
import re
import subprocess
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

TZ_SHANGHAI = ZoneInfo("Asia/Shanghai")


def parse_timestamp_to_shanghai(time_str: str) -> datetime | None:
    """
    解析 creation_time 并转换为 Asia/Shanghai 时间
    """
    if not time_str:
        return None
    try:
        if time_str.endswith("Z"):
            dt = datetime.fromisoformat(time_str[:-1] + "+00:00")
        else:
            dt = datetime.fromisoformat(time_str)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=TZ_SHANGHAI)
        return dt.astimezone(TZ_SHANGHAI)
    except Exception as e:
        print(f"❌ 时间解析失败: {e}")
        return None


def get_video_shanghai_time(video_path: Path) -> dict | None:
    """
    提取单个视频在 Asia/Shanghai 时区的时间段
    Returns: { filepath, duration, start, end } 或 None
    """
    cmd = [
        "ffprobe",
        "-v",
        "quiet",
        "-show_format",
        "-print_format",
        "json",
        str(video_path),
    ]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        if result.returncode != 0:
            print(f"❌ ffprobe 失败: {video_path}")
            return None

        info = json.loads(result.stdout)
        fmt = info.get("format", {})
        tags = fmt.get("tags", {})

        # 1. 获取 creation_time 并转为北京时间
        creation_time_str = tags.get("creation_time")
        start_sh = parse_timestamp_to_shanghai(creation_time_str)
        if not start_sh:
            print(f"⚠️ 无效 creation_time: {video_path}")
            return None

        # 2. 获取视频时长
        duration_str = fmt.get("duration")
        if not duration_str:
            return None
        try:
            duration = float(duration_str)
        except ValueError:
            return None

        end_sh = start_sh + timedelta(seconds=duration)

        return {
            "filename": video_path,  # 绝对路径
            "duration": round(duration, 3),
            "start": start_sh.isoformat(),
            "end": end_sh.isoformat(),
        }
    except Exception as e:
        print(f"❌ 处理失败 {video_path}: {e}")
        return None


def get_video_shanghai_time_from_filename(
    video_path: Path, *, default_duration_seconds: float = 300.0
) -> dict | None:
    filename = video_path.name
    match = re.match(
        r"(\d{4})-(\d{1,2})-(\d{1,2})-(\d{1,2})-(\d{1,2})-(\d{1,2})",
        filename,
    )
    if not match:
        return None
    year, month, day, hour, minute, second = map(int, match.groups())
    try:
        start_sh = datetime(year, month, day, hour, minute, second, tzinfo=TZ_SHANGHAI)
    except ValueError:
        return None
    duration = float(default_duration_seconds)
    end_sh = start_sh + timedelta(seconds=duration)
    return {
        "filename": video_path,
        "duration": round(duration, 3),
        "start": start_sh.isoformat(),
        "end": end_sh.isoformat(),
    }


def get_zlm_secret(file_path: str) -> str:
    """从配置文件中获取 zlm 的 secret"""

    if not os.path.exists(file_path):
        raise FileNotFoundError(f"配置文件不存在: {file_path}")

    with open(file_path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()

            # 跳过空行和注释（以 # 或 ; 开头）
            if not line or line.startswith("#") or line.startswith(";"):
                continue

            # 检查是否是 secret 配置
            if line.startswith("secret"):
                try:
                    secret = line.split("=", 1)[1].strip()  # 分割一次，取后面
                    if not secret:
                        raise ValueError("secret 值不能为空")
                    return secret
                except IndexError:
                    raise ValueError("secret 配置格式错误，应为 secret=xxx")

    # 如果没找到 secret
    raise ValueError(f"在配置文件中未找到 'secret' 配置项: {file_path}")


def summarize_existing_recordings(
    *,
    record_root: Path,
    app: str,
    stream: str,
) -> dict | None:
    base_dir = record_root / app / stream
    if not base_dir.exists() or not base_dir.is_dir():
        return None

    date_pattern = re.compile(r"^\d{4}-\d{2}-\d{2}$")
    dates: list[str] = []
    slice_num = 0
    total_size_bytes = 0

    try:
        for item in base_dir.iterdir():
            if not item.is_dir():
                continue
            if not date_pattern.match(item.name):
                continue
            try:
                mp4_files = [
                    p
                    for p in item.iterdir()
                    if p.is_file() and p.suffix.lower() == ".mp4"
                ]
            except Exception:
                continue
            if not mp4_files:
                continue
            dates.append(item.name)
            slice_num += len(mp4_files)
            for p in mp4_files:
                try:
                    total_size_bytes += p.stat().st_size
                except Exception:
                    continue
    except Exception:
        return None

    if slice_num <= 0 or not dates:
        return None

    dates.sort()
    date_from = dates[0]
    date_to = dates[-1]
    return {
        "has_old_recordings": True,
        "app": app,
        "stream": stream,
        "slice_num": slice_num,
        "total_storage_gb": round(total_size_bytes / (1024**3), 2),
        "date_from": date_from,
        "date_to": date_to,
        "date_count": len(dates),
    }
