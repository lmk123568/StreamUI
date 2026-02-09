import re
from pathlib import Path


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
                mp4_files = [p for p in item.iterdir() if p.is_file() and p.suffix.lower() == ".mp4"]
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

