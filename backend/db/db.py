# db.py
import sqlite3
import contextlib
from typing import Generator
from fastapi import Depends

# 数据库路径
DATABASE_URL = "./db/test.db"


# 获取数据库连接（作为 FastAPI 的依赖）
@contextlib.contextmanager
def get_db() -> Generator[sqlite3.Connection, None, None]:
    conn = sqlite3.connect(DATABASE_URL)
    conn.row_factory = sqlite3.Row  # 支持按列名访问结果，如 row['id']
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


# 初始化表
def init_db():
    with get_db() as db:
        cursor = db.cursor()
        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS events_table (
                id INTEGER PRIMARY KEY,
                isProcess BOOLEAN DEFAULT FALSE,
                cameraIp TEXT NOT NULL,
                eventType TEXT NOT NULL,
                eventTime DATETIME NOT NULL,
                eventImage TEXT NOT NULL,
                eventVideo TEXT NOT NULL,
                isCorrect BOOLEAN DEFAULT FALSE
            )
        """
        )
