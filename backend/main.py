import os
import re
import shutil
import asyncio
import time
import csv
import io
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import httpx
import docker
import psutil
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger
from fastapi import FastAPI, Query, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from db import delete_pull_proxy as db_delete_pull_proxy
from db import delete_record_policy as db_delete_record_policy
from db import get_record_policy as db_get_record_policy
from db import init_db as db_init
from db import list_pull_proxies as db_list_pull_proxies
from db import list_record_policies as db_list_record_policies
from db import upsert_record_policy as db_upsert_record_policy
from db import upsert_pull_proxy as db_upsert_pull_proxy
from scheduler import cleanup_old_videos
from utils import get_video_shanghai_time, get_zlm_secret, summarize_existing_recordings

# =========================================================
# zlmediakit 地址
ZLM_SERVER = "http://127.0.0.1:8080"
# zlmediakit 密钥
ZLM_SECRET = get_zlm_secret("/opt/media/conf/config.ini")
# zlmediakit 录像
RECORD_ROOT = Path("/opt/media/bin/www/record")
# 容器
ZLM_CONTAINER_NAME = os.getenv("ZLM_CONTAINER_NAME", "zlm-server")
STREAMUI_CONTAINER_NAME = os.getenv("STREAMUI_CONTAINER_NAME", "streamui-web-server")
# =========================================================

_last_record_start_attempt: dict[tuple[str, str, str], float] = {}


async def ensure_recording_from_policies() -> None:
    try:
        policies = db_list_record_policies(enabled_only=True) or []
    except Exception:
        return
    if not policies:
        return

    try:
        response = await client.get(
            f"{ZLM_SERVER}/index/api/getMediaList", params={"secret": ZLM_SECRET}
        )
        raw = response.json()
        if raw.get("code") != 0:
            return
    except Exception:
        return

    media_map: dict[tuple[str, str, str], dict] = {}
    for media in raw.get("data", []) or []:
        if not isinstance(media, dict):
            continue
        vhost = str(media.get("vhost") or "__defaultVhost__")
        app = str(media.get("app") or "")
        stream = str(media.get("stream") or "")
        if not (app and stream):
            continue
        key = (vhost, app, stream)
        agg = media_map.get(key) or {"isRecordingMP4": False}
        if media.get("isRecordingMP4"):
            agg["isRecordingMP4"] = True
        media_map[key] = agg

    now = time.time()
    for policy in policies:
        vhost = str(policy.get("vhost") or "__defaultVhost__")
        app = str(policy.get("app") or "")
        stream = str(policy.get("stream") or "")
        if not (app and stream):
            continue
        key = (vhost, app, stream)
        state = media_map.get(key)
        if not state:
            continue
        if state.get("isRecordingMP4"):
            continue

        last = _last_record_start_attempt.get(key, 0.0)
        if now - last < 60:
            continue
        _last_record_start_attempt[key] = now

        try:
            await client.get(
                f"{ZLM_SERVER}/index/api/startRecord",
                params={
                    "secret": ZLM_SECRET,
                    "vhost": vhost,
                    "app": app,
                    "stream": stream,
                    "type": "1",
                    "max_second": "300",
                },
            )
        except Exception:
            continue


@asynccontextmanager
async def lifespan(app: FastAPI):
    scheduler = AsyncIOScheduler()

    db_init()
    asyncio.create_task(sync_pull_proxies_from_db())

    # 添加任务：每小时整点执行
    scheduler.add_job(
        cleanup_old_videos,
        kwargs={"path": RECORD_ROOT},
        trigger=CronTrigger(minute=0),
        id="cleanup_videos",
        name="清理旧视频片段",
        replace_existing=True,
    )
    scheduler.add_job(
        ensure_recording_from_policies,
        trigger=IntervalTrigger(seconds=30),
        id="ensure_recording",
        name="保障录像自动续录",
        replace_existing=True,
        max_instances=1,
        coalesce=True,
    )

    # 只有在这里，事件循环已经启动，可以安全 start
    scheduler.start()
    print("[Scheduler] 🚀 定时任务已启动")

    yield

    scheduler.shutdown()
    await client.aclose()
    print("[Scheduler] 🛑 定时任务已取消")


t = """
| 端口  | 协议    | 服务                            |
| ----- | ------- | ------------------------------- |
| 10800 | TCP     | StreamUI frontend                    |
| 10801 | TCP     | StreamUI backend               |
| 1935  | TCP     | RTMP 推流拉流                   |
| 8080  | TCP     | FLV、HLS、TS、fMP4、WebRTC 支持 |
| 8443  | TCP     | HTTPS、WebSocket 支持           |
| 8554  | TCP     | RTSP 服务端口                   |
| 10000 | TCP/UDP | RTP、RTCP 端口                  |
| 8000  | UDP     | WebRTC ICE/STUN 端口            |
| 9000  | UDP     | WebRTC 辅助端口                 |

"""

app = FastAPI(
    title="接口文档",
    version="latest",
    description=t,
    lifespan=lifespan,
)

# 设置 CORS
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


client = httpx.AsyncClient(
    timeout=5.0,
    limits=httpx.Limits(
        max_connections=10,
        max_keepalive_connections=20,
    ),
)


def _audio_type_to_zlm_params(audio_type: int | None) -> dict[str, str]:
    if audio_type == 0:
        return {"enable_audio": "0", "add_mute_audio": "0"}
    if audio_type == 1:
        return {"enable_audio": "1", "add_mute_audio": "0"}
    if audio_type == 2:
        return {"enable_audio": "1", "add_mute_audio": "1"}
    return {}


