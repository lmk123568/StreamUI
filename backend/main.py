import os
import re
import shutil
import sqlite3
from contextlib import asynccontextmanager, contextmanager
from datetime import datetime
from pathlib import Path

import httpx
import psutil
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from fastapi import FastAPI, Query, Request
from fastapi.middleware.cors import CORSMiddleware

# from onvif.api import router as onvif_router
from scheduler import cleanup_old_videos
from utils import get_video_shanghai_time, get_zlm_secret

# 数据库相关配置
DB_PATH = Path("settings.db")

# 数据库连接池
class DatabasePool:
    def __init__(self, db_path, max_connections=5):
        self.db_path = db_path
        self.max_connections = max_connections
        self.connections = []
        
    def get_connection(self):
        if self.connections:
            return self.connections.pop()
        return sqlite3.connect(self.db_path)
    
    def return_connection(self, conn):
        if len(self.connections) < self.max_connections:
            self.connections.append(conn)
        else:
            conn.close()
    
    def close_all(self):
        for conn in self.connections:
            try:
                conn.close()
            except:
                pass
        self.connections = []

# 创建数据库连接池
db_pool = DatabasePool(DB_PATH)

# 数据库连接上下文管理器
@contextmanager
def get_db_connection():
    conn = db_pool.get_connection()
    try:
        yield conn
    finally:
        db_pool.return_connection(conn)

# 初始化数据库
def init_db():
    """初始化数据库，创建表结构并插入默认配置"""
    if not DB_PATH.parent.exists():
        DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    
    with get_db_connection() as conn:
        cursor = conn.cursor()
        
        # 创建拉流配置表
        cursor.execute('''
        CREATE TABLE IF NOT EXISTS pull_streams (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            vhost TEXT NOT NULL DEFAULT '__defaultVhost__',
            app TEXT NOT NULL,
            stream TEXT NOT NULL,
            url TEXT NOT NULL,
            enable_audio INTEGER NOT NULL DEFAULT 0,
            add_mute_audio INTEGER NOT NULL DEFAULT 1,
            rtp_type INTEGER NOT NULL DEFAULT 0,
            timeout_sec REAL NOT NULL DEFAULT 15,
            retry_count INTEGER NOT NULL DEFAULT -1,
            enable_mp4 INTEGER NOT NULL DEFAULT 0,
            enable_rtsp INTEGER NOT NULL DEFAULT 0,
            enable_rtmp INTEGER NOT NULL DEFAULT 1,
            enable_hls INTEGER NOT NULL DEFAULT 0,
            enable_hls_fmp4 INTEGER NOT NULL DEFAULT 0,
            enable_ts INTEGER NOT NULL DEFAULT 0,
            enable_fmp4 INTEGER NOT NULL DEFAULT 1,
            hls_demand INTEGER NOT NULL DEFAULT 0,
            rtsp_demand INTEGER NOT NULL DEFAULT 0,
            rtmp_demand INTEGER NOT NULL DEFAULT 0,
            ts_demand INTEGER NOT NULL DEFAULT 0,
            fmp4_demand INTEGER NOT NULL DEFAULT 0,
            mp4_max_second INTEGER NOT NULL DEFAULT 30,
            mp4_as_player INTEGER NOT NULL DEFAULT 0,
            modify_stamp INTEGER NOT NULL DEFAULT 0,
            auto_close INTEGER NOT NULL DEFAULT 0,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(vhost, app, stream)
        )
        ''')
        
        # 创建默认配置表
        cursor.execute('''
        CREATE TABLE IF NOT EXISTS default_configs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            key TEXT NOT NULL UNIQUE,
            value TEXT NOT NULL,
            description TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
        ''')
        
        # 插入默认配置数据
        default_configs = [
            ("vhost", "__defaultVhost__", "虚拟主机名"),
            ("app", "live", "应用名"),
            ("enable_audio", "0", "是否启用音频"),
            ("add_mute_audio", "1", "是否添加静音音频"),
            ("rtp_type", "0", "rtsp拉流时，拉流方式，0：tcp，1：udp，2：组播"),
            ("timeout_sec", "15", "拉流超时时间，单位秒"),
            ("retry_count", "-1", "拉流重试次数，默认为-1无限重试"),
            ("enable_mp4", "0", "是否允许mp4录制"),
            ("enable_rtsp", "0", "是否转rtsp协议"),
            ("enable_rtmp", "1", "是否转rtmp/flv协议"),
            ("enable_hls", "0", "是否转换成hls-mpegts协议"),
            ("enable_hls_fmp4", "0", "是否转换成hls-fmp4协议"),
            ("enable_ts", "0", "是否转http-ts/ws-ts协议"),
            ("enable_fmp4", "1", "是否转http-fmp4/ws-fmp4协议"),
            ("hls_demand", "0", "是否启用HLS按需模式"),
            ("rtsp_demand", "0", "是否启用RTSP按需模式"),
            ("rtmp_demand", "0", "是否启用RTMP按需模式"),
            ("ts_demand", "0", "是否启用TS按需模式"),
            ("fmp4_demand", "0", "是否启用fMP4按需模式"),
            ("mp4_max_second", "30", "mp4录制切片大小，单位秒"),
            ("mp4_as_player", "0", "MP4录制是否当作观看者参与播放人数计数"),
            ("modify_stamp", "1", "是否开启时间戳覆盖(0:绝对时间戳/1:系统时间戳/2:相对时间戳)"),
            ("auto_close", "0", "无人观看是否自动关闭流(不触发无人观看hook)")
        ]
        
        for key, value, description in default_configs:
            cursor.execute('''
            INSERT OR IGNORE INTO default_configs (key, value, description)
            VALUES (?, ?, ?)
            ''', (key, value, description))
        
        conn.commit()
    print("[Database] 🚀 数据库初始化完成")

