import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from typing import Iterator
from typing import TypedDict


DB_PATH = Path(__file__).resolve().parent / "streamui.db"


class PullProxyRow(TypedDict):
    vhost: str
    app: str
    stream: str
    url: str
    audio_type: int | None
    created_at: str
    updated_at: str


class RecordPolicyRow(TypedDict):
    vhost: str
    app: str
    stream: str
    retention_days: int
    enabled: int
    created_at: str
    updated_at: str


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@contextmanager
def get_db() -> Iterator[sqlite3.Connection]:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(DB_PATH), timeout=10, isolation_level=None)
    try:
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL;")
        conn.execute("PRAGMA foreign_keys=ON;")
        yield conn
    finally:
        conn.close()


def init_db() -> None:
    with get_db() as db:
        db.execute(
            """
            CREATE TABLE IF NOT EXISTS pull_proxy (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                vhost TEXT NOT NULL,
                app TEXT NOT NULL,
                stream TEXT NOT NULL,
                url TEXT NOT NULL,
                audio_type INTEGER,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                UNIQUE(vhost, app, stream)
            )
            """
        )
        db.execute(
            """
            CREATE TABLE IF NOT EXISTS record_policy (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                vhost TEXT NOT NULL,
                app TEXT NOT NULL,
                stream TEXT NOT NULL,
                retention_days INTEGER NOT NULL,
                enabled INTEGER NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                UNIQUE(vhost, app, stream)
            )
            """
        )


def list_pull_proxies() -> list[PullProxyRow]:
    with get_db() as db:
        rows = db.execute(
            """
            SELECT vhost, app, stream, url, audio_type, created_at, updated_at
            FROM pull_proxy
            ORDER BY id DESC
            """
        ).fetchall()

    return [dict(row) for row in rows]  # type: ignore[return-value]


def upsert_pull_proxy(
    *,
    vhost: str,
    app: str,
    stream: str,
    url: str,
    audio_type: int | None,
) -> dict[str, Any]:
    now = _utc_now_iso()
    with get_db() as db:
        try:
            db.execute(
                """
                INSERT INTO pull_proxy (vhost, app, stream, url, audio_type, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(vhost, app, stream) DO UPDATE SET
                    url=excluded.url,
                    audio_type=excluded.audio_type,
                    updated_at=excluded.updated_at
                """,
                (vhost, app, stream, url, audio_type, now, now),
            )
        except sqlite3.OperationalError:
            existing = db.execute(
                "SELECT 1 FROM pull_proxy WHERE vhost=? AND app=? AND stream=?",
                (vhost, app, stream),
            ).fetchone()
            if existing:
                db.execute(
                    """
                    UPDATE pull_proxy
                    SET url=?, audio_type=?, updated_at=?
                    WHERE vhost=? AND app=? AND stream=?
                    """,
                    (url, audio_type, now, vhost, app, stream),
                )
            else:
                db.execute(
                    """
                    INSERT INTO pull_proxy (vhost, app, stream, url, audio_type, created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (vhost, app, stream, url, audio_type, now, now),
                )

        row = db.execute(
            """
            SELECT vhost, app, stream, url, audio_type, created_at, updated_at
            FROM pull_proxy
            WHERE vhost=? AND app=? AND stream=?
            """,
            (vhost, app, stream),
        ).fetchone()

    return dict(row) if row else {}


def delete_pull_proxy(*, vhost: str, app: str, stream: str) -> int:
    with get_db() as db:
        cur = db.execute(
            "DELETE FROM pull_proxy WHERE vhost=? AND app=? AND stream=?",
            (vhost, app, stream),
        )
        return int(cur.rowcount or 0)


def list_record_policies(*, enabled_only: bool = False) -> list[RecordPolicyRow]:
    with get_db() as db:
        if enabled_only:
            rows = db.execute(
                """
                SELECT vhost, app, stream, retention_days, enabled, created_at, updated_at
                FROM record_policy
                WHERE enabled=1
                ORDER BY id DESC
                """
            ).fetchall()
        else:
            rows = db.execute(
                """
                SELECT vhost, app, stream, retention_days, enabled, created_at, updated_at
                FROM record_policy
                ORDER BY id DESC
                """
            ).fetchall()

    return [dict(row) for row in rows]  # type: ignore[return-value]


def get_record_policy(*, vhost: str, app: str, stream: str) -> RecordPolicyRow | None:
    with get_db() as db:
        row = db.execute(
            """
            SELECT vhost, app, stream, retention_days, enabled, created_at, updated_at
            FROM record_policy
            WHERE vhost=? AND app=? AND stream=?
            """,
            (vhost, app, stream),
        ).fetchone()
    return dict(row) if row else None  # type: ignore[return-value]


def upsert_record_policy(
    *,
    vhost: str,
    app: str,
    stream: str,
    retention_days: int,
    enabled: bool,
) -> dict[str, Any]:
    now = _utc_now_iso()
    enabled_int = 1 if enabled else 0
    with get_db() as db:
        try:
            db.execute(
                """
                INSERT INTO record_policy (vhost, app, stream, retention_days, enabled, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(vhost, app, stream) DO UPDATE SET
                    retention_days=excluded.retention_days,
                    enabled=excluded.enabled,
                    updated_at=excluded.updated_at
                """,
                (vhost, app, stream, int(retention_days), enabled_int, now, now),
            )
        except sqlite3.OperationalError:
            existing = db.execute(
                "SELECT 1 FROM record_policy WHERE vhost=? AND app=? AND stream=?",
                (vhost, app, stream),
            ).fetchone()
            if existing:
                db.execute(
                    """
                    UPDATE record_policy
                    SET retention_days=?, enabled=?, updated_at=?
                    WHERE vhost=? AND app=? AND stream=?
                    """,
                    (int(retention_days), enabled_int, now, vhost, app, stream),
                )
            else:
                db.execute(
                    """
                    INSERT INTO record_policy (vhost, app, stream, retention_days, enabled, created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (vhost, app, stream, int(retention_days), enabled_int, now, now),
                )

        row = db.execute(
            """
            SELECT vhost, app, stream, retention_days, enabled, created_at, updated_at
            FROM record_policy
            WHERE vhost=? AND app=? AND stream=?
            """,
            (vhost, app, stream),
        ).fetchone()

    return dict(row) if row else {}


def delete_record_policy(*, vhost: str, app: str, stream: str) -> int:
    with get_db() as db:
        cur = db.execute(
            "DELETE FROM record_policy WHERE vhost=? AND app=? AND stream=?",
            (vhost, app, stream),
        )
        return int(cur.rowcount or 0)