def _stream_proxy_key(vhost: str, app: str, stream: str) -> str:
    return f"{vhost}/{app}/{stream}"


_APP_STREAM_PATTERN = re.compile(r"^[a-zA-Z0-9._-]+$")
_URL_PREFIXES = ("rtsp://", "rtmp://", "http://", "https://")
_IMPORT_DEFAULT_VHOST = "__defaultVhost__"
_CSV_IMPORT_HEADERS = {
    "vhost": {"vhost", "虚拟主机(vhost)", "虚拟主机"},
    "app": {"app", "应用名(app)", "应用名"},
    "stream": {"stream", "流id(stream)", "流id", "流ID(stream)", "流ID"},
    "url": {"url", "源流地址(url)", "源流地址"},
    "audio_type": {"audio_type", "音频设置(audio_type)", "音频设置"},
}


def _normalize_csv_header(value: str) -> str:
    return str(value or "").strip().lower()


def _resolve_csv_header_map(headers: list[str]) -> dict[str, str]:
    resolved: dict[str, str] = {}
    normalized_to_raw = {_normalize_csv_header(h): h for h in headers}
    for field, aliases in _CSV_IMPORT_HEADERS.items():
        found = None
        for alias in aliases:
            if _normalize_csv_header(alias) in normalized_to_raw:
                found = normalized_to_raw[_normalize_csv_header(alias)]
                break
        if found:
            resolved[field] = found
    return resolved


def _extract_csv_value(row: dict[str, str], header_map: dict[str, str], field: str) -> str:
    key = header_map.get(field)
    if not key:
        return ""
    return str(row.get(key, "")).strip()


def _parse_audio_type(value: str) -> tuple[int | None, str | None]:
    text = str(value or "").strip()
    if text == "":
        return None, None
    if text in {"0", "1", "2"}:
        return int(text), None
    lowered = text.lower()
    if lowered in {"disable", "off", "none", "noaudio"}:
        return 0, None
    if lowered in {"origin", "source", "audio"}:
        return 1, None
    if lowered in {"mute", "silent"}:
        return 2, None
    return None, "audio_type 仅支持 0/1/2（可为空）"


def _validate_pull_proxy_input(
    *,
    app: str,
    stream: str,
    url: str,
    audio_type: int | None,
) -> str | None:
    if not _APP_STREAM_PATTERN.match(app):
        return "app 只能包含字母、数字、下划线(_)、短横线(-) 或英文句点(.)"
    if not _APP_STREAM_PATTERN.match(stream):
        return "stream 只能包含字母、数字、下划线(_)、短横线(-) 或英文句点(.)"
    if not any(url.startswith(prefix) for prefix in _URL_PREFIXES):
        return "源流地址必须以 rtsp://、rtmp://、http:// 或 https:// 开头"
    if audio_type is not None and audio_type not in (0, 1, 2):
        return "audio_type 仅支持 0/1/2"
    return None


async def _add_stream_proxy_to_zlm(
    *,
    vhost: str,
    app: str,
    stream: str,
    url: str,
    audio_type: int | None,
) -> None:
    query_params = {
        "secret": ZLM_SECRET,
        "vhost": vhost,
        "app": app,
        "stream": stream,
        "url": url,
    }
    query_params.update(_audio_type_to_zlm_params(audio_type))
    try:
        await client.get(f"{ZLM_SERVER}/index/api/addStreamProxy", params=query_params)
    except Exception:
        return


async def _del_stream_proxy_from_zlm(*, vhost: str, app: str, stream: str) -> None:
    query_params = {"secret": ZLM_SECRET}
    query_params["key"] = _stream_proxy_key(vhost, app, stream)
    try:
        await client.get(f"{ZLM_SERVER}/index/api/delStreamProxy", params=query_params)
    except Exception:
        return


async def sync_pull_proxies_from_db() -> None:
    rows = db_list_pull_proxies()
    if not rows:
        return

    existing_keys: set[str] = set()
    try:
        query_params = {"secret": ZLM_SECRET}
        response = await client.get(
            f"{ZLM_SERVER}/index/api/listStreamProxy", params=query_params
        )
        raw_data = response.json()
        if raw_data.get("code") == 0:
            for item in raw_data.get("data", []) or []:
                if isinstance(item, dict) and item.get("key"):
                    existing_keys.add(str(item["key"]))
                    continue
                src = (item or {}).get("src") or {}
                vhost = src.get("vhost")
                app = src.get("app")
                stream = src.get("stream")
                if vhost and app and stream:
                    existing_keys.add(_stream_proxy_key(vhost, app, stream))
    except Exception:
        existing_keys = set()

    for row in rows:
        vhost = row["vhost"]
        app = row["app"]
        stream = row["stream"]
        key = _stream_proxy_key(vhost, app, stream)
        if key in existing_keys:
            continue

        query_params = {
            "secret": ZLM_SECRET,
            "vhost": vhost,
            "app": app,
            "stream": stream,
            "url": row["url"],
        }
        query_params.update(_audio_type_to_zlm_params(row.get("audio_type")))
        try:
            await client.get(
                f"{ZLM_SERVER}/index/api/addStreamProxy", params=query_params
            )
        except Exception:
            continue


# =============================================================================


@app.get("/api/perf/statistic", summary="获取主要对象个数", tags=["性能"])
async def get_statistic():
    query_params = {"secret": ZLM_SECRET}
    response = await client.get(
        f"{ZLM_SERVER}/index/api/getStatistic", params=query_params
    )
    return response.json()


