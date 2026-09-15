#!/usr/bin/env python3
"""待办事项追踪工具 —— Python 单文件后端。

设计目标：
1. 单文件可运行，依赖仅 Python3 标准库，拷贝即用。
2. 数据持久化使用 SQLite（data.db），单文件、零配置、并发安全。
3. 提供分级事项树 + 统计看板的 REST API，前端为单页应用。

启动：python3 app.py [端口号，默认 8000]
访问：浏览器打开 http://localhost:8000
"""

import json
import os
import re
import sqlite3
import sys
import threading
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta as _timedelta
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse

# 运行目录（web 资源相对此路径解析）
# 路径处理：区分源码运行与 PyInstaller 打包运行
# Why: PyInstaller --onefile 模式运行时把资源解压到临时目录 sys._MEIPASS，
# 但数据文件 data.db 必须写到 exe 旁边持久化，不能放临时目录（重启丢失）
if getattr(sys, "frozen", False):
    # 打包后运行：资源在临时解压目录，数据写到 exe 同级
    BASE_DIR = os.path.dirname(sys.executable)
    WEB_DIR = os.path.join(sys._MEIPASS, "web")
else:
    BASE_DIR = os.path.dirname(os.path.abspath(__file__))
    WEB_DIR = os.path.join(BASE_DIR, "web")
DATA_FILE = os.path.join(BASE_DIR, "data.db")

# 全局数据锁，保证并发写安全（SQLite 单连接跨线程共享，必须串行化访问）
_lock = threading.Lock()


