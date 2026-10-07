"""OmniVideo 用户系统：SQLite 存储用户与会话。

- users:   用户名 + PBKDF2 密码哈希（随机盐，不存明文）
- sessions:登录会话（随机 token，30 天过期），存入浏览器 Cookie
- 内置管理员：启动时由 OMNI_USER/OMNI_PASS 预置，注册不可占用同名
"""

from __future__ import annotations
import hashlib
import hmac
import os
import secrets
import sqlite3
import threading
import time

DB_PATH = os.environ.get("OMNI_DB", os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "omnivideo.db"))

SESSION_DAYS = 30
PBKDF2_ITER = 200_000

_lock = threading.Lock()


def _conn():
    c = sqlite3.connect(DB_PATH, timeout=10)
    c.row_factory = sqlite3.Row
    return c


def init_db():
    with _lock, _conn() as c:
        c.execute("PRAGMA journal_mode=WAL")
        c.execute("""
            CREATE TABLE IF NOT EXISTS users(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                username TEXT NOT NULL UNIQUE,
                pass_hash TEXT NOT NULL,
                salt TEXT NOT NULL,
                is_admin INTEGER NOT NULL DEFAULT 0,
                created_at INTEGER NOT NULL
            )""")
        # 审核状态迁移：status（pending=待审核 / approved=已通过 / rejected=已拒绝）；管理员恒为 approved
        try:
            c.execute("ALTER TABLE users ADD COLUMN status TEXT NOT NULL DEFAULT 'pending'")
        except sqlite3.OperationalError:
            pass  # 列已存在
        c.execute("UPDATE users SET status='approved' WHERE is_admin=1")
        c.execute("""
            CREATE TABLE IF NOT EXISTS sessions(
                token TEXT PRIMARY KEY,
                user_id INTEGER NOT NULL,
                created_at INTEGER NOT NULL,
                expires_at INTEGER NOT NULL
            )""")
        c.execute("CREATE INDEX IF NOT EXISTS idx_sessions_expires ON sessions(expires_at)")
        c.execute("""
            CREATE TABLE IF NOT EXISTS tasks(
                id TEXT PRIMARY KEY,
                user_id INTEGER NOT NULL,
                url TEXT NOT NULL,
                platform TEXT,
                quality TEXT DEFAULT 'compat',
                title TEXT,
                status TEXT NOT NULL,
                progress REAL DEFAULT 0,
                message TEXT DEFAULT '',
                size REAL,
                file_path TEXT,
                file_name TEXT,
                error TEXT,
                height INTEGER,
                created_at INTEGER NOT NULL,
                finished_at INTEGER
            )""")
        c.execute("CREATE INDEX IF NOT EXISTS idx_tasks_user ON tasks(user_id, created_at DESC)")


def _hash_password(password: str, salt: str | None = None) -> tuple[str, str]:
    salt = salt or secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"),
                                 salt.encode("utf-8"), PBKDF2_ITER)
    return digest.hex(), salt


def _verify_password(password: str, salt: str, expected: str) -> bool:
    digest, _ = _hash_password(password, salt)
    return hmac.compare_digest(digest, expected)


def create_user(username: str, password: str, is_admin: bool = False) -> tuple[bool, str]:
    """注册新用户。返回 (成功?, 错误信息或空)。"""
    username = username.strip()
    if not (3 <= len(username) <= 32):
        return False, "用户名长度需在 3-32 个字符之间"
    if not username.replace("_", "").replace("-", "").isalnum():
        return False, "用户名只能包含字母、数字、下划线和短横线"
    if len(password) < 6:
        return False, "密码至少需要 6 位"
    digest, salt = _hash_password(password)
    with _lock, _conn() as c:
        try:
            c.execute(
                "INSERT INTO users(username, pass_hash, salt, is_admin, created_at) VALUES(?,?,?,?,?)",
                (username, digest, salt, 1 if is_admin else 0, int(time.time())))
            return True, ""
        except sqlite3.IntegrityError:
            return False, "该用户名已被注册，换个试试"


def get_user_by_name(username: str) -> sqlite3.Row | None:
    with _lock, _conn() as c:
        return c.execute("SELECT * FROM users WHERE username=?", (username,)).fetchone()


