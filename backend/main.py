import os
import shutil
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path
import re
import httpx
import psutil
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from fastapi import FastAPI, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from scheduler import cleanup_old_videos
from utils import get_video_shanghai_time, get_zlm_secret


from onvif.api import router as onvif_router

# =========================================================
# zlmediakit 地址
ZLM_SERVER = "http://127.0.0.1:8080"
# zlmediakit 密钥
ZLM_SECRET = get_zlm_secret("/opt/media/conf/config.ini")
# zlmediakit 录像
RECORD_ROOT = Path("/opt/media/bin/www/record")
# 录像最大切片数
KEEP_VIDEOS = 72
# =========================================================


@asynccontextmanager
async def lifespan(app: FastAPI):
    scheduler = AsyncIOScheduler()

    # 添加任务：每小时整点执行
    scheduler.add_job(
        cleanup_old_videos,
        kwargs={"path": RECORD_ROOT, "keep_videos": KEEP_VIDEOS},
        trigger=CronTrigger(hour=0, minute=0),  # 每小时整点
        id="cleanup_videos",
        name="每小时清理旧视频片段",
        replace_existing=True,
    )

    # 只有在这里，事件循环已经启动，可以安全 start
    scheduler.start()
    print("[Scheduler] 🚀 定时任务已启动")

    yield

    scheduler.shutdown()
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
    title="接口",
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
    tags=["性能"],
    summary="获取当前系统资源使用率（CPU、内存、磁盘、网络）",
)
async def get_system_stats():
    timestamp = datetime.now().strftime("%H:%M:%S")

    # CPU 使用率（非阻塞）
    cpu_percent = psutil.cpu_percent(interval=None)

    # 内存
    memory = psutil.virtual_memory()
    memory_info = {
        "used": round(memory.used / (1024**3), 2),
        "total": round(memory.total / (1024**3), 2),
    }

    # 磁盘
    disk = psutil.disk_usage("/")
    disk_info = {
        "used": round(disk.used / (1024**3), 2),
        "total": round(disk.total / (1024**3), 2),
    }

    # 网络
    net = psutil.net_io_counters()
    net_info = {
        "sent": net.bytes_sent,
        "recv": net.bytes_recv,
    }

    return {
        "code": 0,
        "data": {
            "time": timestamp,
            "cpu": round(cpu_percent, 2),
            "memory": memory_info,
            "disk": disk_info,
            "network": net_info,
        },
    }


# =============================================================================
@app.post("/api/stream/pull-proxy", tags=["流"], summary="添加拉流代理")
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

    # 构造转发请求
    query_params = {
        "secret": ZLM_SECRET,
        "vhost": vhost,
        "app": app,
        "stream": stream,
        "url": url,
    }

    # 处理 audio_type 映射
    if audio_type == 0:
        query_params["enable_audio"] = "0"
        query_params["add_mute_audio"] = "0"
    elif audio_type == 1:
        query_params["enable_audio"] = "1"
        query_params["add_mute_audio"] = "0"
    elif audio_type == 2:
        query_params["enable_audio"] = "1"
        query_params["add_mute_audio"] = "1"

    response = await client.get(
        f"{ZLM_SERVER}/index/api/addStreamProxy", params=query_params
    )
    return response.json()


@app.delete("/api/stream/pull-proxy", summary="删除拉流代理", tags=["流"])
async def delete_pull_proxy(
    vhost: str = Query("__defaultVhost__", description="虚拟主机"),
    app: str = Query(..., description="应用名"),
    stream: str = Query(..., description="流id"),
):
    query_params = {"secret": ZLM_SECRET}
    query_params["key"] = f"{vhost}/{app}/{stream}"

    response = await client.get(
        f"{ZLM_SERVER}/index/api/delStreamProxy", params=query_params
    )
    return response.json()


@app.get("/api/stream/pull-proxy-list", summary="获取拉流代理列表", tags=["流"])
async def get_pull_proxy_list():
    query_params = {"secret": ZLM_SECRET}
    response = await client.get(
        f"{ZLM_SERVER}/index/api/listStreamProxy", params=query_params
    )
    return response.json()


@app.get("/api/stream/streamid-list", summary="获取当前在线流ID列表", tags=["流"])
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