# 执行数据库初始化
init_db()

# =========================================================
# zlmediakit 地址
ZLM_SERVER = "http://127.0.0.1:8080"
# zlmediakit 密钥
try:
    ZLM_SECRET = get_zlm_secret("/opt/media/conf/config.ini")
except (FileNotFoundError, ValueError):
    # 配置文件不存在时使用默认值
    ZLM_SECRET = "035c73f7-bb6b-4889-a715-d9eb2d1925cc"
    print("[Warning] 使用默认的 ZLM_SECRET 值，这仅用于测试")
# zlmediakit 录像回放
RECORD_ROOT = Path("/opt/media/bin/www/record")
# 录像最大切片数
KEEP_VIDEOS = 72
# =========================================================


async def load_pull_proxy_on_startup():
    """应用启动时从数据库加载拉流配置"""
    print("[Startup] 🚀 正在从数据库加载拉流配置...")
    
    try:
        with get_db_connection() as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            
            # 先获取数据库中的所有配置
            cursor.execute('''
            SELECT * FROM pull_streams
            ''')
            rows = cursor.fetchall()
            
            if not rows:
                print("[Startup] ℹ️ 数据库中没有拉流配置")
                return
            
            success_count = 0
            fail_count = 0
            
            # 逐个更新到 ZLMediaKit
            for row in rows:
                stream_config = dict(row)
                query_params = {
                    "secret": ZLM_SECRET,
                    "vhost": stream_config["vhost"],
                    "app": stream_config["app"],
                    "stream": stream_config["stream"],
                    "url": stream_config["url"],
                    "enable_audio": str(stream_config["enable_audio"]),
                    "add_mute_audio": str(stream_config["add_mute_audio"]),
                    "rtp_type": str(stream_config["rtp_type"]),
                    "timeout_sec": str(stream_config["timeout_sec"]),
                    "retry_count": str(stream_config["retry_count"]),
                    "enable_mp4": str(stream_config["enable_mp4"]),
                    "enable_rtsp": str(stream_config["enable_rtsp"]),
                    "enable_rtmp": str(stream_config["enable_rtmp"]),
                    "enable_hls": str(stream_config["enable_hls"]),
                    "enable_hls_fmp4": str(stream_config["enable_hls_fmp4"]),
                    "enable_ts": str(stream_config["enable_ts"]),
                    "enable_fmp4": str(stream_config["enable_fmp4"]),
                    "hls_demand": str(stream_config["hls_demand"]),
                    "rtsp_demand": str(stream_config["rtsp_demand"]),
                    "rtmp_demand": str(stream_config["rtmp_demand"]),
                    "ts_demand": str(stream_config["ts_demand"]),
                    "fmp4_demand": str(stream_config["fmp4_demand"]),
                    "mp4_max_second": str(stream_config["mp4_max_second"]),
                    "mp4_as_player": str(stream_config["mp4_as_player"]),
                    "modify_stamp": str(stream_config["modify_stamp"]),
                    "auto_close": str(stream_config["auto_close"]),
                }
                
                try:
                    response = await client.get(
                        f"{ZLM_SERVER}/index/api/addStreamProxy", params=query_params
                    )
                    result = response.json()
                    
                    if result.get("code") == 0:
                        success_count += 1
                    else:
                        fail_count += 1
                        print(f"[Startup] ❌ 加载拉流配置失败: {stream_config['app']}/{stream_config['stream']} - {result.get('msg')}")
                except Exception as e:
                    fail_count += 1
                    print(f"[Startup] ❌ 加载拉流配置异常: {stream_config['app']}/{stream_config['stream']} - {e}")
        
        print(f"[Startup] 🎉 拉流配置加载完成，成功: {success_count}, 失败: {fail_count}")
    except Exception as e:
        print(f"[Startup] ❌ 从数据库加载配置失败: {e}")


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

    # 启动时从数据库加载拉流配置
    await load_pull_proxy_on_startup()

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
    summary="获取当前系统资源使用率",
)
async def get_host_stats():
    timestamp = datetime.now().strftime("%H:%M:%S")

    # CPU 使用率
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
    enable_audio: int = Query(0, description="是否启用音频"),
    add_mute_audio: int = Query(1, description="是否添加静音音频"),
    rtp_type: int = Query(0, description="rtsp拉流时，拉流方式"),
    timeout_sec: float = Query(15, description="拉流超时时间，单位秒"),
    retry_count: int = Query(-1, description="拉流重试次数"),
    enable_mp4: int = Query(0, description="是否允许mp4录制"),
    enable_rtsp: int = Query(0, description="是否转rtsp协议"),
    enable_rtmp: int = Query(1, description="是否转rtmp/flv协议"),
    enable_hls: int = Query(0, description="是否转换成hls-mpegts协议"),
    enable_hls_fmp4: int = Query(0, description="是否转换成hls-fmp4协议"),
    enable_ts: int = Query(0, description="是否转http-ts/ws-ts协议"),
    enable_fmp4: int = Query(1, description="是否转http-fmp4/ws-fmp4协议"),
    hls_demand: int = Query(0, description="是否启用HLS按需模式"),
    rtsp_demand: int = Query(0, description="是否启用RTSP按需模式"),
    rtmp_demand: int = Query(0, description="是否启用RTMP按需模式"),
    ts_demand: int = Query(0, description="是否启用TS按需模式"),
    fmp4_demand: int = Query(0, description="是否启用fMP4按需模式"),
    mp4_max_second: int = Query(30, description="mp4录制切片大小，单位秒"),
    mp4_as_player: int = Query(0, description="MP4录制是否当作观看者参与播放人数计数"),
    modify_stamp: int = Query(1, description="是否开启时间戳覆盖"),
    auto_close: int = Query(0, description="无人观看是否自动关闭流"),
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

    # 处理 audio_type 映射（兼容旧接口）
    if audio_type is not None:
        if audio_type == 0:
            enable_audio = 0
            add_mute_audio = 0
        elif audio_type == 1:
            enable_audio = 1
            add_mute_audio = 0
        elif audio_type == 2:
            enable_audio = 1
            add_mute_audio = 1

    # 构造转发请求
    query_params = {
        "secret": ZLM_SECRET,
        "vhost": vhost,
        "app": app,
        "stream": stream,
        "url": url,
        "enable_audio": str(enable_audio),
        "add_mute_audio": str(add_mute_audio),
        "rtp_type": str(rtp_type),
        "timeout_sec": str(timeout_sec),
        "retry_count": str(retry_count),
        "enable_mp4": str(enable_mp4),
        "enable_rtsp": str(enable_rtsp),
        "enable_rtmp": str(enable_rtmp),
        "enable_hls": str(enable_hls),
        "enable_hls_fmp4": str(enable_hls_fmp4),
        "enable_ts": str(enable_ts),
        "enable_fmp4": str(enable_fmp4),
        "hls_demand": str(hls_demand),
        "rtsp_demand": str(rtsp_demand),
        "rtmp_demand": str(rtmp_demand),
        "ts_demand": str(ts_demand),
        "fmp4_demand": str(fmp4_demand),
        "mp4_max_second": str(mp4_max_second),
        "mp4_as_player": str(mp4_as_player),
        "modify_stamp": str(modify_stamp),
        "auto_close": str(auto_close),
    }

    # 先尝试删除已存在的流（如果存在）
    del_params = {
        "secret": ZLM_SECRET,
        "key": f"{vhost}/{app}/{stream}"
    }
    await client.get(
        f"{ZLM_SERVER}/index/api/delStreamProxy", params=del_params
    )

    # 发送请求到 ZLMediaKit 添加新流
    response = await client.get(
        f"{ZLM_SERVER}/index/api/addStreamProxy", params=query_params
    )
    result = response.json()

    # 如果成功，保存到数据库
    if result.get("code") == 0:
        try:
            with get_db_connection() as conn:
                cursor = conn.cursor()
                
                # 先检查记录是否存在
                cursor.execute('''
                SELECT id FROM pull_streams WHERE vhost = ? AND app = ? AND stream = ?
                ''', (vhost, app, stream))
                existing_record = cursor.fetchone()
                
                if existing_record:
                    # 记录存在，使用 UPDATE 语句更新
                    cursor.execute('''
                    UPDATE pull_streams SET 
                    url = ?, enable_audio = ?, add_mute_audio = ?, rtp_type = ?, timeout_sec = ?, 
                    retry_count = ?, enable_mp4 = ?, enable_rtsp = ?, enable_rtmp = ?, enable_hls = ?, 
                    enable_hls_fmp4 = ?, enable_ts = ?, enable_fmp4 = ?, hls_demand = ?, rtsp_demand = ?, 
                    rtmp_demand = ?, ts_demand = ?, fmp4_demand = ?, mp4_max_second = ?, mp4_as_player = ?, 
                    modify_stamp = ?, auto_close = ?, updated_at = CURRENT_TIMESTAMP
                    WHERE vhost = ? AND app = ? AND stream = ?
                    ''', (
                        url, enable_audio, add_mute_audio, rtp_type, timeout_sec,
                        retry_count, enable_mp4, enable_rtsp, enable_rtmp, enable_hls,
                        enable_hls_fmp4, enable_ts, enable_fmp4, hls_demand, rtsp_demand,
                        rtmp_demand, ts_demand, fmp4_demand, mp4_max_second, mp4_as_player,
                        modify_stamp, auto_close, vhost, app, stream
                    ))
                else:
                    # 记录不存在，使用 INSERT 语句插入
                    cursor.execute('''
                    INSERT INTO pull_streams 
                    (vhost, app, stream, url, enable_audio, add_mute_audio, rtp_type, timeout_sec, 
                     retry_count, enable_mp4, enable_rtsp, enable_rtmp, enable_hls, enable_hls_fmp4, 
                     enable_ts, enable_fmp4, hls_demand, rtsp_demand, rtmp_demand, ts_demand, 
                     fmp4_demand, mp4_max_second, mp4_as_player, modify_stamp, auto_close, 
                     created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
                    ''', (
                        vhost, app, stream, url, enable_audio, add_mute_audio, rtp_type, timeout_sec,
                        retry_count, enable_mp4, enable_rtsp, enable_rtmp, enable_hls, enable_hls_fmp4,
                        enable_ts, enable_fmp4, hls_demand, rtsp_demand, rtmp_demand, ts_demand,
                        fmp4_demand, mp4_max_second, mp4_as_player, modify_stamp, auto_close
                    ))
                
                conn.commit()
                print(f"[Database] 📝 拉流配置已保存: {app}/{stream}")
        except Exception as e:
            print(f"[Database] ❌ 保存拉流配置失败: {e}")

    return result