@app.get("/api/perf/work-threads-load", summary="获取后台线程负载", tags=["性能"])
async def get_work_threads_load():
    query_params = {"secret": ZLM_SECRET}
    response = await client.get(
        f"{ZLM_SERVER}/index/api/getWorkThreadsLoad", params=query_params
    )
    return response.json()


@app.get("/api/perf/threads-load", summary="获取网络线程负载", tags=["性能"])
async def get_threads_load():
    query_params = {"secret": ZLM_SECRET}
    response = await client.get(
        f"{ZLM_SERVER}/index/api/getThreadsLoad", params=query_params
    )
    return response.json()


@app.get(
    "/api/perf/host-stats",
    summary="获取当前系统资源使用率",
    tags=["性能"],
)
async def get_host_stats():
    timestamp = datetime.now().strftime("%H:%M:%S")

    # CPU 使用率
    cpu_percent = psutil.cpu_percent(interval=None)

    # 内存使用率
    memory = psutil.virtual_memory()
    memory_info = {
        "used": round(memory.used / (1024**3), 2),
        "total": round(memory.total / (1024**3), 2),
    }

    # 磁盘使用率
    disks: list[dict] = []
    seen_devices: set[str] = set()
    try:
        for part in psutil.disk_partitions(all=False):
            if not part.device or not part.fstype:
                continue
            if not (
                part.device.startswith("/dev/sd")
                or part.device.startswith("/dev/nvme")
                or part.device.startswith("/dev/vd")
            ):
                continue
            if part.device in seen_devices:
                continue
            try:
                usage = psutil.disk_usage(part.mountpoint)
            except PermissionError:
                continue
            seen_devices.add(part.device)
            disks.append(
                {
                    "device": part.device,
                    "mountpoint": part.mountpoint,
                    "fstype": part.fstype,
                    "used": round(usage.used / (1024**3), 2),
                    "total": round(usage.total / (1024**3), 2),
                }
            )
    except Exception:
        disks = []

    if disks:
        total_used = sum(d["used"] for d in disks)
        total_total = sum(d["total"] for d in disks)
        disk_info = {
            "used": round(total_used, 2),
            "total": round(total_total, 2),
        }
    else:
        disk = psutil.disk_usage("/")
        disk_info = {
            "used": round(disk.used / (1024**3), 2),
            "total": round(disk.total / (1024**3), 2),
        }

    net = psutil.net_io_counters()
    net_io = {
        "sent": int(net.bytes_sent),
        "recv": int(net.bytes_recv),
    }

    return {
        "code": 0,
        "data": {
            "time": timestamp,
            "ts_ms": int(datetime.now().timestamp() * 1000),
            "cpu": round(cpu_percent, 2),
            "memory": memory_info,
            "disk": disk_info,
            "disks": disks,
            "net_io": net_io,
        },
    }


# =============================================================================
@app.post("/api/stream/pull-proxy", summary="添加拉流代理", tags=["流"])
async def post_pull_proxy(
    vhost: str = Query("__defaultVhost__", description="虚拟主机"),
    app: str = Query(..., description="应用名"),
    stream: str = Query(..., description="流ID"),
    url: str = Query(..., description="源流地址"),
    audio_type: int | None = Query(None, description="音频设置"),
):
    if not re.match(r"^[a-zA-Z0-9._-]+$", app):
        return {
            "code": -1,
            "msg": "app 只能包含字母、数字、下划线(_)、短横线(-) 或英文句点(.)",
        }
    if not re.match(r"^[a-zA-Z0-9._-]+$", stream):
        return {
            "code": -1,
            "msg": "stream 只能包含字母、数字、下划线(_)、短横线(-) 或英文句点(.)",
        }

    # 验证 url 前缀
    if not any(
        url.startswith(prefix)
        for prefix in ["rtsp://", "rtmp://", "http://", "https://"]
    ):
        return {
            "code": -1,
            "msg": "源流地址必须以 rtsp://、rtmp://、http:// 或 https:// 开头",
        }

    db_row = db_upsert_pull_proxy(
        vhost=vhost,
        app=app,
        stream=stream,
        url=url,
        audio_type=audio_type,
    )

    asyncio.create_task(
        _add_stream_proxy_to_zlm(
            vhost=vhost,
            app=app,
            stream=stream,
            url=url,
            audio_type=audio_type,
        )
    )

    warning = summarize_existing_recordings(
        record_root=RECORD_ROOT, app=app, stream=stream
    )
    return {"code": 0, "msg": "已保存，后台连接中", "db": db_row, "warning": warning}


@app.delete("/api/stream/pull-proxy", summary="删除拉流代理", tags=["流"])
async def delete_pull_proxy(
    vhost: str = Query("__defaultVhost__", description="虚拟主机"),
    app: str = Query(..., description="应用名"),
    stream: str = Query(..., description="流id"),
):
    deleted = db_delete_pull_proxy(vhost=vhost, app=app, stream=stream)
    db_delete_record_policy(vhost=vhost, app=app, stream=stream)
    asyncio.create_task(_del_stream_proxy_from_zlm(vhost=vhost, app=app, stream=stream))
    return {"code": 0, "msg": "已删除，后台同步中", "db_deleted": deleted}


# @app.get("/api/stream/pull-proxy-list", summary="获取拉流代理列表", tags=["流"])
# async def get_pull_proxy_list():
#     rows = db_list_pull_proxies()