@app.delete("/api/stream/streamid", tags=["流"], summary="删除在线流ID")
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
# playback
@app.get("/api/record/start-record", tags=["录制"], summary="开启录制")
async def get_start_record(
    vhost: str = Query("__defaultVhost__", description="虚拟主机"),
    app: str = Query(..., description="应用名"),
    stream: str = Query(..., description="流ID"),
    record_days: str = Query(..., description="录制天数"),
):
    stream_record_dir = RECORD_ROOT / app / stream

    date_pattern = re.compile(r"^\d{4}-\d{2}-\d{2}$")

    # 检查 streamid 目录下有没有 YYYY-MM-DD
    if stream_record_dir.exists():
        if any(
            item.is_dir() and date_pattern.match(item.name)
            for item in stream_record_dir.iterdir()
        ):
            return {"code": -1, "msg": "该流ID录像存在，为防止覆盖，请先删除"}

    url = f"{ZLM_SERVER}/index/api/startRecord"

    query = {"secret": ZLM_SECRET}
    query["vhost"] = str(vhost)
    query["app"] = str(app)
    query["stream"] = str(stream)
    query["type"] = "1"

    max_second = (int(record_days) * 24 * 60 * 60) / KEEP_VIDEOS
    query["max_second"] = str(max_second)

    response = await client.get(url, params=query)
    return response.json()


@app.get("/api/record/stop-record", tags=["录制"], summary="停止录制")
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
    return response.json()


@app.get("/api/record/event-record", tags=["录制"], summary="开启事件视频录制")
async def get_event_record(
    vhost: str = Query("__defaultVhost__", description="虚拟主机"),
    app: str = Query(..., description="应用名"),
    stream: str = Query(..., description="流ID"),
    path: str = Query(..., description="录像保存相对路径"),
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
    "/api/record/videos-list",
    tags=["录制"],
    summary="获取本地所有流ID的录制信息",
)
async def get_videos_list():
    result = []

    if not RECORD_ROOT.exists() or not RECORD_ROOT.is_dir():
        return {"code": -1, "msg": f"{RECORD_ROOT} 目录不存在或不是目录"}

    # 正则匹配 YYYY-MM-DD 格式
    date_pattern = re.compile(r"^(\d{4})-(\d{2})-(\d{2})$")

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

                result.append(
                    {
                        "app": app_name,
                        "stream": stream_name,
                        "slice_num": total_slices,
                        "total_storage_gb": round(total_size_bytes / (1024**3), 2),
                        "dates": sorted(dates),
                    }
                )

        return {"code": 0, "data": result}

    except Exception as e:
        return {"code": -1, "msg": f"目录遍历异常 {e}"}


@app.get("/api/record/videos", tags=["录制"], summary="获取指定流ID的全部录制信息")
async def get_videos(
    app: str = Query(..., description="应用名"),
    stream: str = Query(..., description="流ID"),
    date: str = Query(..., description="日期格式 YYYY-MM-DD"),
):
    target_dir = RECORD_ROOT / app / stream / date

    if not target_dir.exists():
        return {"code": 1, "msg": f"目录不存在: {target_dir}"}

    if not target_dir.is_dir():
        return {"code": 1, "msg": f"路径不是目录: {target_dir}"}

    results = []

    for file_path in target_dir.iterdir():
        if file_path.suffix.lower() == ".mp4":
            data = get_video_shanghai_time(file_path)
            if data:
                try:
                    # 计算相对路径：app/stream/date/filename.mp4
                    rel_path = file_path.relative_to(RECORD_ROOT)
                    data["filename"] = str(rel_path)
                except ValueError:
                    print(f"⚠️ 文件不在 RECORD_ROOT 下，跳过: {file_path}")
                    continue

                results.append(data)

    # 按开始时间排序
    results.sort(key=lambda x: x["start"])

    return {"code": 0, "data": results}


@app.delete("/api/record/videos", tags=["录制"], summary="删除指定流ID的全部录制文件")
async def delete_videos(
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


@app.get("/api/server/config", tags=["配置"], summary="获取服务器配置")
async def get_server_config():
    query_params = {"secret": ZLM_SECRET}
    response = await client.get(
        f"{ZLM_SERVER}/index/api/getServerConfig", params=query_params
    )
    return response.json()


@app.put("/api/server/config", tags=["配置"], summary="修改服务器配置")
async def put_server_config(request: Request):
    query_params = dict(request.query_params)
    query_params["secret"] = ZLM_SECRET

    response = await client.get(
        f"{ZLM_SERVER}/index/api/setServerConfig", params=query_params
    )
    return response.json()


app.include_router(onvif_router)

if __name__ == "__main__":
    import uvicorn

    uvicorn.run("main:app", host="0.0.0.0", port=10801, reload=True)
    # uvicorn.run("main:app", host="0.0.0.0", port=10801, reload=False)
