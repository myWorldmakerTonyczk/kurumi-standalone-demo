"""Account-owned static stickers."""
import base64
import json
from pathlib import Path
from random import choice
from io import BytesIO
from uuid import uuid4

from aiohttp import web
from PIL import Image


class Stickers:
    def __init__(self, db):
        self.db = db
        root = Path(__file__).resolve().parents[2] / "config" / "default_stickers"
        self.default_categories = []
        self.default_stickers = {}
        for group in json.loads((root / "manifest.json").read_text(encoding="utf-8")):
            category = "default:" + group["id"]
            self.default_categories.append(dict(id=category, label=group["label"], description=group["description"],
                                                count=len(group["items"]), builtin=True))
            for item in group["items"]:
                sticker_id = "default:" + item["file"]
                encoded = base64.b64encode((root / item["file"]).read_bytes()).decode("ascii")
                self.default_stickers[sticker_id] = dict(id=sticker_id, category=category, label=item["label"],
                                                            src="data:image/webp;base64," + encoded, builtin=True)
        db.execute("""CREATE TABLE IF NOT EXISTS agent_sticker_categories (
            id TEXT PRIMARY KEY,
            owner TEXT NOT NULL REFERENCES sim_users(id) ON DELETE CASCADE,
            label TEXT NOT NULL,
            description TEXT NOT NULL,
            UNIQUE(owner, label)
        )""")
        db.execute("""CREATE TABLE IF NOT EXISTS agent_stickers (
            id TEXT PRIMARY KEY,
            owner TEXT NOT NULL REFERENCES sim_users(id) ON DELETE CASCADE,
            label TEXT NOT NULL,
            src TEXT NOT NULL,
            category TEXT REFERENCES agent_sticker_categories(id) ON DELETE SET NULL
        )""")

        if "category" not in {row[1] for row in db.execute("PRAGMA table_info(agent_stickers)")}:
            db.execute("ALTER TABLE agent_stickers ADD COLUMN category TEXT REFERENCES agent_sticker_categories(id) ON DELETE SET NULL")

    def categories(self, owner):
        return self.default_categories + [dict(row) for row in self.db.execute("""
            SELECT c.id,c.label,c.description,count(s.id) AS count
            FROM agent_sticker_categories c LEFT JOIN agent_stickers s
                ON s.category=c.id AND s.owner=c.owner
            WHERE c.owner=? GROUP BY c.id ORDER BY c.rowid
        """, (owner,))]

    def category_update(self, owner, body):
        action = body["action"]
        category = body.get("category")
        if action != "category-save" or category:
            if not isinstance(category, str) or not self.db.execute(
                "SELECT 1 FROM agent_sticker_categories WHERE owner=? AND id=?", (owner, category)
            ).fetchone():
                raise web.HTTPNotFound(reason="找不到该分类")
        if action == "category-delete":
            self.db.execute("DELETE FROM agent_sticker_categories WHERE owner=? AND id=?", (owner, category))
            return
        label = body.get("label")
        description = body.get("description", "")
        if not isinstance(label, str) or not 1 <= len(label.strip()) <= 40:
            raise ValueError("分类名称须为 1～40 个字符")
        if not isinstance(description, str) or len(description.strip()) > 160:
            raise ValueError("分类说明最多 160 个字符")
        label = label.strip()
        if label in ("全部", "未分类") or self.db.execute(
            "SELECT 1 FROM agent_sticker_categories WHERE owner=? AND label=? AND id<>?",
            (owner, label, category or ""),
        ).fetchone():
            raise ValueError("该分类名称已被使用")
        if category:
            self.db.execute("UPDATE agent_sticker_categories SET label=?,description=? WHERE owner=? AND id=?",
                            (label, description.strip(), owner, category))
        else:
            if self.db.execute("SELECT count(*) FROM agent_sticker_categories WHERE owner=?", (owner,)).fetchone()[0] >= 30:
                raise ValueError("最多创建 30 个分类")
            self.db.execute("INSERT INTO agent_sticker_categories VALUES(?,?,?,?)",
                            (uuid4().hex, owner, label, description.strip()))

    def pick(self, owner, category):
        if category.startswith("default:"):
            entries = [entry for entry in self.default_stickers.values() if entry["category"] == category]
            return choice(entries) if entries else None
        return self.db.execute(
            "SELECT label,src FROM agent_stickers WHERE owner=? AND category=? ORDER BY random() LIMIT 1",
            (owner, category),
        ).fetchone()

    def list(self, owner):
        return list(self.default_stickers.values()) + [dict(row) for row in self.db.execute(
            "SELECT id,label,src,category FROM agent_stickers WHERE owner=? ORDER BY rowid", (owner,)
        )]

    def find(self, owner, sticker_id):
        if sticker_id in self.default_stickers:
            return self.default_stickers[sticker_id]
        return self.db.execute(
            "SELECT label,src FROM agent_stickers WHERE owner=? AND id=?", (owner, sticker_id)
        ).fetchone()

    def update(self, owner, body):
        action = body.get("action")
        if action in ("category-save", "category-delete"):
            return self.category_update(owner, body)
        sticker_id = body.get("id")
        if action not in ("add", "rename", "delete"):
            raise ValueError("未知表情包操作")
        if action != "add" and (not isinstance(sticker_id, str) or not self.find(owner, sticker_id)):
            raise web.HTTPNotFound(reason="找不到该表情包")
        if action != "add" and sticker_id in self.default_stickers:
            raise web.HTTPForbidden(reason="系统默认表情由系统统一管理")
        if action == "delete":
            self.db.execute("DELETE FROM agent_stickers WHERE owner=? AND id=?", (owner, sticker_id))
            return
        label = body.get("label")
        if not isinstance(label, str) or not 1 <= len(label.strip()) <= 40:
            raise ValueError("名称须为 1～40 个字符")
        category = body.get("category")
        if action == "rename" and "category" not in body:
            category = self.db.execute("SELECT category FROM agent_stickers WHERE owner=? AND id=?",
                                       (owner, sticker_id)).fetchone()[0]
        if category and (not isinstance(category, str) or not self.db.execute(
            "SELECT 1 FROM agent_sticker_categories WHERE owner=? AND id=?", (owner, category)
        ).fetchone()):
            raise ValueError("请选择自己的表情分类")
        category = category or None
        if action == "rename":
            self.db.execute("UPDATE agent_stickers SET label=?,category=? WHERE owner=? AND id=?",
                            (label.strip(), category, owner, sticker_id))
            return
        if self.db.execute("SELECT count(*) FROM agent_stickers WHERE owner=?", (owner,)).fetchone()[0] >= 30:
            raise ValueError("最多保存 30 张表情包")
        src = body.get("src")
        prefix = "data:image/webp;base64,"
        if not isinstance(src, str) or not src.startswith(prefix) or len(src) > 48_000:
            raise ValueError("表情包图片格式有误或体积过大")
        try:
            raw = base64.b64decode(src[len(prefix):], validate=True)
            with Image.open(BytesIO(raw), formats=["WEBP"]) as image:
                if max(image.size) > 256 or image.is_animated:
                    raise ValueError()
                image.load()
        except (OSError, ValueError, Image.DecompressionBombError):
            raise ValueError("请上传有效的静态图片") from None
        self.db.execute("INSERT INTO agent_stickers(id,owner,label,src,category) VALUES(?,?,?,?,?)",
                        (uuid4().hex, owner, label.strip(), src, category))