#     repull_count_map: dict[str, int] = {}
#     try:
#         query_params = {"secret": ZLM_SECRET}
#         response = await client.get(
#             f"{ZLM_SERVER}/index/api/listStreamProxy", params=query_params
#         )
#         raw_data = response.json()
#         if raw_data.get("code") == 0:
#             for item in raw_data.get("data", []) or []:
#                 src = (item or {}).get("src") or {}
#                 vhost = src.get("vhost")
#                 app = src.get("app")
#                 stream = src.get("stream")
#                 if not (vhost and app and stream):
#                     continue
#                 key = _stream_proxy_key(vhost, app, stream)
#                 try:
#                     repull_count_map[key] = int(item.get("rePullCount", 0) or 0)
#                 except Exception:
#                     repull_count_map[key] = 0
#     except Exception:
#         repull_count_map = {}

#     data: list[dict] = []
#     for row in rows:
#         vhost = row["vhost"]
#         app = row["app"]
#         stream = row["stream"]
#         key = _stream_proxy_key(vhost, app, stream)
#         data.append(
#             {
#                 "key": key,
#                 "src": {"vhost": vhost, "app": app, "stream": stream},
#                 "url": row["url"],
#                 "rePullCount": repull_count_map.get(key, 0),
#                 "audio_type": row.get("audio_type"),
#             }
#         )

#     return {"code": 0, "data": data}


@app.get(
    "/api/stream/pull-proxy-table",
    summary="获取拉流列表（含在线状态）",
    tags=["流"],
)
async def get_pull_proxy_table(
    vhost: str = Query("__defaultVhost__", description="筛选虚拟主机"),
    app: str | None = Query(None, description="筛选应用名"),
    stream: str | None = Query(None, description="筛选流id"),
):
    rows = db_list_pull_proxies()
    if vhost:
        rows = [r for r in rows if r.get("vhost") == vhost]
    if app:
        rows = [r for r in rows if app in str(r.get("app", ""))]
    if stream:
        rows = [r for r in rows if stream in str(r.get("stream", ""))]

    repull_count_map: dict[str, int] = {}
    active_stream_map: dict[str, dict] = {}

    try:
        list_params = {"secret": ZLM_SECRET}
        media_params = {"secret": ZLM_SECRET, "vhost": vhost}
        list_resp, media_resp = await asyncio.gather(
            client.get(f"{ZLM_SERVER}/index/api/listStreamProxy", params=list_params),
            client.get(f"{ZLM_SERVER}/index/api/getMediaList", params=media_params),
        )

        list_raw = list_resp.json()
        if list_raw.get("code") == 0:
            for item in list_raw.get("data", []) or []:
                src = (item or {}).get("src") or {}
                src_vhost = src.get("vhost")
                src_app = src.get("app")
                src_stream = src.get("stream")
                if not (src_vhost and src_app and src_stream):
                    continue
                key = _stream_proxy_key(src_vhost, src_app, src_stream)
                try:
                    repull_count_map[key] = int(item.get("rePullCount", 0) or 0)
                except Exception:
                    repull_count_map[key] = 0

        media_raw = media_resp.json()
        if media_raw.get("code") == 0:
            for media in media_raw.get("data", []) or []:
                if not isinstance(media, dict):
                    continue
                if media.get("originTypeStr") != "pull":
                    continue

                media_vhost = str(media.get("vhost", ""))
                media_app = str(media.get("app", ""))
                media_stream = str(media.get("stream", ""))
                if not (media_vhost and media_app and media_stream):
                    continue

                key = _stream_proxy_key(media_vhost, media_app, media_stream)
                if key not in active_stream_map:
                    active_stream_map[key] = {
                        "vhost": media_vhost,
                        "app": media_app,
                        "stream": media_stream,
                        "originTypeStr": media.get("originTypeStr"),
                        "originUrl": media.get("originUrl"),
                        "originSock": media.get("originSock"),
                        "aliveSecond": media.get("aliveSecond"),
                        "isRecordingMP4": media.get("isRecordingMP4"),
                        "isRecordingHLS": media.get("isRecordingHLS"),
                        "totalReaderCount": media.get("totalReaderCount"),
                        "schemas": [],
                    }

                active_stream_map[key]["schemas"].append(
                    {
                        "schema": media.get("schema"),
                        "bytesSpeed": media.get("bytesSpeed"),
                        "readerCount": media.get("readerCount"),
                        "totalBytes": media.get("totalBytes"),
                        "tracks": media.get("tracks", []),
                    }
                )
    except Exception:
        repull_count_map = {}
        active_stream_map = {}

    data: list[dict] = []
    for row in rows:
        row_vhost = str(row.get("vhost", "__defaultVhost__"))
        row_app = str(row.get("app", ""))
        row_stream = str(row.get("stream", ""))
        key = _stream_proxy_key(row_vhost, row_app, row_stream)
        active = active_stream_map.get(key)
        data.append(
            {
                "vhost": row_vhost,
                "app": row_app,
                "stream": row_stream,
                "url": row.get("url"),
                "audio_type": row.get("audio_type"),
                "rePullCount": repull_count_map.get(key, 0),
                "isOnline": bool(active),
                "totalReaderCount": active.get("totalReaderCount") if active else "-",
                "aliveSecond": active.get("aliveSecond") if active else "-",
                "isRecordingMP4": active.get("isRecordingMP4") if active else "-",
                "schemas": active.get("schemas") if active else "-",
            }
        )

    return {"code": 0, "data": data}