@app.delete("/api/stream/pull-proxy", summary="删除拉流代理", tags=["流"])
async def delete_pull_proxy(
    vhost: str = Query("__defaultVhost__", description="虚拟主机"),
    app: str = Query(..., description="应用名"),
    stream: str = Query(..., description="流id"),
):
    query_params = {"secret": ZLM_SECRET}
    query_params["key"] = f"{vhost}/{app}/{stream}"

    # 发送请求到 ZLMediaKit
    response = await client.get(
        f"{ZLM_SERVER}/index/api/delStreamProxy", params=query_params
    )
    result = response.json()

    # 如果成功，从数据库中删除
    if result.get("code") == 0:
        try:
            with get_db_connection() as conn:
                cursor = conn.cursor()
                
                cursor.execute('''
                DELETE FROM pull_streams WHERE vhost = ? AND app = ? AND stream = ?
                ''', (vhost, app, stream))
                conn.commit()
                print(f"[Database] 🗑️ 拉流配置已删除: {app}/{stream}")
        except Exception as e:
            print(f"[Database] ❌ 删除拉流配置失败: {e}")

    return result


@app.get("/api/stream/pull-proxy-list", summary="获取拉流代理列表", tags=["流"])
async def get_pull_proxy_list():
    query_params = {"secret": ZLM_SECRET}
    response = await client.get(
        f"{ZLM_SERVER}/index/api/listStreamProxy", params=query_params
    )
    return response.json()