# 表结构定义，建库与迁移脚本共用
SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS items (
    id          TEXT PRIMARY KEY,
    parent_id   TEXT,
    title       TEXT NOT NULL,
    owner       TEXT DEFAULT '',
    status      TEXT DEFAULT 'pending',
    priority    TEXT DEFAULT 'medium',
    progress    INTEGER DEFAULT 0,
    plan_end    TEXT,
    actual_end  TEXT,
    remark      TEXT DEFAULT '',
    tags        TEXT DEFAULT '[]',
    depends_on  TEXT DEFAULT '[]',
    sort_order  INTEGER DEFAULT 0,
    archived_at TEXT,
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL,
    FOREIGN KEY (parent_id) REFERENCES items(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_items_parent ON items(parent_id);
CREATE INDEX IF NOT EXISTS idx_items_status ON items(status);
CREATE INDEX IF NOT EXISTS idx_items_plan_end ON items(plan_end);

CREATE TABLE IF NOT EXISTS comments (
    id         TEXT PRIMARY KEY,
    item_id    TEXT NOT NULL,
    author     TEXT DEFAULT '匿名',
    text       TEXT NOT NULL,
    created_at TEXT NOT NULL,
    FOREIGN KEY (item_id) REFERENCES items(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_comments_item ON comments(item_id);
"""

# archived_at 索引在已有库升级后单独建（SCHEMA 里建索引对新库生效，
# 但旧库此时该列还没加会报错，所以分两步）
INDEX_ARCHIVED_SQL = "CREATE INDEX IF NOT EXISTS idx_items_archived ON items(archived_at)"


# ----------------------------------------------------------------------
# 数据存储层
# ----------------------------------------------------------------------
class TodoStore:
    """基于 SQLite 的事项存储。

    Why: 单机工具，无需服务型数据库；SQLite 单文件、零配置、并发安全，
    且自带外键级联，子项删除由数据库自动处理，无需在 Python 中递归。
    """

    # 允许在 update_item 中改动的字段（白名单，避免误改 id/created_at）
    _UPDATABLE = (
        "title", "owner", "status", "priority", "progress",
        "plan_end", "actual_end", "remark", "tags", "parent_id",
        "depends_on",
    )
    # tags / depends_on 以 JSON 字符串存 SQLite，列名列表
    _JSON_COLS = ("tags", "depends_on")

    def __init__(self, path: str):
        self.path = path
        # check_same_thread=False：HTTP Handler 多线程访问共享单连接；
        # 串行化由 _lock 保证，避免 "objects can only be used in that same thread"
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        # 必须显式开启外键约束，否则 ON DELETE CASCADE 不生效
        self._conn.execute("PRAGMA foreign_keys = ON")
        self._init_schema()

    def _init_schema(self):
        with self._tx() as conn:
            conn.executescript(SCHEMA_SQL)
            # 已有库升级：若 items 表缺 archived_at 列则补上
            # Why: SQLite 的 CREATE TABLE IF NOT EXISTS 不会给已存在的表加新列
            cols = {r["name"] for r in conn.execute("PRAGMA table_info(items)")}
            if "archived_at" not in cols:
                conn.execute("ALTER TABLE items ADD COLUMN archived_at TEXT")
            # 补 archived_at 索引（无论新老库都建一次，幂等）
            conn.execute(INDEX_ARCHIVED_SQL)
            # 自动归档元数据表：记录上次扫描时间，用于 24h 节流
            conn.execute(
                "CREATE TABLE IF NOT EXISTS meta ("
                "  key TEXT PRIMARY KEY,"
                "  value TEXT"
                ")"
            )

    def _meta_get(self, conn, key: str):
        r = conn.execute(
            "SELECT value FROM meta WHERE key=?", (key,)
        ).fetchone()
        return r["value"] if r else None

    def _meta_set(self, conn, key: str, value: str):
        conn.execute(
            "INSERT INTO meta(key, value) VALUES(?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, value)
        )

    @contextmanager
    def _tx(self):
        """事务上下文：保证写操作原子性 + 跨线程串行化。

        Why: SQLite 单连接被多线程共享时若并发写会抛 OperationalError；
        用全局 _lock 串行化 + 每次操作 commit/rollback，保证一致性。
        """
        with _lock:
            try:
                yield self._conn
                self._conn.commit()
            except Exception:
                self._conn.rollback()
                raise

    @staticmethod
    def _now() -> str:
        return datetime.now().isoformat(timespec="seconds")

    # ---------- 查询 ----------
    def list_items(self, archived: bool = False) -> list:
        """返回事项列表，按 sort_order 排序；附加 comments 子列表。

        archived:
          False（默认）= 只返回未归档事项（主视图用）
          True = 只返回已归档事项（归档视图用）
        Why: 主视图与归档视图数据隔离；前端依赖 comments 数组字段。
        """
        with self._tx() as conn:
            if archived:
                where = "WHERE archived_at IS NOT NULL"
            else:
                where = "WHERE archived_at IS NULL"
            rows = conn.execute(
                f"SELECT * FROM items {where} ORDER BY sort_order, created_at"
            ).fetchall()
            items = []
            for r in rows:
                it = dict(r)
                # 反序列化 JSON 字段
                for k in self._JSON_COLS:
                    it[k] = json.loads(it.get(k) or "[]")
                # 附加评论
                cs = conn.execute(
                    "SELECT * FROM comments WHERE item_id=? ORDER BY created_at",
                    (it["id"],)
                ).fetchall()
                it["comments"] = [dict(c) for c in cs]
                items.append(it)
            return items

    def get_item(self, item_id: str):
        with self._tx() as conn:
            r = conn.execute(
                "SELECT * FROM items WHERE id=?", (item_id,)
            ).fetchone()
            if r is None:
                return None
            it = dict(r)
            for k in self._JSON_COLS:
                it[k] = json.loads(it.get(k) or "[]")
            cs = conn.execute(
                "SELECT * FROM comments WHERE item_id=? ORDER BY created_at",
                (it["id"],)
            ).fetchall()
            it["comments"] = [dict(c) for c in cs]
            return it

    def _next_sort_order(self, parent_id) -> int:
        """取同层最大 sort_order + 1，新项追加到末尾。"""
        with self._tx() as conn:
            cur = conn.execute(
                "SELECT MAX(sort_order) AS m FROM items WHERE parent_id IS ?",
                (parent_id,)
            ).fetchone()
            m = cur["m"] if cur else None
            return 0 if m is None else int(m) + 1

    # ---------- 写操作 ----------
    def add_item(self, payload: dict) -> dict:
        now = self._now()
        item_id = payload.get("id") or str(uuid.uuid4())
        parent_id = payload.get("parent_id") or None
        record = {
            "id": item_id,
            "parent_id": parent_id,
            "title": payload.get("title", "").strip(),
            "owner": payload.get("owner", "").strip(),
            "status": payload.get("status", "pending"),
            "priority": payload.get("priority", "medium"),
            "progress": int(payload.get("progress", 0) or 0),
            "plan_end": payload.get("plan_end") or None,
            "actual_end": payload.get("actual_end") or None,
            "remark": payload.get("remark", ""),
            "tags": json.dumps(payload.get("tags", []), ensure_ascii=False),
            "depends_on": json.dumps(payload.get("depends_on", []), ensure_ascii=False),
            "sort_order": self._next_sort_order(parent_id),
            "created_at": now,
            "updated_at": now,
        }
        cols = ", ".join(record.keys())
        placeholders = ", ".join("?" for _ in record)
        with self._tx() as conn:
            conn.execute(
                f"INSERT INTO items ({cols}) VALUES ({placeholders})",
                tuple(record.values())
            )
        # 返回给前端的对象需含 comments + 反序列化的 tags/depends_on
        return self.get_item(item_id)

    def update_item(self, item_id: str, payload: dict):
        sets, vals = [], []
        for k in self._UPDATABLE:
            if k in payload:
                v = payload[k]
                # tags/depends_on 序列化为 JSON 字符串存储
                if k in self._JSON_COLS:
                    v = json.dumps(v, ensure_ascii=False)
                sets.append(f"{k} = ?")
                vals.append(v)
        sets.append("updated_at = ?")
        vals.append(self._now())
        # 标记完成时自动填实际完成时间（若未显式传 actual_end）
        if payload.get("status") == "done" and not payload.get("actual_end"):
            sets.append("actual_end = ?")
            vals.append(self._now())
        vals.append(item_id)
        with self._tx() as conn:
            cur = conn.execute(
                f"UPDATE items SET {', '.join(sets)} WHERE id = ?", vals
            )
            if cur.rowcount == 0:
                return None
        return self.get_item(item_id)

    def reorder(self, orders: list):
        """批量更新 sort_order。

        orders: [{"id": ..., "sort_order": int, "parent_id": ...}]

        做合法性校验：
        - id 存在
        - parent_id 要么为 None（根级），要么是存在的 id
        - 不能形成循环（节点不能移到自己的后代下）
        """
        if not orders:
            return
        with self._tx() as conn:
            # 取所有 id 集合用于存在性校验
            all_ids = {row["id"] for row in conn.execute("SELECT id FROM items")}
            now = self._now()
            for o in orders:
                item_id = o.get("id")
                if not item_id or item_id not in all_ids:
                    continue
                new_parent = o.get("parent_id", None)
                # parent_id 合法性校验
                if new_parent:
                    if new_parent not in all_ids:
                        continue
                    # 循环依赖检测：new_parent 不能是自身或后代
                    if new_parent == item_id or new_parent in self._descendants(conn, item_id):
                        continue
                else:
                    new_parent = None
                # 更新 parent_id（如有）和 sort_order
                if "parent_id" in o:
                    conn.execute(
                        "UPDATE items SET parent_id=?, updated_at=? WHERE id=?",
                        (new_parent, now, item_id)
                    )
                if "sort_order" in o:
                    conn.execute(
                        "UPDATE items SET sort_order=?, updated_at=? WHERE id=?",
                        (int(o["sort_order"]), now, item_id)
                    )

    def _descendants(self, conn, item_id) -> set:
        """递归获取某节点的所有后代 id（在已开启的事务内调用）。"""
        result = set()
        stack = [item_id]
        while stack:
            cur = stack.pop()
            rows = conn.execute(
                "SELECT id FROM items WHERE parent_id=? AND id != ?",
                (cur, item_id)
            ).fetchall()
            for r in rows:
                if r["id"] not in result:
                    result.add(r["id"])
                    stack.append(r["id"])
        return result

    # ---------- 评论 ----------
    def add_comment(self, item_id: str, author: str, text: str):
        with self._tx() as conn:
            # 校验 item 存在
            if conn.execute(
                "SELECT 1 FROM items WHERE id=?", (item_id,)
            ).fetchone() is None:
                return None
            cid = str(uuid.uuid4())
            now = self._now()
            conn.execute(
                """INSERT INTO comments (id, item_id, author, text, created_at)
                   VALUES (?, ?, ?, ?, ?)""",
                (cid, item_id,
                 (author or "").strip() or "匿名",
                 (text or "").strip(), now)
            )
            # 更新 item 的 updated_at，保持与原版语义一致
            conn.execute(
                "UPDATE items SET updated_at=? WHERE id=?", (now, item_id)
            )
            return {
                "id": cid,
                "item_id": item_id,
                "author": (author or "").strip() or "匿名",
                "text": (text or "").strip(),
                "created_at": now,
            }

    def delete_comment(self, item_id: str, comment_id: str) -> bool:
        with self._tx() as conn:
            cur = conn.execute(
                "DELETE FROM comments WHERE id=? AND item_id=?",
                (comment_id, item_id)
            )
            if cur.rowcount == 0:
                return False
            conn.execute(
                "UPDATE items SET updated_at=? WHERE id=?",
                (self._now(), item_id)
            )
            return True

    def delete_item(self, item_id: str) -> bool:
        """删除事项及其所有子项。

        Why: 外键 ON DELETE CASCADE 会自动级联删除子项和评论，
        无需在 Python 中递归收集。
        """
        with self._tx() as conn:
            cur = conn.execute("DELETE FROM items WHERE id=?", (item_id,))
            return cur.rowcount > 0

    # ---------- 归档 ----------
    def archive_item(self, item_id: str, cascade: bool = False):
        """手动归档单条事项。

        cascade=False（默认）：只归档当前项，不动子项
        cascade=True：连同所有未归档后代一起归档（自动归档用）
        Why: 手动归档是用户明确意图，只动当前项；自动归档要保证
        父项消失后子项不悬空，所以联动。
        """
        now = self._now()
        with self._tx() as conn:
            r = conn.execute(
                "SELECT 1 FROM items WHERE id=?", (item_id,)
            ).fetchone()
            if r is None:
                return None
            ids = [item_id]
            if cascade:
                ids += list(self._descendants(conn, item_id))
            placeholders = ", ".join("?" for _ in ids)
            conn.execute(
                f"UPDATE items SET archived_at=?, updated_at=? "
                f"WHERE id IN ({placeholders}) AND archived_at IS NULL",
                [now, now] + ids
            )
        # Why: 不能在 _tx 事务里调 get_item（它会再开一个 _tx 递归加锁死锁），
        # 事务结束后再单独查
        return self.get_item(item_id)

    def unarchive_item(self, item_id: str):
        """恢复归档事项到主视图（只恢复单条，不联动后代）。"""
        with self._tx() as conn:
            r = conn.execute(
                "SELECT 1 FROM items WHERE id=? AND archived_at IS NOT NULL",
                (item_id,)
            ).fetchone()
            if r is None:
                return None
            conn.execute(
                "UPDATE items SET archived_at=NULL, updated_at=? WHERE id=?",
                (self._now(), item_id)
            )
        return self.get_item(item_id)

    def _run_auto_archive(self, days: int = 90) -> int:
        """自动归档：N 天前 + status=done 的事项连带其子项归档。

        返回本次归档的事项数。带 24h 节流，避免每次请求都扫表。
        Why: 业界做法（Jira）后台定时任务，单机工具无 cron，
        借助启动 + meta 表节流实现"每天扫一次"。
        """
        with self._tx() as conn:
            last = self._meta_get(conn, "auto_archive_last_run")
            now_dt = datetime.now()
            if last:
                try:
                    last_dt = datetime.fromisoformat(last)
                    if (now_dt - last_dt).total_seconds() < 86400:
                        return 0
                except ValueError:
                    pass
            cutoff = (now_dt - _timedelta(days=days)).isoformat(timespec="seconds")
            rows = conn.execute(
                "SELECT id FROM items "
                "WHERE archived_at IS NULL AND status='done' "
                "AND (actual_end IS NOT NULL AND actual_end < ? "
                "     OR actual_end IS NULL AND updated_at < ?)",
                (cutoff[:10], cutoff[:10])
            ).fetchall()
            count = 0
            for r in rows:
                ids = [r["id"]] + list(self._descendants(conn, r["id"]))
                placeholders = ", ".join("?" for _ in ids)
                cur = conn.execute(
                    f"UPDATE items SET archived_at=?, updated_at=? "
                    f"WHERE id IN ({placeholders}) AND archived_at IS NULL",
                    [now_dt.isoformat(timespec="seconds"),
                     now_dt.isoformat(timespec="seconds")] + ids
                )
                count += cur.rowcount
            self._meta_set(conn, "auto_archive_last_run",
                           now_dt.isoformat(timespec="seconds"))
            return count

    def stats(self) -> dict:
        """按状态/优先级/进度汇总，供看板展示。"""
        with self._tx() as conn:
            total = conn.execute("SELECT COUNT(*) AS c FROM items").fetchone()["c"]
            by_status = {"pending": 0, "in_progress": 0, "done": 0, "blocked": 0}
            for r in conn.execute(
                "SELECT status, COUNT(*) AS c FROM items GROUP BY status"
            ):
                by_status[r["status"]] = r["c"]
            by_priority = {"low": 0, "medium": 0, "high": 0, "urgent": 0}
            for r in conn.execute(
                "SELECT priority, COUNT(*) AS c FROM items GROUP BY priority"
            ):
                by_priority[r["priority"]] = r["c"]
            done_count = by_status.get("done", 0)
            return {
                "total": total,
                "by_status": by_status,
                "by_priority": by_priority,
                "completion_rate": round(done_count / total * 100, 1) if total else 0.0,
            }

    def export_all(self) -> dict:
        """导出全量数据，供备份迁移。

        Why: 替代旧版直接访问 store._data；返回与旧 JSON 文件同构的对象。
        """
        return {"items": self.list_items()}


store = TodoStore(DATA_FILE)


# ----------------------------------------------------------------------
# HTTP 层
# ----------------------------------------------------------------------
class Handler(BaseHTTPRequestHandler):
    """路由分发：/api/* 走 JSON 接口，其他走静态文件。"""

    # 静态文件 MIME 表
    _MIME = {
        ".html": "text/html; charset=utf-8",
        ".css": "text/css; charset=utf-8",
        ".js": "application/javascript; charset=utf-8",
        ".svg": "image/svg+xml",
        ".png": "image/png",
        ".ico": "image/x-icon",
    }

    def log_message(self, fmt, *args):
        # 简化日志：仅打印方法 + 路径 + 状态
        sys.stderr.write("%s - %s\n" % (self.address_string(), fmt % args))

    # ---------- 通用工具 ----------
    def _send_json(self, obj, status=200):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET,POST,PUT,DELETE,OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()
        self.wfile.write(body)

    def _read_body(self) -> dict:
        length = int(self.headers.get("Content-Length", 0) or 0)
        if length == 0:
            return {}
        raw = self.rfile.read(length)
        try:
            return json.loads(raw.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError):
            return {}

    def _send_static(self, path: str):
        full = os.path.normpath(os.path.join(WEB_DIR, path.lstrip("/")))
        # 安全：禁止路径穿越 web 目录
        if not full.startswith(WEB_DIR) or not os.path.isfile(full):
            self.send_error(404, "Not Found")
            return
        ext = os.path.splitext(full)[1].lower()
        mime = self._MIME.get(ext, "application/octet-stream")
        with open(full, "rb") as f:
            body = f.read()
        self.send_response(200)
        self.send_header("Content-Type", mime)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    # ---------- HTTP 方法 ----------
    def do_OPTIONS(self):
        self._send_json({"ok": True})

    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path

        if path == "/" or path == "":
            return self._send_static("/index.html")

        if path.startswith("/api/"):
            return self._api_get(path)

        return self._send_static(path)

    def do_POST(self):
        parsed = urlparse(self.path)
        path = parsed.path
        if path == "/api/items":
            # 锁由 store._tx 内部统一管理，Handler 层不再额外加锁
            # Why: store 每个方法用 _tx 上下文加锁，外层再 with _lock 会
            # 递归获取不可重入锁导致死锁
            item = store.add_item(self._read_body())
            return self._send_json(item, 201)
        # 添加评论：/api/items/{id}/comments
        m = re.match(r"^/api/items/([^/]+)/comments$", path)
        if m:
            body = self._read_body()
            c = store.add_comment(m.group(1),
                                  body.get("author", ""),
                                  body.get("text", ""))
            if c is None:
                return self._send_json({"error": "not found"}, 404)
            return self._send_json(c, 201)
        # 归档事项：/api/items/{id}/archive
        m = re.match(r"^/api/items/([^/]+)/archive$", path)
        if m:
            item = store.archive_item(m.group(1))
            if item is None:
                return self._send_json({"error": "not found"}, 404)
            return self._send_json(item)
        # 恢复归档：/api/items/{id}/unarchive
        m = re.match(r"^/api/items/([^/]+)/unarchive$", path)
        if m:
            item = store.unarchive_item(m.group(1))
            if item is None:
                return self._send_json({"error": "not found or not archived"}, 404)
            return self._send_json(item)
        self.send_error(404, "Not Found")

    def do_PUT(self):
        path = urlparse(self.path).path
        m = re.match(r"^/api/items/([^/]+)$", path)
        if m:
            item = store.update_item(m.group(1), self._read_body())
            if item is None:
                return self._send_json({"error": "not found"}, 404)
            return self._send_json(item)
        if path == "/api/reorder":
            store.reorder(self._read_body().get("orders", []))
            return self._send_json({"ok": True})
        self.send_error(404, "Not Found")

    def do_DELETE(self):
        path = urlparse(self.path).path
        m = re.match(r"^/api/items/([^/]+)$", path)
        if m:
            ok = store.delete_item(m.group(1))
            return self._send_json({"deleted": ok})
        # 删除评论：/api/items/{id}/comments/{cid}
        m = re.match(r"^/api/items/([^/]+)/comments/([^/]+)$", path)
        if m:
            ok = store.delete_comment(m.group(1), m.group(2))
            return self._send_json({"deleted": ok})
        self.send_error(404, "Not Found")

    # ---------- API 路由 ----------
    def _api_get(self, path: str):
        if path == "/api/items":
            return self._send_json(store.list_items())
        if path == "/api/items/archived":
            return self._send_json(store.list_items(archived=True))
        if path == "/api/stats":
            return self._send_json(store.stats())
        if path == "/api/export":
            # 导出全量数据，便于备份迁移
            return self._send_json(store.export_all())
        m = re.match(r"^/api/items/([^/]+)$", path)
        if m:
            item = store.get_item(m.group(1))
            if item is None:
                return self._send_json({"error": "not found"}, 404)
            return self._send_json(item)
        self.send_error(404, "Not Found")


def main():
    port = 8000
    if len(sys.argv) > 1:
        try:
            port = int(sys.argv[1])
        except ValueError:
            sys.stderr.write("端口号必须为整数\n")
            sys.exit(1)

    # 启动时扫一次自动归档（24h 节流内会自动跳过）
    # Why: 单机工具无后台 cron，启动时跑保证长期运行也能定期归档
    archived_n = store._run_auto_archive()
    if archived_n:
        print(f"启动自动归档：{archived_n} 条事项已归档")

    server = HTTPServer(("0.0.0.0", port), Handler)
    print(f"待办事项服务已启动：http://localhost:{port}")
    print(f"数据文件：{DATA_FILE}")
    print(f"Web 资源：{WEB_DIR}")
    print("按 Ctrl+C 退出")
    # 打包成 exe 运行时自动打开默认浏览器，免去手动输地址
    # Why: 非开发人员双击 exe 后不清楚要访问哪个 URL
    if getattr(sys, "frozen", False):
        import threading as _t
        import webbrowser as _wb

        def _open():
            import time
            time.sleep(1.5)  # 等服务就绪
            _wb.open(f"http://localhost:{port}")

        _t.Thread(target=_open, daemon=True).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n服务已停止")
        server.server_close()


if __name__ == "__main__":
    main()