@app.get(
    "/api/stream/streamid-list",
    summary="获取当前在线流ID列表（包括拉流和推流）",
    tags=["流"],
)
async def get_streamid_list(
    vhost: str = Query("__defaultVhost__", description="筛选虚拟主机"),
    schema: str | None = Query(None, description="筛选协议，例如 rtsp或rtmp"),
    app: str | None = Query(None, description="筛选应用名"),
    stream: str | None = Query(None, description="筛选流id"),
):
    query_params = {"secret": ZLM_SECRET}

    if schema:
        query_params["schema"] = schema
    if vhost:
        query_params["vhost"] = vhost
    if app:
        query_params["app"] = app
    if stream:
        query_params["stream"] = stream

    response = await client.get(
        f"{ZLM_SERVER}/index/api/getMediaList", params=query_params
    )
    raw_data = response.json()

    if raw_data["code"] != 0:
        return raw_data  # 错误直接返回

    media_list = raw_data.get("data", [])
    stream_map = {}

    for media in media_list:
        key = (media["vhost"], media["app"], media["stream"])
        if key not in stream_map:
            # 初始化主信息（这些字段在同一个流中应该一致）
            stream_map[key] = {
                "vhost": media["vhost"],
                "app": media["app"],
                "stream": media["stream"],
                "originTypeStr": media["originTypeStr"],
                "originUrl": media["originUrl"],
                "originSock": media["originSock"],
                "aliveSecond": media["aliveSecond"],
                "isRecordingMP4": media["isRecordingMP4"],
                "isRecordingHLS": media["isRecordingHLS"],
                "totalReaderCount": media["totalReaderCount"],
                "schemas": [],
            }

        # 添加当前 schema 的信息
        stream_map[key]["schemas"].append(
            {
                "schema": media["schema"],
                "bytesSpeed": media["bytesSpeed"],
                "readerCount": media["readerCount"],
                "totalBytes": media["totalBytes"],
                "tracks": media.get("tracks", []),
            }
        )

    # 转为列表返回
    result = list(stream_map.values())
    return {"code": 0, "data": result}


@app.delete(
    "/api/stream/streamid", summary="删除在线流ID（包括拉流和推流）", tags=["流"]
)
async def delete_streamid(
    vhost: str = Query("__defaultVhost__", description="虚拟主机"),
    app: str = Query(..., description="应用名"),
    stream: str = Query(..., description="流ID"),
):
    query_params = {"secret": ZLM_SECRET}
    query_params["vhost"] = str(vhost)
    query_params["app"] = str(app)
    query_params["stream"] = str(stream)
    query_params["force"] = "1"

    response = await client.get(
        f"{ZLM_SERVER}/index/api/close_streams", params=query_params
    )
    return response.json()


# =============================================================================
@app.get("/api/playback/start-record", summary="开启录制", tags=["录制"])
async def get_start_record(
    vhost: str = Query("__defaultVhost__", description="虚拟主机"),
    app: str = Query(..., description="应用名"),
    stream: str = Query(..., description="流ID"),
    record_days: str = Query(..., description="录制天数"),
):
    url = f"{ZLM_SERVER}/index/api/startRecord"

    query = {"secret": ZLM_SECRET}
    query["vhost"] = str(vhost)
    query["app"] = str(app)
    query["stream"] = str(stream)
    query["type"] = "1"
    try:
        retention_days = int(record_days)
    except Exception:
        return {"code": -1, "msg": "record_days 必须是整数"}
    if retention_days <= 0 or retention_days > 30:
        return {"code": -1, "msg": "录像天数范围建议 1-30 天"}

    db_row = db_upsert_record_policy(
        vhost=str(vhost),
        app=str(app),
        stream=str(stream),
        retention_days=retention_days,
        enabled=True,
    )
    query["max_second"] = "300"

    response = await client.get(url, params=query)
    raw = response.json()
    raw["record_policy"] = db_row
    return raw


@app.get("/api/playback/stop-record", summary="停止录制", tags=["录制"])
async def get_stop_record(
    vhost: str = Query("__defaultVhost__", description="虚拟主机"),
    app: str = Query(..., description="应用名"),
    stream: str = Query(..., description="流ID"),
):
    url = f"{ZLM_SERVER}/index/api/stopRecord"

    query = {"secret": ZLM_SECRET}
    query["vhost"] = str(vhost)
    query["app"] = str(app)
    query["stream"] = str(stream)
    query["type"] = "1"

    response = await client.get(url, params=query)
    raw = response.json()
    existing = db_get_record_policy(vhost=str(vhost), app=str(app), stream=str(stream))
    if existing:
        try:
            retention_days = int(existing.get("retention_days", 0) or 0)
        except Exception:
            retention_days = 0
        db_upsert_record_policy(
            vhost=str(vhost),
            app=str(app),
            stream=str(stream),
            retention_days=retention_days,
            enabled=False,
        )
    return raw


@app.get("/api/playback/event-record", summary="开启事件视频录制", tags=["录制"])
async def get_event_record(
    vhost: str = Query("__defaultVhost__", description="虚拟主机"),
    app: str = Query(..., description="应用名"),
    stream: str = Query(..., description="流ID"),
    path: str = Query(..., description="录像保存相对路径，如 person/test.mp4"),
    back_ms: str = Query(..., description="回溯录制时长"),
    forward_ms: str = Query(..., description="后续录制时长"),
):
    url = f"{ZLM_SERVER}/index/api/startRecordTask"

    query = {"secret": ZLM_SECRET}
    query["vhost"] = str(vhost)
    query["app"] = str(app)
    query["stream"] = str(stream)
    query["path"] = path
    query["back_ms"] = back_ms
    query["forward_ms"] = forward_ms

    response = await client.get(url, params=query)
    return response.json()