@app.get("/api/stream/pull-proxy-list-db", summary="获取数据库中的拉流代理列表", tags=["流"])
async def get_pull_proxy_list_db():
    """从数据库中获取拉流代理列表"""
    try:
        with get_db_connection() as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            
            cursor.execute('''
            SELECT * FROM pull_streams ORDER BY id DESC
            ''')
            rows = cursor.fetchall()
            
            data = []
            for row in rows:
                data.append(dict(row))
            
            return {"code": 0, "data": data}
    except Exception as e:
        return {"code": -1, "msg": f"获取数据库拉流列表失败: {e}"}


@app.post("/api/stream/pull-proxy-load", summary="从数据库加载拉流配置到服务器", tags=["流"])
async def load_pull_proxy_from_db():
    """从数据库加载所有拉流配置并更新到ZLMediaKit服务器"""
    try:
        with get_db_connection() as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            
            # 先获取数据库中的所有配置
            cursor.execute('''
            SELECT * FROM pull_streams
            ''')
            rows = cursor.fetchall()
            
            results = []
            success_count = 0
            fail_count = 0
            
            # 逐个更新到 ZLMediaKit
            for row in rows:
                stream_config = dict(row)
                query_params = {
                    "secret": ZLM_SECRET,
                    "vhost": stream_config["vhost"],
                    "app": stream_config["app"],
                    "stream": stream_config["stream"],
                    "url": stream_config["url"],
                    "enable_audio": str(stream_config["enable_audio"]),
                    "add_mute_audio": str(stream_config["add_mute_audio"]),
                    "rtp_type": str(stream_config["rtp_type"]),
                    "timeout_sec": str(stream_config["timeout_sec"]),
                    "retry_count": str(stream_config["retry_count"]),
                    "enable_mp4": str(stream_config["enable_mp4"]),
                    "enable_rtsp": str(stream_config["enable_rtsp"]),
                    "enable_rtmp": str(stream_config["enable_rtmp"]),
                    "enable_hls": str(stream_config["enable_hls"]),
                    "enable_hls_fmp4": str(stream_config["enable_hls_fmp4"]),
                    "enable_ts": str(stream_config["enable_ts"]),
                    "enable_fmp4": str(stream_config["enable_fmp4"]),
                    "hls_demand": str(stream_config["hls_demand"]),
                    "rtsp_demand": str(stream_config["rtsp_demand"]),
                    "rtmp_demand": str(stream_config["rtmp_demand"]),
                    "ts_demand": str(stream_config["ts_demand"]),
                    "fmp4_demand": str(stream_config["fmp4_demand"]),
                    "mp4_max_second": str(stream_config["mp4_max_second"]),
                    "mp4_as_player": str(stream_config["mp4_as_player"]),
                    "modify_stamp": str(stream_config["modify_stamp"]),
                    "auto_close": str(stream_config["auto_close"]),
                }
                
                response = await client.get(
                    f"{ZLM_SERVER}/index/api/addStreamProxy", params=query_params
                )
                result = response.json()
                
                if result.get("code") == 0:
                    success_count += 1
                else:
                    fail_count += 1
                
                results.append({
                    "app": stream_config["app"],
                    "stream": stream_config["stream"],
                    "result": result
                })
        
        return {
            "code": 0,
            "msg": f"从数据库加载配置完成，成功: {success_count}, 失败: {fail_count}",
            "data": results
        }
    except Exception as e:
        return {"code": -1, "msg": f"从数据库加载配置失败: {e}"}


