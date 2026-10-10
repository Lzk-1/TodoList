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
import html
import re
import smtplib
import sqlite3
import sys
import threading
import time
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta as _timedelta
from email.header import Header
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
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

CREATE TABLE IF NOT EXISTS mail_log (
    item_id TEXT NOT NULL,
    day     TEXT NOT NULL,
    sent_at TEXT NOT NULL,
    PRIMARY KEY (item_id, day)
);

CREATE TABLE IF NOT EXISTS mail_errors (
    item_id    TEXT NOT NULL,
    day        TEXT NOT NULL,
    item_title TEXT DEFAULT '',
    to_addr    TEXT DEFAULT '',
    subject    TEXT DEFAULT '',
    error      TEXT DEFAULT '',
    created_at TEXT NOT NULL,
    read_at    TEXT,
    PRIMARY KEY (item_id, day)
);
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
        "depends_on", "remind_email",
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
            # 邮件提醒邮箱（可空）：单个事项可覆盖全局默认收件邮箱
            if "remind_email" not in cols:
                conn.execute("ALTER TABLE items ADD COLUMN remind_email TEXT DEFAULT ''")
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
            "remind_email": payload.get("remind_email") or "",
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
            by_status = {"pending": 0, "in_progress": 0, "done": 0, "blocked": 0,
                         "on_hold": 0}
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
        含未归档 + 已归档事项，确保导出是完整快照。
        """
        with self._tx() as conn:
            rows = conn.execute(
                "SELECT * FROM items ORDER BY sort_order, created_at"
            ).fetchall()
            items = []
            for r in rows:
                it = dict(r)
                for k in self._JSON_COLS:
                    it[k] = json.loads(it.get(k) or "[]")
                cs = conn.execute(
                    "SELECT * FROM comments WHERE item_id=? ORDER BY created_at",
                    (it["id"],)
                ).fetchall()
                it["comments"] = [dict(c) for c in cs]
                items.append(it)
        return {"items": items}

    def import_data(self, data: dict) -> dict:
        """导入全量数据，与 export_all 输出格式兼容。

        策略：
          - 按 parent_id 拓扑顺序导入（父先于子），避免外键冲突
          - ID 已存在则跳过，不覆盖本地数据（保守策略，防误覆盖）
          - comments 一起导入，item_id 冲突同样跳过
          - 整体在一个事务内，任何异常全量回滚
        """
        raw_items = data.get("items", [])
        if not raw_items:
            return {"imported": 0, "skipped": 0, "comments": 0}

        # 拓扑排序：parent_id 为空或指向不在导入集合的项 → 第一批
        id_set = {it.get("id") for it in raw_items if it.get("id")}
        ordered = []
        placed = set()

        def _place_ready():
            """把所有父项已就位（或父不在本次导入集合）的项放入 ordered。"""
            for it in raw_items:
                iid = it.get("id")
                if not iid or iid in placed:
                    continue
                pid = it.get("parent_id")
                # 父为空 或 父不在本次导入集合（外部数据）→ 可放
                if not pid or pid not in id_set or pid in placed:
                    placed.add(iid)
                    ordered.append(it)

        # 简单循环到收敛（N 轮足够）
        for _ in range(len(raw_items) + 1):
            before = len(placed)
            _place_ready()
            if len(placed) >= len(raw_items):
                break
            if len(placed) == before:
                break  # 有环，剩下的跳过

        imported = 0
        skipped = 0
        comments_n = 0

        with self._tx() as conn:
            existing_ids = {r["id"] for r in conn.execute("SELECT id FROM items")}
            existing_cids = {r["id"] for r in conn.execute("SELECT id FROM comments")}

            for it in ordered:
                iid = it.get("id")
                if not iid or iid in existing_ids:
                    skipped += 1
                    continue
                now = it.get("updated_at") or self._now()
                record = {
                    "id": iid,
                    "parent_id": it.get("parent_id") or None,
                    "title": (it.get("title") or "").strip() or "(未命名)",
                    "owner": it.get("owner", ""),
                    "status": it.get("status", "pending"),
                    "priority": it.get("priority", "medium"),
                    "progress": int(it.get("progress", 0) or 0),
                    "plan_end": it.get("plan_end") or None,
                    "actual_end": it.get("actual_end") or None,
                    "remark": it.get("remark", ""),
                    "remind_email": it.get("remind_email") or "",
                    "tags": json.dumps(it.get("tags", []), ensure_ascii=False),
                    "depends_on": json.dumps(it.get("depends_on", []), ensure_ascii=False),
                    "sort_order": int(it.get("sort_order", 0) or 0),
                    "archived_at": it.get("archived_at") or None,
                    "created_at": it.get("created_at") or now,
                    "updated_at": now,
                }
                cols = ", ".join(record.keys())
                placeholders = ", ".join("?" for _ in record)
                conn.execute(
                    f"INSERT INTO items ({cols}) VALUES ({placeholders})",
                    list(record.values())
                )
                existing_ids.add(iid)
                imported += 1

                # 导入评论
                for c in it.get("comments") or []:
                    cid = c.get("id")
                    if not cid or cid in existing_cids:
                        continue
                    conn.execute(
                        "INSERT INTO comments (id, item_id, author, text, created_at) "
                        "VALUES (?,?,?,?,?)",
                        (
                            cid,
                            iid,
                            c.get("author", ""),
                            c.get("text", ""),
                            c.get("created_at") or self._now(),
                        )
                    )
                    existing_cids.add(cid)
                    comments_n += 1

        return {"imported": imported, "skipped": skipped, "comments": comments_n}

    # ---------- 邮件提醒 ----------
    MAIL_KEYS = ("smtp_host", "smtp_port", "smtp_user", "mail_from", "mail_to",
                 "remind_time")

    def mail_settings_get(self) -> dict:
        """读取邮件设置（不含授权码，附 has_password 标记，供前端展示）。"""
        with self._tx() as conn:
            s = {k: (self._meta_get(conn, k) or "") for k in self.MAIL_KEYS}
            s["smtp_port"] = int(s["smtp_port"]) if s["smtp_port"] else 465
            s["reminder_enabled"] = (self._meta_get(conn, "reminder_enabled") or "0") == "1"
            s["has_password"] = bool(self._meta_get(conn, "smtp_pass"))
            if not s.get("remind_time"):
                s["remind_time"] = "09:00"  # 默认每天 9 点发送，避免凌晨提醒
            return s

    def mail_settings_raw(self) -> dict:
        """读取含授权码的完整设置（仅供后台提醒线程/测试发送内部使用）。"""
        with self._tx() as conn:
            s = {k: (self._meta_get(conn, k) or "") for k in self.MAIL_KEYS}
            s["smtp_pass"] = self._meta_get(conn, "smtp_pass") or ""
            s["reminder_enabled"] = (self._meta_get(conn, "reminder_enabled") or "0") == "1"
            if not s.get("remind_time"):
                s["remind_time"] = "09:00"
            return s

    def mail_settings_save(self, payload: dict):
        with self._tx() as conn:
            for k in self.MAIL_KEYS + ("smtp_pass",):
                if k not in payload or payload[k] is None:
                    continue
                v = str(payload[k]).strip()
                if k == "smtp_pass" and not v:
                    continue  # 留空 = 不修改已保存的授权码
                self._meta_set(conn, k, v)
            if "reminder_enabled" in payload:
                self._meta_set(conn, "reminder_enabled",
                               "1" if payload["reminder_enabled"] else "0")

    def mail_log_has(self, item_id: str, day: str) -> bool:
        """该事项在某提醒日是否已发过邮件（避免重复发送）。"""
        with self._tx() as conn:
            r = conn.execute(
                "SELECT 1 FROM mail_log WHERE item_id=? AND day=?",
                (item_id, day)
            ).fetchone()
            return r is not None

    def mail_log_add(self, item_id: str, day: str):
        with self._tx() as conn:
            conn.execute(
                "INSERT OR IGNORE INTO mail_log (item_id, day, sent_at) "
                "VALUES (?,?,?)", (item_id, day, self._now())
            )

    def mail_error_record(self, item_id: str, day: str, item_title: str,
                          to_addr: str, subject: str, error: str):
        """记录一条发送失败，供前端轮询展示；重复失败覆盖并重置为未读。"""
        with self._tx() as conn:
            conn.execute(
                "INSERT INTO mail_errors (item_id, day, item_title, to_addr, "
                "subject, error, created_at) VALUES (?,?,?,?,?,?,?) "
                "ON CONFLICT(item_id, day) DO UPDATE SET "
                "item_title=excluded.item_title, to_addr=excluded.to_addr, "
                "subject=excluded.subject, error=excluded.error, "
                "created_at=excluded.created_at, read_at=NULL",
                (item_id, day, item_title, to_addr, subject, error, self._now())
            )

    def mail_error_clear(self, item_id: str, day: str):
        """发送成功后清除该事项的失败记录。"""
        with self._tx() as conn:
            conn.execute(
                "DELETE FROM mail_errors WHERE item_id=? AND day=?",
                (item_id, day)
            )

    def mail_errors_unread(self) -> list:
        """未读失败列表（前端轮询用），按时间正序。"""
        with self._tx() as conn:
            rows = conn.execute(
                "SELECT item_id, day, item_title, to_addr, subject, error, "
                "created_at FROM mail_errors WHERE read_at IS NULL "
                "ORDER BY created_at"
            ).fetchall()
            return [dict(r) for r in rows]

    def mail_errors_mark_read(self, keys: list):
        """keys: [{"item_id":..., "day":...}]，把对应的失败标记为已读。"""
        if not keys:
            return
        now = self._now()
        with self._tx() as conn:
            for k in keys:
                if not k or not k.get("item_id") or not k.get("day"):
                    continue
                conn.execute(
                    "UPDATE mail_errors SET read_at=? "
                    "WHERE item_id=? AND day=? AND read_at IS NULL",
                    (now, k["item_id"], k["day"])
                )

    def due_items(self, days: list) -> list:
        """返回计划完成日属于给定日期集合、未完成/未挂起/未归档的事项。

        前端 plan_end 存的是 YYYY-MM-DD，但历史导入数据可能带时间，
        用 substr 取前 10 位统一比较。
        """
        with self._tx() as conn:
            placeholders = ", ".join("?" for _ in days)
            rows = conn.execute(
                f"SELECT * FROM items WHERE archived_at IS NULL "
                f"AND status NOT IN ('done','on_hold') "
                f"AND plan_end IS NOT NULL "
                f"AND substr(plan_end,1,10) IN ({placeholders})",
                days
            ).fetchall()
            items = []
            for r in rows:
                it = dict(r)
                for k in self._JSON_COLS:
                    it[k] = json.loads(it.get(k) or "[]")
                items.append(it)
            return items


store = TodoStore(DATA_FILE)


# ----------------------------------------------------------------------
# 邮件提醒
# ----------------------------------------------------------------------
class MailReminder:
    """邮件提醒：后台线程定期扫描到期事项并发送提醒邮件。

    提醒时机：计划完成日前一天（"明天到期"）+ 计划完成日当天（"今天到期"），
    各发一次。Why: 单机工具无 cron，参照自动归档模式——启动即扫一次 +
    后台线程每 30 分钟扫一次；mail_log 记录 (item_id, 提醒日期) 防止重复发送。
    发送仅用标准库 smtplib，不引入第三方依赖。
    """

    SCAN_INTERVAL = 1800  # 秒（30 分钟）

    _STATUS_CN = {"pending": "待开始", "in_progress": "进行中", "done": "已完成",
                  "blocked": "阻塞", "on_hold": "挂起"}
    _PRIORITY_CN = {"low": "低", "medium": "中", "high": "高", "urgent": "紧急"}

    # 邮件 HTML 里的配色（与界面风格一致：紧急红、高橙、中蓝、低灰）
    _PRIORITY_COLOR = {"low": "#64748b", "medium": "#2563eb",
                       "high": "#f97316", "urgent": "#dc2626"}
    _STATUS_COLOR = {"pending": "#64748b", "in_progress": "#2563eb",
                     "done": "#16a34a", "blocked": "#dc2626", "on_hold": "#f59e0b"}

    def __init__(self, store):
        self.store = store

    def scan(self) -> int:
        """扫描一次，发送应发的提醒邮件。返回本次发送数。

        同一收件人的多个到期事项合并成一封邮件发送（今天/明天各一段），
        避免每个事项单独发一封造成邮件轰炸。
        """
        s = self.store.mail_settings_raw()
        if not s.get("reminder_enabled"):
            return 0
        if not (s.get("smtp_host") and s.get("smtp_user") and s.get("smtp_pass")):
            return 0
        # 提醒发送时间闸门：未到配置时间（默认 09:00）不发送
        # Why: 日期在零点切换，若不设闸门，凌晨 0:00-0:30 的扫描就会把
        # "今天/明天到期"的邮件发出去，打扰休息；到点后由本轮扫描统一补发
        try:
            hh, mm = (s.get("remind_time") or "09:00").split(":")
            gate = int(hh) * 60 + int(mm)
        except (ValueError, TypeError):
            gate = 9 * 60
        now = datetime.now()
        if now.hour * 60 + now.minute < gate:
            return 0
        today = now.strftime("%Y-%m-%d")
        tomorrow = (now + _timedelta(days=1)).strftime("%Y-%m-%d")
        # 收件人：事项自身提醒邮箱优先，否则用全局默认收件邮箱
        default_to = (s.get("mail_to") or "").strip()
        groups = {}  # 收件人 -> [(day_key, label, item)]
        for it in self.store.due_items([today, tomorrow]):
            due = (it.get("plan_end") or "")[:10]
            if due == today:
                day_key, label = today, "今天到期"
            elif due == tomorrow:
                day_key, label = tomorrow, "明天到期"
            else:
                continue
            if self.store.mail_log_has(it["id"], day_key):
                continue
            # 收件人：事项自身提醒邮箱优先，否则用全局默认收件邮箱；
            # 多个邮箱用逗号/分号分隔，按归一化后的收件人集合分组合并发送
            raw_to = (it.get("remind_email") or "").strip() or default_to
            key = ",".join(self._split_recipients(raw_to))
            if not key:
                continue
            groups.setdefault(key, []).append((day_key, label, it, raw_to))
        if not groups:
            return 0
        sent = 0
        for key, entries in groups.items():
            sections = {}
            to = entries[0][3]  # 原始收件人字符串（发送时再拆分成地址列表）
            for day_key, label, it, _raw in entries:
                sections.setdefault(label, []).append(it)
            # 主题里体现数量，如：【待办提醒】今天到期 2 个事项、明天到期 1 个事项
            parts = [f"{label} {len(sections[label])} 个事项"
                     for label in ("今天到期", "明天到期") if label in sections]
            subject = "【待办提醒】" + "、".join(parts)
            try:
                self._send(
                    s.get("smtp_host"), int(s.get("smtp_port") or 465),
                    s.get("smtp_user"), s.get("smtp_pass"),
                    (s.get("mail_from") or "").strip() or s.get("smtp_user"),
                    to, subject, self._body_combined(sections),
                    self._body_html(sections)
                )
            except Exception as e:
                sys.stderr.write(f"邮件发送失败 [{to}]：{e}\n")
                # 整封合并邮件失败：为每个涉及事项写失败记录，供前端轮询弹提示
                for day_key, label, it, _raw in entries:
                    self.store.mail_error_record(
                        it["id"], day_key, it.get("title") or "",
                        to, subject, str(e)
                    )
                continue
            for day_key, label, it, _raw in entries:
                self.store.mail_log_add(it["id"], day_key)
                self.store.mail_error_clear(it["id"], day_key)
            sent += 1
        return sent

    @staticmethod
    def _split_recipients(raw: str) -> list:
        """把逗号/分号（中英文）分隔的收件人字符串拆成地址列表，去空去重。

        Why: 支持"一个收件人字段填多个邮箱"，SMTP 的 RCPT 本来就支持多收件人。
        """
        out, seen = [], set()
        for part in re.split(r"[,;，；]", raw or ""):
            addr = part.strip()
            if addr and addr not in seen:
                seen.add(addr)
                out.append(addr)
        return out

    def _send(self, host, port, user, pwd, mail_from, to, subject, body,
              body_html=None):
        """经 SMTP 发送一封邮件。465 端口用 SSL，其余端口用 STARTTLS。

        收件人支持多个邮箱（逗号/分号分隔，去重）。
        有 HTML 版本时发送 multipart/alternative（纯文本 + HTML），
        让不支持 HTML 的客户端也能正常阅读。
        """
        rcpts = self._split_recipients(to)
        if not rcpts:
            raise ValueError("收件邮箱为空")
        if body_html:
            msg = MIMEMultipart("alternative")
            msg.attach(MIMEText(body, "plain", "utf-8"))
            msg.attach(MIMEText(body_html, "html", "utf-8"))
        else:
            msg = MIMEText(body, "plain", "utf-8")
        msg["Subject"] = Header(subject, "utf-8")
        msg["From"] = mail_from or user
        msg["To"] = ", ".join(rcpts)
        if int(port) == 465:
            server = smtplib.SMTP_SSL(host, int(port), timeout=20)
        else:
            server = smtplib.SMTP(host, int(port), timeout=20)
            server.starttls()
        server.login(user, pwd)
        server.sendmail(mail_from, rcpts, msg.as_string())
        server.quit()

    def _subject(self, it: dict, label: str) -> str:
        return f"【待办提醒】{label}：{it.get('title')}"

    def _body(self, it: dict) -> str:
        lines = [
            f"事项：{it.get('title')}",
            f"计划完成时间：{(it.get('plan_end') or '')[:10]}",
        ]
        if it.get("owner"):
            lines.append(f"负责人：{it['owner']}")
        lines.append(f"优先级：{self._PRIORITY_CN.get(it.get('priority'), it.get('priority'))}")
        lines.append(f"状态：{self._STATUS_CN.get(it.get('status'), it.get('status'))}")
        lines.append(f"进度：{it.get('progress', 0)}%")
        tags = it.get("tags") or []
        if tags:
            lines.append("标签：" + ", ".join(tags))
        if it.get("remark"):
            lines.append(f"备注：{it['remark']}")
        return "\n".join(lines)

    def _body_combined(self, sections: dict) -> str:
        """合并邮件正文：今天到期 / 明天到期各一段，段内逐条列出事项。"""
        parts = []
        for label in ("今天到期", "明天到期"):
            its = sections.get(label)
            if not its:
                continue
            parts.append(f"{label}（{len(its)} 个）：")
            for it in its:
                parts.append("-" * 20)
                parts.append(self._body(it))
        return "\n".join(parts)

    def _body_html(self, sections: dict) -> str:
        """合并邮件的 HTML 正文：渐变头图 + 事项卡片，样式全部内联以兼容各客户端。"""
        now = datetime.now()
        date_str = f"{now.year}年{now.month}月{now.day}日"
        total = sum(len(v) for v in sections.values())
        secs = []
        for label, color in (("今天到期", "#dc2626"), ("明天到期", "#f97316")):
            its = sections.get(label)
            if not its:
                continue
            cards = "".join(self._item_card_html(it) for it in its)
            secs.append(
                f'<div style="margin-bottom:20px;">'
                f'<div style="font-size:15px;font-weight:700;color:{color};margin-bottom:12px;">'
                f'{label}（{len(its)} 个）</div>{cards}</div>'
            )
        return (
            '<!DOCTYPE html><html><head><meta charset="utf-8"></head>'
            '<body style="margin:0;padding:0;background:#f4f6f8;font-family:Segoe UI,Microsoft YaHei,Arial,sans-serif;color:#333;">'
            '<div style="max-width:640px;margin:0 auto;padding:24px 16px;">'
            '<div style="background:#ffffff;border-radius:12px;overflow:hidden;box-shadow:0 2px 10px rgba(0,0,0,0.06);">'
            '<div style="background:linear-gradient(135deg,#2563eb,#4f46e5);padding:18px 24px;">'
            '<div style="font-size:18px;font-weight:600;color:#ffffff;">待办事项到期提醒</div>'
            f'<div style="font-size:12px;color:#dbe4ff;margin-top:4px;">{date_str} · 共 {total} 个事项</div>'
            '</div>'
            f'<div style="padding:20px 24px;">{"".join(secs)}</div>'
            '<div style="padding:14px 24px;background:#f8fafc;border-top:1px solid #e5e7eb;font-size:12px;color:#94a3b8;">'
            '由「待办事项追踪」自动发送，此邮件无需回复</div>'
            '</div></div></body></html>'
        )

    def _item_card_html(self, it: dict) -> str:
        """单个事项的 HTML 卡片：左侧优先级色条 + 信息表格。"""
        e = html.escape
        title = e(it.get("title") or "")
        pcol = self._PRIORITY_COLOR.get(it.get("priority"), "#64748b")
        pri = e(self._PRIORITY_CN.get(it.get("priority"), it.get("priority") or ""))
        scol = self._STATUS_COLOR.get(it.get("status"), "#64748b")
        st = e(self._STATUS_CN.get(it.get("status"), it.get("status") or ""))
        rows = [self._row_html("计划完成", e((it.get("plan_end") or "")[:10]))]
        if it.get("owner"):
            rows.append(self._row_html("负责人", e(it["owner"])))
        rows.append(self._row_html("状态", f'<span style="color:{scol};font-weight:600;">{st}</span>'))
        rows.append(self._row_html("进度", self._progress_html(int(it.get("progress") or 0))))
        tags = it.get("tags") or []
        if tags:
            chips = "".join(
                f'<span style="display:inline-block;background:#eef2ff;color:#4f46e5;'
                f'border-radius:10px;padding:1px 8px;font-size:12px;margin:2px 4px 2px 0;">{e(t)}</span>'
                for t in tags)
            rows.append(self._row_html("标签", chips))
        if it.get("remark"):
            rows.append(self._row_html("备注", f'<span style="color:#666;">{e(it["remark"])}</span>'))
        return (
            '<div style="border:1px solid #e5e7eb;border-left:4px solid '
            f'{pcol};border-radius:8px;padding:12px 14px;margin-bottom:12px;">'
            f'<div style="font-size:14px;font-weight:600;color:#111;">{title}'
            f'<span style="float:right;font-size:12px;color:{pcol};font-weight:600;margin-top:2px;">{pri}</span></div>'
            '<table style="width:100%;margin-top:6px;font-size:13px;color:#555;border-collapse:collapse;">'
            f'{"".join(rows)}</table></div>'
        )

    def _row_html(self, key: str, value_html: str) -> str:
        return (
            f'<tr><td style="padding:3px 0;width:96px;color:#94a3b8;vertical-align:top;">{key}</td>'
            f'<td style="padding:3px 0;">{value_html}</td></tr>'
        )

    def _progress_html(self, prog: int) -> str:
        return (
            '<div style="display:inline-block;background:#e5e7eb;border-radius:4px;'
            'height:6px;width:120px;vertical-align:middle;overflow:hidden;">'
            f'<div style="background:#2563eb;height:6px;width:{prog}%;"></div></div>'
            f'&nbsp;<span style="color:#111;">{prog}%</span>'
        )

    def run(self):
        while True:
            try:
                n = self.scan()
                if n:
                    print(f"邮件提醒：已发送 {n} 封提醒邮件")
            except Exception as e:
                sys.stderr.write(f"邮件提醒扫描异常: {e}\n")
            time.sleep(self.SCAN_INTERVAL)

    def start(self):
        # 线程首轮立即执行一次，之后每 SCAN_INTERVAL 秒一次
        threading.Thread(target=self.run, daemon=True).start()


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
        # 本地单机工具：禁止浏览器缓存静态资源
        # Why: 更新 web/ 后若被启发式缓存，旧 HTML + 新 JS 错配会报错
        self.send_header("Cache-Control", "no-cache")
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
        # 批量删除归档事项：/api/items/batch-delete
        if path == "/api/items/batch-delete":
            body = self._read_body()
            ids = body.get("ids", [])
            if not ids:
                return self._send_json({"error": "no ids provided"}, 400)
            deleted = 0
            for item_id in ids:
                if store.delete_item(item_id):
                    deleted += 1
            return self._send_json({"deleted": deleted})
        # 导入数据：/api/import
        if path == "/api/import":
            body = self._read_body()
            if not body or "items" not in body:
                return self._send_json({"error": "invalid payload, expect {items: [...]}"}, 400)
            result = store.import_data(body)
            return self._send_json(result)
        # 发送测试邮件：/api/mail/test
        # 直接用表单当前填写的配置发送；留空的字段（如授权码）回落到已保存的配置
        if path == "/api/mail/test":
            cfg = store.mail_settings_raw()
            payload = self._read_body() or {}
            for k in ("smtp_host", "smtp_port", "smtp_user", "smtp_pass",
                      "mail_from", "mail_to"):
                v = payload.get(k)
                if v not in (None, ""):
                    cfg[k] = str(v).strip()
            to = (cfg.get("mail_to") or "").strip()
            if not to:
                return self._send_json({"ok": False, "error": "请先在设置中填写默认收件邮箱"}, 400)
            if not (cfg.get("smtp_host") and cfg.get("smtp_user") and cfg.get("smtp_pass")):
                return self._send_json({"ok": False, "error": "请先填写 SMTP 服务器、账号和授权码"}, 400)
            try:
                MailReminder(store)._send(
                    cfg.get("smtp_host"), int(cfg.get("smtp_port") or 465),
                    cfg.get("smtp_user"), cfg.get("smtp_pass"),
                    (cfg.get("mail_from") or "").strip() or cfg.get("smtp_user"),
                    to, "【待办提醒】测试邮件",
                    "这是一封来自待办事项追踪工具的测试邮件。\n如果你收到了这封邮件，说明邮件提醒配置可用。")
                return self._send_json({"ok": True})
            except Exception as e:
                return self._send_json({"ok": False, "error": str(e)}, 400)
        # 标记邮件发送失败为已读：/api/mail/errors/read
        if path == "/api/mail/errors/read":
            store.mail_errors_mark_read(self._read_body().get("keys", []))
            return self._send_json({"ok": True})
        self.send_error(404, "Not Found")

    def do_PUT(self):
        path = urlparse(self.path).path
        if path == "/api/mail/settings":
            store.mail_settings_save(self._read_body())
            return self._send_json(store.mail_settings_get())
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
        if path == "/api/mail/settings":
            return self._send_json(store.mail_settings_get())
        if path == "/api/mail/errors":
            # 后台发送失败的未读列表，供前端轮询弹提示
            return self._send_json({"errors": store.mail_errors_unread()})
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

    # 启动邮件提醒后台线程（启动即扫一次，之后每 30 分钟一次）
    # Why: 与自动归档同理，无 cron 情况下靠"启动 + 定期循环"实现
    MailReminder(store).start()

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