@app.get(
    "/api/playback/streamid-record-list",
    summary="获取本地所有流ID的录制信息",
    tags=["录制"],
)
async def get_streamid_record_list():
    result = []

    if not RECORD_ROOT.exists() or not RECORD_ROOT.is_dir():
        return {"code": -1, "msg": f"{RECORD_ROOT} 目录不存在或不是目录"}

    # 正则匹配 YYYY-MM-DD 格式
    date_pattern = re.compile(r"^(\d{4})-(\d{2})-(\d{2})$")

    policy_map: dict[tuple[str, str, str], dict] = {}
    try:
        for row in db_list_record_policies(enabled_only=False) or []:
            vhost = str(row.get("vhost") or "__defaultVhost__")
            app_name = str(row.get("app") or "")
            stream_name = str(row.get("stream") or "")
            if not (app_name and stream_name):
                continue
            policy_map[(vhost, app_name, stream_name)] = dict(row)
    except Exception:
        policy_map = {}

    active_keys: set[tuple[str, str, str]] = set()
    recording_map: dict[tuple[str, str, str], bool] = {}
    try:
        query_params = {"secret": ZLM_SECRET, "vhost": "__defaultVhost__"}
        response = await client.get(
            f"{ZLM_SERVER}/index/api/getMediaList", params=query_params
        )
        raw = response.json()
        if raw.get("code") == 0:
            for media in raw.get("data", []) or []:
                if not isinstance(media, dict):
                    continue
                vhost = str(media.get("vhost") or "__defaultVhost__")
                app_name = str(media.get("app") or "")
                stream_name = str(media.get("stream") or "")
                if not (app_name and stream_name):
                    continue
                key = (vhost, app_name, stream_name)
                active_keys.add(key)
                try:
                    is_recording = bool(media.get("isRecordingMP4"))
                except Exception:
                    is_recording = False
                if is_recording:
                    recording_map[key] = True
                else:
                    recording_map.setdefault(key, False)
    except Exception:
        active_keys = set()
        recording_map = {}

    try:
        for app_name in os.listdir(RECORD_ROOT):
            app_path = RECORD_ROOT / app_name
            if not app_path.is_dir():
                continue

            for stream_name in os.listdir(app_path):
                stream_path = app_path / stream_name
                if not stream_path.is_dir():
                    continue

                total_slices = 0
                total_size_bytes = 0
                dates = set()

                # 遍历 stream_path 下所有子项
                for item in os.listdir(stream_path):
                    item_path = stream_path / item

                    if not item_path.is_dir():
                        continue

                    # 使用正则匹配 YYYY-MM-DD
                    match = date_pattern.match(item)
                    if not match:
                        continue  # 不符合格式

                    # 检查该日期目录下是否有 .mp4 文件
                    try:
                        mp4_files = [
                            f
                            for f in os.listdir(item_path)
                            if f.lower().endswith(".mp4")
                        ]
                    except Exception:
                        continue

                    if not mp4_files:
                        # 空目录：删除
                        try:
                            shutil.rmtree(item_path)
                            print(f"已删除空录像目录: {item_path}")
                        except Exception as e:
                            print(f"删除空目录失败 {item_path}: {e}")
                        continue

                    # 统计文件数量和大小
                    for fname in mp4_files:
                        file_path = item_path / fname
                        if not file_path.is_file():
                            continue
                        try:
                            size = file_path.stat().st_size
                            total_size_bytes += size
                            total_slices += 1
                        except OSError as e:
                            print(f"读取文件大小失败 {file_path}: {e}")

                    # 添加有效日期
                    dates.add(item)

                # 只有存在录像片段才加入结果
                if total_slices == 0:
                    continue

                policy = (
                    policy_map.get(("__defaultVhost__", app_name, stream_name)) or {}
                )
                try:
                    enabled = int(policy.get("enabled", 0) or 0)
                except Exception:
                    enabled = 0
                record_days = policy.get("retention_days", "-") if enabled == 1 else "-"

                result.append(
                    {
                        "app": app_name,
                        "stream": stream_name,
                        "slice_num": total_slices,
                        "total_storage_gb": round(total_size_bytes / (1024**3), 2),
                        "dates": sorted(dates),
                        "record_days": record_days,
                        "isOnline": ("__defaultVhost__", app_name, stream_name)
                        in active_keys,
                        "isRecordingMP4": recording_map.get(
                            ("__defaultVhost__", app_name, stream_name), False
                        ),
                    }
                )

        return {"code": 0, "data": result}

    except Exception as e:
        return {"code": -1, "msg": f"目录遍历异常 {e}"}