@app.get("/api/stream/pull-proxy-export", summary="导出拉流配置为CSV文件", tags=["流"])
async def export_pull_proxy():
    """导出数据库中的拉流配置为CSV格式"""
    try:
        with get_db_connection() as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            
            cursor.execute('''
            SELECT * FROM pull_streams
            ''')
            rows = cursor.fetchall()
            
            if not rows:
                return {"code": -1, "msg": "数据库中没有拉流配置"}
            
            # 生成CSV内容
            import csv
            import io
            
            output = io.StringIO()
            writer = csv.DictWriter(output, fieldnames=rows[0].keys())
            writer.writeheader()
            for row in rows:
                writer.writerow(dict(row))
            
            csv_content = output.getvalue()
            
            # 这里返回CSV内容，前端可以处理下载
            return {
                "code": 0,
                "data": csv_content,
                "msg": f"成功导出 {len(rows)} 条拉流配置"
            }
    except Exception as e:
        return {"code": -1, "msg": f"导出配置失败: {e}"}


@app.post("/api/stream/pull-proxy-import", summary="从CSV文件导入拉流配置", tags=["流"])
async def import_pull_proxy(request: Request):
    """从CSV文件导入拉流配置并更新到服务器"""
    try:
        data = await request.json()
        csv_content = data.get("csv_content")
        
        if not csv_content:
            return {"code": -1, "msg": "缺少CSV内容"}
        
        # 解析CSV内容
        import csv
        import io
        
        reader = csv.DictReader(io.StringIO(csv_content))
        configs = list(reader)
        
        if not configs:
            return {"code": -1, "msg": "CSV文件中没有配置数据"}
        
        # 导入到数据库并更新到服务器
        success_count = 0
        fail_count = 0
        results = []
        
        try:
            with get_db_connection() as conn:
                cursor = conn.cursor()
                
                for config in configs:
                    # 验证必要字段
                    if not all(key in config for key in ["app", "stream", "url"]):
                        fail_count += 1
                        results.append({
                            "app": config.get("app", ""),
                            "stream": config.get("stream", ""),
                            "result": {"code": -1, "msg": "缺少必要字段"}
                        })
                        continue
                    
                    # 转换字段类型
                    try:
                        # 保存到数据库
                        cursor.execute('''
                        INSERT OR REPLACE INTO pull_streams 
                        (vhost, app, stream, url, enable_audio, add_mute_audio, rtp_type, timeout_sec, 
                         retry_count, enable_mp4, enable_rtsp, enable_rtmp, enable_hls, enable_hls_fmp4, 
                         enable_ts, enable_fmp4, hls_demand, rtsp_demand, rtmp_demand, ts_demand, 
                         fmp4_demand, mp4_max_second, mp4_as_player, modify_stamp, auto_close, updated_at)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
                        ''', (
                            config.get("vhost", "__defaultVhost__"),
                            config.get("app"),
                            config.get("stream"),
                            config.get("url"),
                            int(config.get("enable_audio", 0)),
                            int(config.get("add_mute_audio", 1)),
                            int(config.get("rtp_type", 0)),
                            float(config.get("timeout_sec", 15)),
                            int(config.get("retry_count", -1)),
                            int(config.get("enable_mp4", 0)),
                            int(config.get("enable_rtsp", 0)),
                            int(config.get("enable_rtmp", 1)),
                            int(config.get("enable_hls", 0)),
                            int(config.get("enable_hls_fmp4", 0)),
                            int(config.get("enable_ts", 0)),
                            int(config.get("enable_fmp4", 1)),
                            int(config.get("hls_demand", 0)),
                            int(config.get("rtsp_demand", 0)),
                            int(config.get("rtmp_demand", 0)),
                            int(config.get("ts_demand", 0)),
                            int(config.get("fmp4_demand", 0)),
                            int(config.get("mp4_max_second", 30)),
                            int(config.get("mp4_as_player", 0)),
                            int(config.get("modify_stamp", 1)),
                            int(config.get("auto_close", 0))
                        ))
                        
                        # 更新到ZLMediaKit
                        query_params = {
                            "secret": ZLM_SECRET,
                            "vhost": config.get("vhost", "__defaultVhost__"),
                            "app": config.get("app"),
                            "stream": config.get("stream"),
                            "url": config.get("url"),
                            "enable_audio": str(config.get("enable_audio", 0)),
                            "add_mute_audio": str(config.get("add_mute_audio", 1)),
                            "rtp_type": str(config.get("rtp_type", 0)),
                            "timeout_sec": str(config.get("timeout_sec", 15)),
                            "retry_count": str(config.get("retry_count", -1)),
                            "enable_mp4": str(config.get("enable_mp4", 0)),
                            "enable_rtsp": str(config.get("enable_rtsp", 0)),
                            "enable_rtmp": str(config.get("enable_rtmp", 1)),
                            "enable_hls": str(config.get("enable_hls", 0)),
                            "enable_hls_fmp4": str(config.get("enable_hls_fmp4", 0)),
                            "enable_ts": str(config.get("enable_ts", 0)),
                            "enable_fmp4": str(config.get("enable_fmp4", 1)),
                            "hls_demand": str(config.get("hls_demand", 0)),
                            "rtsp_demand": str(config.get("rtsp_demand", 0)),
                            "rtmp_demand": str(config.get("rtmp_demand", 0)),
                            "ts_demand": str(config.get("ts_demand", 0)),
                            "fmp4_demand": str(config.get("fmp4_demand", 0)),
                            "mp4_max_second": str(config.get("mp4_max_second", 30)),
                            "mp4_as_player": str(config.get("mp4_as_player", 0)),
                            "modify_stamp": str(config.get("modify_stamp", 1)),
                            "auto_close": str(config.get("auto_close", 0)),
                        }
                        
                        response = await client.get(
                            f"{ZLM_SERVER}/index/api/addStreamProxy", params=query_params
                        )
                        result = response.json()
                        
                        if result.get("code") == 0:
                            success_count += 1
                        else:
                            fail_count += 1
                        
                        results.append({
                            "app": config.get("app"),
                            "stream": config.get("stream"),
                            "result": result
                        })
                    except Exception as e:
                        fail_count += 1
                        results.append({
                            "app": config.get("app", ""),
                            "stream": config.get("stream", ""),
                            "result": {"code": -1, "msg": f"处理失败: {e}"}
                        })
                
                conn.commit()
                
                return {
                    "code": 0,
                    "msg": f"导入配置完成，成功: {success_count}, 失败: {fail_count}",
                    "data": results
                }
        except Exception as e:
            return {"code": -1, "msg": f"导入配置失败: {e}"}
    except Exception as e:
        return {"code": -1, "msg": f"导入配置失败: {e}"}


