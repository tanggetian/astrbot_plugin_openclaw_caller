"""SQLite 存储层——插件独立 DB、openclaw_sessions + openclaw_tasks 两张表。

提供：
- 路径与初始化：get_db_path, init_db, _connect
- session 持久化（给 call_openclaw 用，审计最近 1 轮）：load_session, save_session, clear_session
- TaskLog 类：任务生命周期的统一抽象（create / update / get / list / delete）

schema 故意保持原样（task_id TEXT PK, finished_at REAL 等）——不动 P1 项。
"""
from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path
from typing import Any

from astrbot.core.utils.astrbot_path import get_astrbot_data_path

from .util import PLUGIN_NAME


def get_db_path() -> Path:
    """获取插件独立 SQLite 数据库路径。

    放在 ``<data_dir>/plugins/<PLUGIN_NAME>/openclaw_caller.db``，不污染 AstrBot 主库。
    """
    data_dir = Path(get_astrbot_data_path()) / "plugins" / PLUGIN_NAME
    data_dir.mkdir(parents=True, exist_ok=True)
    return data_dir / "openclaw_caller.db"


def init_db() -> None:
    """确保 openclaw_sessions / openclaw_tasks 两张表存在。"""
    db = get_db_path()
    conn = sqlite3.connect(str(db))
    try:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS openclaw_sessions (
                user_id    TEXT NOT NULL,
                session_key TEXT NOT NULL,
                messages   TEXT NOT NULL,
                updated_at INTEGER NOT NULL,
                PRIMARY KEY (user_id, session_key)
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS openclaw_tasks (
                task_id      TEXT PRIMARY KEY,
                user_id      TEXT NOT NULL,
                project      TEXT NOT NULL,
                task_text    TEXT NOT NULL,
                status       TEXT NOT NULL,
                mode         TEXT NOT NULL,
                created_at   REAL NOT NULL,
                finished_at  REAL,
                result_text  TEXT,
                error_text   TEXT
            )
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_tasks_created ON openclaw_tasks(created_at DESC)")
        conn.commit()
    finally:
        conn.close()


def _connect() -> sqlite3.Connection:
    return sqlite3.connect(str(get_db_path()))


def load_session(user_id: str, session_key: str) -> list[dict]:
    """加载主人在这个 session_key 的历史 messages。"""
    init_db()
    conn = _connect()
    try:
        row = conn.execute(
            "SELECT messages FROM openclaw_sessions WHERE user_id=? AND session_key=?",
            (user_id, session_key),
        ).fetchone()
    finally:
        conn.close()
    if row:
        try:
            return json.loads(row[0])
        except (json.JSONDecodeError, TypeError):
            return []
    return []


def save_session(user_id: str, session_key: str, messages: list[dict]) -> None:
    """保存历史到 SQLite。"""
    init_db()
    conn = _connect()
    try:
        conn.execute(
            "INSERT OR REPLACE INTO openclaw_sessions (user_id, session_key, messages, updated_at) "
            "VALUES (?,?,?,?)",
            (user_id, session_key, json.dumps(messages, ensure_ascii=False), int(time.time())),
        )
        conn.commit()
    finally:
        conn.close()


def clear_session(user_id: str, session_key: str) -> None:
    """清空会话历史（/oc reset 用）。"""
    init_db()
    conn = _connect()
    try:
        conn.execute(
            "DELETE FROM openclaw_sessions WHERE user_id=? AND session_key=?",
            (user_id, session_key),
        )
        conn.commit()
    finally:
        conn.close()


class TaskLog:
    """任务审计 / 列表的 SQLite 封装。

    设计目标：
    - 把 4 个散落的模块级 _db_* 函数收口到一个类
    - 不改 schema / 行为，纯结构封装
    - 错误一律 logger.warning 兜底（不抛——避免影响主流程）

    字段（与原 _db_* 完全一致）：
    - task_id (TEXT PK)
    - user_id, project, task_text, status, mode, created_at, finished_at, result_text, error_text
    """

    @staticmethod
    def _connect_with_init() -> sqlite3.Connection:
        init_db()
        return _connect()

    def insert(self, info: dict) -> None:
        try:
            with self._connect_with_init() as conn:
                conn.execute(
                    "INSERT OR REPLACE INTO openclaw_tasks "
                    "(task_id,user_id,project,task_text,status,mode,"
                    "created_at,finished_at,result_text,error_text) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?)",
                    (
                        info["task_id"], info["user_id"], info["project"],
                        info["task_text"], info["status"], info["mode"],
                        info["created_at"], info["finished_at"],
                        info.get("result_text"), info.get("error_text"),
                    ),
                )
                conn.commit()
        except Exception as e:
            from astrbot.api import logger
            logger.warning(f"[openclaw_caller] TaskLog.insert 失败: {e}")

    def update(self, info: dict) -> None:
        try:
            with self._connect_with_init() as conn:
                conn.execute(
                    "UPDATE openclaw_tasks SET status=?, finished_at=?, "
                    "result_text=?, error_text=? WHERE task_id=?",
                    (
                        info["status"], info["finished_at"],
                        info.get("result_text"), info.get("error_text"),
                        info["task_id"],
                    ),
                )
                conn.commit()
        except Exception as e:
            from astrbot.api import logger
            logger.warning(f"[openclaw_caller] TaskLog.update 失败: {e}")

    def get_for_user(
        self, user_id: str, task_id: str = "", project: str = ""
    ) -> dict[str, Any] | None:
        try:
            with self._connect_with_init() as conn:
                conn.row_factory = sqlite3.Row
                if task_id:
                    row = conn.execute(
                        "SELECT task_id,user_id,project,task_text,status,mode,"
                        "created_at,finished_at,result_text,error_text "
                        "FROM openclaw_tasks WHERE user_id=? AND task_id=?",
                        (user_id, task_id),
                    ).fetchone()
                elif project:
                    row = conn.execute(
                        "SELECT task_id,user_id,project,task_text,status,mode,"
                        "created_at,finished_at,result_text,error_text "
                        "FROM openclaw_tasks WHERE user_id=? AND project=? "
                        "ORDER BY created_at DESC LIMIT 1",
                        (user_id, project),
                    ).fetchone()
                else:
                    row = conn.execute(
                        "SELECT task_id,user_id,project,task_text,status,mode,"
                        "created_at,finished_at,result_text,error_text "
                        "FROM openclaw_tasks WHERE user_id=? "
                        "ORDER BY created_at DESC LIMIT 1",
                        (user_id,),
                    ).fetchone()
                return dict(row) if row else None
        except Exception as e:
            from astrbot.api import logger
            logger.warning(f"[openclaw_caller] TaskLog.get_for_user 失败: {e}")
            return None

    def list_recent(self, limit: int = 200) -> list[dict[str, Any]]:
        try:
            with self._connect_with_init() as conn:
                conn.row_factory = sqlite3.Row
                rows = conn.execute(
                    "SELECT task_id,project,task_text,status,mode,"
                    "created_at,finished_at "
                    "FROM openclaw_tasks ORDER BY created_at DESC LIMIT ?",
                    (limit,),
                ).fetchall()
            return [dict(r) for r in rows]
        except Exception as e:
            from astrbot.api import logger
            logger.warning(f"[openclaw_caller] TaskLog.list_recent 失败: {e}")
            return []

    def delete(self, task_id: str) -> bool:
        try:
            with self._connect_with_init() as conn:
                cur = conn.execute(
                    "DELETE FROM openclaw_tasks WHERE task_id=?",
                    (task_id,),
                )
                conn.commit()
                return cur.rowcount > 0
        except Exception as e:
            from astrbot.api import logger
            logger.warning(f"[openclaw_caller] TaskLog.delete 失败: {e}")
            return False