@app.get(
    "/api/playback/streamid-record", summary="获取指定流ID的全部录制信息", tags=["录制"]
)
async def get_streamid_record(
    app: str = Query(..., description="应用名"),
    stream: str = Query(..., description="流ID"),
    date: str = Query(..., description="日期格式 YYYY-MM-DD"),
):
    target_dir = RECORD_ROOT / app / stream / date

    if not target_dir.exists():
        return {"code": 1, "msg": f"目录不存在: {target_dir}"}

    if not target_dir.is_dir():
        return {"code": 1, "msg": f"路径不是目录: {target_dir}"}

    tz_shanghai = ZoneInfo("Asia/Shanghai")
    parsed: list[tuple[Path, datetime]] = []
    fallback_files: list[Path] = []

    for file_path in target_dir.iterdir():
        if not file_path.is_file():
            continue
        if file_path.suffix.lower() != ".mp4":
            continue
        if file_path.name.startswith("."):
            continue

        m = re.match(
            r"(\d{4})-(\d{1,2})-(\d{1,2})-(\d{1,2})-(\d{1,2})-(\d{1,2})",
            file_path.name,
        )
        if not m:
            fallback_files.append(file_path)
            continue
        year, month, day, hour, minute, second = map(int, m.groups())
        try:
            start_dt = datetime(
                year, month, day, hour, minute, second, tzinfo=tz_shanghai
            )
        except ValueError:
            fallback_files.append(file_path)
            continue
        parsed.append((file_path, start_dt))

    parsed.sort(key=lambda x: x[1])

    results: list[dict] = []
    default_duration = 300.0
    for i, (file_path, start_dt) in enumerate(parsed):
        duration = default_duration
        if i + 1 < len(parsed):
            next_start = parsed[i + 1][1]
            delta = (next_start - start_dt).total_seconds()
            if 1 <= delta <= 600:
                duration = float(delta)
        end_dt = start_dt + timedelta(seconds=duration)

        try:
            rel_path = file_path.relative_to(RECORD_ROOT)
        except ValueError:
            continue

        results.append(
            {
                "filename": str(rel_path),
                "duration": round(duration, 3),
                "start": start_dt.isoformat(),
                "end": end_dt.isoformat(),
            }
        )

    for file_path in fallback_files:
        data = get_video_shanghai_time(file_path)
        if not data:
            continue
        try:
            rel_path = file_path.relative_to(RECORD_ROOT)
            data["filename"] = str(rel_path)
        except ValueError:
            continue
        results.append(data)

    results.sort(key=lambda x: x["start"])

    return {"code": 0, "data": results}


@app.delete(
    "/api/playback/streamid-record", summary="删除指定流ID的全部录制文件", tags=["录制"]
)
async def delete_streamid_record(
    app: str = Query(..., description="应用名"),
    stream: str = Query(..., description="流ID"),
):
    base_dir = RECORD_ROOT / app / stream

    if not base_dir.exists():
        return {"code": -1, "msg": f"目录不存在: {base_dir}"}

    if not base_dir.is_dir():
        return {"code": -1, "msg": f"路径不是目录: {base_dir}"}

    # 匹配 YYYY-MM-DD 格式
    date_pattern = re.compile(r"^\d{4}-\d{2}-\d{2}$")

    deleted_count = 0

    for item in base_dir.iterdir():
        if item.is_dir() and date_pattern.match(item.name):
            shutil.rmtree(item)
            deleted_count += 1

    return {"code": 0, "msg": f"已删除 {deleted_count} 个录像目录"}


# =============================================================================


@app.get("/api/server/config", summary="获取服务器配置", tags=["配置"])
async def get_server_config():
    query_params = {"secret": ZLM_SECRET}
    response = await client.get(
        f"{ZLM_SERVER}/index/api/getServerConfig", params=query_params
    )
    return response.json()


@app.put("/api/server/config", summary="修改服务器配置", tags=["配置"])
async def put_server_config(request: Request):
    query_params = dict(request.query_params)
    query_params["secret"] = ZLM_SECRET

    response = await client.get(
        f"{ZLM_SERVER}/index/api/setServerConfig", params=query_params
    )
    return response.json()


@app.get(
    "/api/server/restart",
    summary="重启 ZLMediaKit（同时重启 StreamUI）",
    tags=["配置"],
)
async def get_restart_zlm(delay_ms: int = Query(0, description="延迟重启（毫秒）")):
    await asyncio.sleep(max(delay_ms, 0) / 1000)

    try:
        client = docker.DockerClient(base_url="unix://var/run/docker.sock")
        zlm_container = client.containers.get(ZLM_CONTAINER_NAME)
        zlm_container.restart()

        if STREAMUI_CONTAINER_NAME:

            async def _restart_self():
                try:
                    c = docker.DockerClient(base_url="unix://var/run/docker.sock")
                    self_container = c.containers.get(STREAMUI_CONTAINER_NAME)
                    self_container.restart()
                except Exception:
                    return

            asyncio.create_task(_restart_self())

        return {
            "code": 0,
            "msg": "重启成功",
            "via": "docker",
            "restart_self": True,
        }
    except Exception as e:
        return {"code": -1, "msg": "重启失败", "error": str(e), "via": "docker"}


@app.get(
    "/api/stream/pull-proxy-template",
    summary="Download pull proxy import template",
    tags=["流"],
)
async def get_pull_proxy_template():
    output = io.StringIO()
    writer = csv.writer(output, lineterminator="\n")
    writer.writerow(
        [
            "虚拟主机(vhost)",
            "应用名(app)",
            "流ID(stream)",
            "源流地址(url)",
            "音频设置(audio_type)",
        ]
    )
    writer.writerow(
        [
            "__defaultVhost__",
            "live",
            "cam101",
            "rtsp://admin:password@192.168.0.64:554/Streaming/Channels/101",
            "1",
        ]
    )
    content = "\ufeff" + output.getvalue()
    return Response(
        content=content,
        media_type="text/csv; charset=utf-8",
        headers={
            "Content-Disposition": (
                'attachment; filename="pull-proxy-template.csv"; '
                "filename*=UTF-8''pull-proxy-template.csv"
            )
        },
    )


