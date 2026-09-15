#!/usr/bin/env python3
"""一次性迁移脚本：把 data.json 导入 data.db。

Why: 历史数据存在 JSON 文件里，迁到 SQLite 后需把存量事项导入新库。
脚本幂等：每次运行会重建 data.db。

用法：
    python3 migrate_to_sqlite.py [源json路径] [目标db路径]

默认：
    源：./data.json
    目标：./data.db
"""

import json
import os
import sqlite3
import sys

# 复用 app.py 中定义的表结构，避免重复
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from app import SCHEMA_SQL  # noqa: E402

BASE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_SRC = os.path.join(BASE, "data.json")
DEFAULT_DST = os.path.join(BASE, "data.db")


def main():
    src = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_SRC
    dst = sys.argv[2] if len(sys.argv) > 2 else DEFAULT_DST

    if not os.path.exists(src):
        print(f"源文件不存在：{src}")
        sys.exit(1)

    with open(src, "r", encoding="utf-8") as f:
        data = json.load(f)

    items = data.get("items", [])
    print(f"源文件：{src}（{len(items)} 条事项）")
    print(f"目标库：{dst}")

    # 若库已存在，删除重建（保证幂等）
    if os.path.exists(dst):
        os.remove(dst)
        print(f"已删除旧库：{dst}")

    conn = sqlite3.connect(dst)
    try:
        conn.executescript(SCHEMA_SQL)
        # 必须显式开启外键约束（与运行时一致）
        conn.execute("PRAGMA foreign_keys = ON")

        item_count = 0
        comment_count = 0

        for it in items:
            conn.execute(
                """INSERT INTO items
                   (id, parent_id, title, owner, status, priority, progress,
                    plan_end, actual_end, remark, tags, depends_on,
                    sort_order, created_at, updated_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    it["id"],
                    it.get("parent_id"),
                    it.get("title", ""),
                    it.get("owner", ""),
                    it.get("status", "pending"),
                    it.get("priority", "medium"),
                    int(it.get("progress", 0) or 0),
                    it.get("plan_end"),
                    it.get("actual_end"),
                    it.get("remark", ""),
                    json.dumps(it.get("tags", []), ensure_ascii=False),
                    json.dumps(it.get("depends_on", []), ensure_ascii=False),
                    int(it.get("sort_order", 0) or 0),
                    it.get("created_at", ""),
                    it.get("updated_at", ""),
                )
            )
            item_count += 1

            for c in it.get("comments", []):
                conn.execute(
                    """INSERT INTO comments
                       (id, item_id, author, text, created_at)
                       VALUES (?,?,?,?,?)""",
                    (
                        c["id"],
                        it["id"],
                        c.get("author", "匿名"),
                        c.get("text", ""),
                        c.get("created_at", ""),
                    )
                )
                comment_count += 1

        conn.commit()

        # 校验：回查数量是否匹配
        db_items = conn.execute("SELECT COUNT(*) FROM items").fetchone()[0]
        db_comments = conn.execute("SELECT COUNT(*) FROM comments").fetchone()[0]
        print(f"导入完成：{db_items} 条事项、{db_comments} 条评论")
        if db_items != item_count:
            print(f"警告：源中有 {item_count} 条事项，DB 中有 {db_items} 条")
        if db_comments != comment_count:
            print(f"警告：源中有 {comment_count} 条评论，DB 中有 {db_comments} 条")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