@app.get("/api/stream/default-configs", summary="获取默认配置值", tags=["流"])
async def get_default_configs():
    """获取默认配置值"""
    try:
        with get_db_connection() as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            
            cursor.execute('''
            SELECT * FROM default_configs
            ''')
            rows = cursor.fetchall()
            
            data = {}
            for row in rows:
                data[row["key"]] = row["value"]
            
            return {"code": 0, "data": data}
    except Exception as e:
        return {"code": -1, "msg": f"获取默认配置失败: {e}"}


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
@app.get("/api/playback/start-record", tags=["录制"], summary="开启录制")
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


@app.get("/api/playback/stop-record", tags=["录制"], summary="停止录制")
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


@app.get("/api/playback/event-record", tags=["录制"], summary="开启事件视频录制")
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
    tags=["录制"],
    summary="获取本地所有流ID的录制信息",
)
async def get_streamid_record_list():
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


@app.get(
    "/api/playback/streamid-record", tags=["录制"], summary="获取指定流ID的全部录制信息"
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


@app.delete(
    "/api/playback/streamid-record", tags=["录制"], summary="删除指定流ID的全部录制文件"
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


# app.include_router(onvif_router)

if __name__ == "__main__":
    import uvicorn

    uvicorn.run("main:app", host="0.0.0.0", port=10801, reload=True)
    # uvicorn.run("main:app", host="0.0.0.0", port=10801, reload=False)