@app.get(
    "/api/stream/pull-proxy-export",
    summary="Export pull proxies to CSV",
    tags=["流"],
)
async def get_pull_proxy_export():
    rows = db_list_pull_proxies()
    output = io.StringIO()
    writer = csv.writer(output, lineterminator="\n")
    writer.writerow(
        [
            "虚拟主机(vhost)",
            "应用名(app)",
            "流ID(stream)",
            "源流地址(url)",
            "音频设置(audio_type)",
        ]
    )
    for row in rows:
        writer.writerow(
            [
                str(row.get("vhost") or _IMPORT_DEFAULT_VHOST),
                str(row.get("app") or ""),
                str(row.get("stream") or ""),
                str(row.get("url") or ""),
                "" if row.get("audio_type") is None else str(row.get("audio_type")),
            ]
        )
    content = "\ufeff" + output.getvalue()
    now_str = datetime.now().strftime("%Y%m%d_%H%M%S")
    filename = f"pull-proxy-export-{now_str}.csv"
    return Response(
        content=content,
        media_type="text/csv; charset=utf-8",
        headers={
            "Content-Disposition": (
                f'attachment; filename="{filename}"; '
                f"filename*=UTF-8''{filename}"
            )
        },
    )


@app.post(
    "/api/stream/pull-proxy-import",
    summary="Import pull proxies from CSV",
    tags=["流"],
)
async def post_pull_proxy_import(request: Request):
    try:
        body = await request.json()
    except Exception:
        return {"code": -1, "msg": "请求体必须是 JSON"}

    csv_text = str(body.get("csv_text") or "")
    mode = str(body.get("mode") or "overwrite").strip().lower()
    if mode not in {"overwrite", "skip"}:
        return {"code": -1, "msg": "mode 仅支持 overwrite 或 skip"}

    if not csv_text.strip():
        return {"code": -1, "msg": "导入文件内容为空"}

    # Allow UTF-8 BOM.
    csv_text = csv_text.lstrip("\ufeff")

    try:
        reader = csv.DictReader(io.StringIO(csv_text))
        headers = list(reader.fieldnames or [])
    except Exception:
        return {"code": -1, "msg": "CSV 格式解析失败"}

    if not headers:
        return {"code": -1, "msg": "CSV 缺少表头"}

    header_map = _resolve_csv_header_map(headers)
    required_fields = ("app", "stream", "url")
    missing = [field for field in required_fields if field not in header_map]
    if missing:
        return {
            "code": -1,
            "msg": f"CSV 缺少必要列: {', '.join(missing)}",
            "supported_headers": _CSV_IMPORT_HEADERS,
        }

    existing_rows = db_list_pull_proxies()
    existing_keys = {
        _stream_proxy_key(
            str(item.get("vhost") or _IMPORT_DEFAULT_VHOST),
            str(item.get("app") or ""),
            str(item.get("stream") or ""),
        )
        for item in existing_rows
    }

    total = 0
    created = 0
    overwritten = 0
    skipped = 0
    failed = 0
    seen_in_file: set[str] = set()
    errors: list[dict] = []
    imported_rows: list[dict] = []

    for line_no, row in enumerate(reader, start=2):
        total += 1
        vhost = _extract_csv_value(row, header_map, "vhost") or _IMPORT_DEFAULT_VHOST
        app = _extract_csv_value(row, header_map, "app")
        stream = _extract_csv_value(row, header_map, "stream")
        url = _extract_csv_value(row, header_map, "url")
        audio_raw = _extract_csv_value(row, header_map, "audio_type")

        audio_type, audio_err = _parse_audio_type(audio_raw)
        if audio_err:
            failed += 1
            errors.append({"line": line_no, "reason": audio_err, "row": row})
            continue

        invalid_reason = _validate_pull_proxy_input(
            app=app,
            stream=stream,
            url=url,
            audio_type=audio_type,
        )
        if invalid_reason:
            failed += 1
            errors.append({"line": line_no, "reason": invalid_reason, "row": row})
            continue

        key = _stream_proxy_key(vhost, app, stream)
        if key in seen_in_file:
            failed += 1
            errors.append({"line": line_no, "reason": "导入文件内存在重复 app/stream", "row": row})
            continue
        seen_in_file.add(key)

        if mode == "skip" and key in existing_keys:
            skipped += 1
            continue

        db_row = db_upsert_pull_proxy(
            vhost=vhost,
            app=app,
            stream=stream,
            url=url,
            audio_type=audio_type,
        )
        asyncio.create_task(
            _add_stream_proxy_to_zlm(
                vhost=vhost,
                app=app,
                stream=stream,
                url=url,
                audio_type=audio_type,
            )
        )
        imported_rows.append({"vhost": vhost, "app": app, "stream": stream, "db": db_row})

        if key in existing_keys:
            overwritten += 1
        else:
            created += 1
            existing_keys.add(key)

    return {
        "code": 0,
        "msg": "导入完成",
        "mode": mode,
        "summary": {
            "total": total,
            "created": created,
            "overwritten": overwritten,
            "skipped": skipped,
            "failed": failed,
        },
        "errors": errors[:200],
        "imported": imported_rows[:200],
    }


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("main:app", host="0.0.0.0", port=10801, reload=True)
    # uvicorn.run("main:app", host="0.0.0.0", port=10801, reload=False)