def get_user_by_id(uid: int) -> sqlite3.Row | None:
    with _lock, _conn() as c:
        return c.execute(
            "SELECT id, username, is_admin, status FROM users WHERE id=?", (uid,)).fetchone()


# ---------- 审核制 ----------

def is_user_approved(uid: int) -> bool:
    """用户是否可用：管理员恒可用；普通用户需 status='approved'。"""
    row = get_user_by_id(uid)
    if not row:
        return False
    if row["is_admin"]:
        return True
    return row["status"] == "approved"


def list_pending_users() -> list[dict]:
    """待审核用户（非管理员、待审核）。"""
    with _lock, _conn() as c:
        rows = c.execute(
            "SELECT id, username, created_at FROM users "
            "WHERE is_admin=0 AND status='pending' ORDER BY created_at ASC"
        ).fetchall()
        return [dict(r) for r in rows]


def list_all_users() -> list[dict]:
    """全部用户（管理用）。"""
    with _lock, _conn() as c:
        rows = c.execute(
            "SELECT id, username, is_admin, status, created_at FROM users ORDER BY is_admin DESC, created_at DESC"
        ).fetchall()
        return [dict(r) for r in rows]


def set_user_status(uid: int, status: str) -> bool:
    """设置普通用户状态（approved / pending / rejected）。返回是否命中行。"""
    if status not in ("approved", "pending", "rejected"):
        return False
    with _lock, _conn() as c:
        cur = c.execute(
            "UPDATE users SET status=? WHERE id=? AND is_admin=0", (status, uid))
        return cur.rowcount > 0


def count_pending() -> int:
    with _lock, _conn() as c:
        return c.execute(
            "SELECT COUNT(*) n FROM users WHERE is_admin=0 AND status='pending'").fetchone()["n"]


def check_login(username: str, password: str) -> tuple[bool, str, int | None]:
    """校验账号密码。返回 (成功?, 信息, user_id)。"""
    row = get_user_by_name(username.strip())
    if not row:
        return False, "用户名或密码错误", None
    if not _verify_password(password, row["salt"], row["pass_hash"]):
        return False, "用户名或密码错误", None
    return True, "", row["id"]


def create_session(user_id: int) -> str:
    token = secrets.token_urlsafe(32)
    now = int(time.time())
    with _lock, _conn() as c:
        c.execute("DELETE FROM sessions WHERE expires_at < ?", (now,))
        c.execute(
            "INSERT INTO sessions(token, user_id, created_at, expires_at) VALUES(?,?,?,?)",
            (token, user_id, now, now + SESSION_DAYS * 86400))
    return token


def check_session(token: str) -> int | None:
    """校验会话 token，返回 user_id；无效返回 None。"""
    if not token:
        return None
    with _lock, _conn() as c:
        row = c.execute(
            "SELECT user_id, expires_at FROM sessions WHERE token=?", (token,)).fetchone()
        if not row:
            return None
        if row["expires_at"] < int(time.time()):
            c.execute("DELETE FROM sessions WHERE token=?", (token,))
            return None
        return row["user_id"]


def delete_session(token: str):
    with _lock, _conn() as c:
        c.execute("DELETE FROM sessions WHERE token=?", (token,))


def list_user_count() -> int:
    with _lock, _conn() as c:
        return c.execute("SELECT COUNT(*) n FROM users").fetchone()["n"]


def ensure_admin(username: str, password: str):
    """预置内置管理员（OMNI_USER/OMNI_PASS）。"""
    username = username.strip()
    row = get_user_by_name(username)
    if row:
        return
    digest, salt = _hash_password(password)
    with _lock, _conn() as c:
        try:
            c.execute(
                "INSERT INTO users(username, pass_hash, salt, is_admin, status, created_at) VALUES(?,?,?,?, 'approved', ?)",
                (username, digest, salt, 1, int(time.time())))
        except sqlite3.IntegrityError:
            pass


# ---------- 任务表（下载记录） ----------

def task_create(t: dict):
    with _lock, _conn() as c:
        c.execute(
            "INSERT INTO tasks(id,user_id,url,platform,quality,title,status,progress,message,"
            "size,file_path,file_name,error,height,created_at,finished_at) "
            "VALUES(:id,:user_id,:url,:platform,:quality,:title,:status,:progress,:message,"
            ":size,:file_path,:file_name,:error,:height,:created_at,:finished_at)",
            {k: t.get(k) for k in (
                "id","user_id","url","platform","quality","title","status","progress","message",
                "size","file_path","file_name","error","height","created_at","finished_at")})


def task_get(tid: str, user_id: int | None = None) -> dict | None:
    with _lock, _conn() as c:
        if user_id is None:
            row = c.execute("SELECT * FROM tasks WHERE id=?", (tid,)).fetchone()
        else:
            row = c.execute("SELECT * FROM tasks WHERE id=? AND user_id=?", (tid, user_id)).fetchone()
        return dict(row) if row else None


def task_list(user_id: int | None = None, page: int = 1, page_size: int = 20) -> tuple[list[dict], int]:
    """按用户分页查询，返回 (rows, total)。user_id=None 表示全部。"""
    page = max(page, 1)
    page_size = min(max(page_size, 1), 100)
    offset = (page - 1) * page_size
    with _lock, _conn() as c:
        if user_id is None:
            total = c.execute("SELECT COUNT(*) n FROM tasks").fetchone()["n"]
            rows = c.execute(
                "SELECT * FROM tasks ORDER BY created_at DESC LIMIT ? OFFSET ?",
                (page_size, offset)).fetchall()
        else:
            total = c.execute("SELECT COUNT(*) n FROM tasks WHERE user_id=?", (user_id,)).fetchone()["n"]
            rows = c.execute(
                "SELECT * FROM tasks WHERE user_id=? ORDER BY created_at DESC LIMIT ? OFFSET ?",
                (user_id, page_size, offset)).fetchall()
        return [dict(r) for r in rows], total


def task_update(tid: str, **kw):
    if not kw:
        return
    fields = {k: v for k, v in kw.items() if k in (
        "url","platform","quality","title","status","progress","message",
        "size","file_path","file_name","error","height","finished_at")}
    if not fields:
        return
    cols = ", ".join(f"{k}=?" for k in fields)
    with _lock, _conn() as c:
        c.execute(f"UPDATE tasks SET {cols} WHERE id=?", (*fields.values(), tid))


def task_delete(tid: str, user_id: int | None = None) -> bool:
    with _lock, _conn() as c:
        if user_id is None:
            cur = c.execute("DELETE FROM tasks WHERE id=?", (tid,))
        else:
            cur = c.execute("DELETE FROM tasks WHERE id=? AND user_id=?", (tid, user_id))
        return cur.rowcount > 0


def task_mark_running_failed(reason: str = "服务重启，任务已中断"):
    """启动恢复：把未完成任务标记为失败。"""
    with _lock, _conn() as c:
        c.execute(
            "UPDATE tasks SET status='failed', progress=0, error=?, message='失败' "
            "WHERE status IN ('queued','parsing','downloading','merging')", (reason,))


def task_user_total(user_id: int) -> int:
    with _lock, _conn() as c:
        return c.execute("SELECT COUNT(*) n FROM tasks WHERE user_id=?", (user_id,)).fetchone()["n"]


def task_import(records: list[dict], user_id: int):
    """迁移旧 tasks.json 记录。"""
    for t in records:
        t = dict(t)
        t["id"] = str(t.get("id") or uuid4_hex())[:32]
        t["user_id"] = user_id
        if t.get("status") in ("queued", "parsing", "downloading", "merging"):
            t["status"] = "failed"
            t["error"] = "服务重启，任务已中断"
            t["progress"] = 0
        if t.get("status") == "done" and t.get("file_path") and not os.path.exists(t["file_path"]):
            t["status"] = "failed"
            t["error"] = "文件已被清理"
        task_create(t)


def task_clear_finished(user_id: int | None = None) -> int:
    """清理已完成或失败的历史任务。"""
    with _lock, _conn() as c:
        if user_id is None:
            cur = c.execute("DELETE FROM tasks WHERE status IN ('done', 'failed')")
        else:
            cur = c.execute("DELETE FROM tasks WHERE user_id=? AND status IN ('done', 'failed')", (user_id,))
        return cur.rowcount


def uuid4_hex():
    import uuid
    return uuid.uuid4().hex
