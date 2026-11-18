"""Account-owned image copies with metadata from the vision model."""

import base64
import hashlib
import json
import re

from aiohttp import web

from .vision import describe_image

IMAGE_URL = re.compile(r"data:image/(webp|png);base64,([A-Za-z0-9+/=]+)")


def image_reference(media, reference):
    if media and media.get("kind") == "image":
        return {"reference": reference, "label": media.get("label", "")}
    return None


class ImageLibrary:
    def __init__(self, app):
        self.app = app
        self.db = app.db
        self.db.execute("""CREATE TABLE IF NOT EXISTS agent_images(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            owner TEXT NOT NULL REFERENCES sim_users(id) ON DELETE CASCADE,
            digest TEXT NOT NULL, src TEXT NOT NULL, name TEXT NOT NULL,
            category TEXT NOT NULL, description TEXT NOT NULL, tags TEXT NOT NULL,
            sources TEXT NOT NULL, created INTEGER NOT NULL DEFAULT (unixepoch()),
            UNIQUE(owner,digest))""")

    def recent_private(self, game):
        entries = self.db.execute("""SELECT id,content,attachments FROM demo_chat
            WHERE game=? AND attachments<>'[]' ORDER BY id DESC LIMIT 12""", (game,))
        return [ref | {"context": row["content"][:100]} for row in entries
                for index, media in enumerate(json.loads(row["attachments"]))
                if (ref := image_reference(media, f"private:{row['id']}:{index}"))][:12]

    def _source(self, owner, game, reference, current):
        if reference == "current" and current:
            return current
        match = re.fullmatch(r"(private|group|post|comment):([0-9]{1,18})(?::([0-9]{1,18}|title))?", reference)
        if not match:
            raise ValueError("没有可用的图片引用")
        kind, number, slot = match.groups()
        if kind == "private" and slot and slot.isdecimal():
            row = self.db.execute("""SELECT c.attachments FROM demo_chat c JOIN games g ON g.id=c.game
                WHERE c.game=? AND c.id=? AND json_extract(g.data,'$.owner_id')=?""", (game, number, owner)).fetchone()
            if row:
                media = json.loads(row[0])
                index = int(slot)
                return media[index] if index < len(media) else None
            return None
        elif kind == "group" and slot and slot.isdecimal():
            row = self.db.execute("""SELECT m.attachment FROM community_group_messages m
                JOIN community_group_members gm ON gm.room=m.room AND gm.owner=?
                WHERE m.room=? AND m.id=? AND m.status='active'""", (owner, number, slot)).fetchone()
        elif kind == "post" and slot in (None, "title"):
            column = "title_media" if slot else "media"
            row = self.db.execute(f"SELECT {column} FROM community_posts WHERE id=? AND status='active'", (number,)).fetchone()
        elif kind == "comment" and slot is None:
            row = self.db.execute("""SELECT m.media FROM community_messages m JOIN community_posts p ON p.id=m.post
                WHERE m.id=? AND m.status='active' AND p.status='active'""", (number,)).fetchone()
        else:
            return None
        return json.loads(row[0]) if row and row[0] else None

    def attachment(self, owner, game, *, reference="", image_id=0, current=None):
        if reference and image_id:
            raise ValueError("图片引用与图库 ID 只填一个")
        if image_id:
            row = self.get(owner, image_id)
            return {"kind": "image", "src": row["src"], "label": row["name"]}
        media = self._source(owner, game, reference, current)
        if not media or media.get("kind") != "image" or not IMAGE_URL.fullmatch(media.get("src", "")):
            raise ValueError("图片不存在、无权访问或格式无效")
        return {"kind": "image", "src": media["src"], "label": media.get("label", "")}

    @staticmethod
    def tags(values):
        if not isinstance(values, list) or any(not isinstance(value, str) for value in values):
            raise ValueError("标签须为文字列表")
        return list(dict.fromkeys(value.strip()[:32] for value in values if value.strip()))[:20]

    @staticmethod
    def metadata(row):
        return {key: row[key] for key in ("id", "name", "category", "description", "created")} | {
            "tags": json.loads(row["tags"]), "sources": json.loads(row["sources"]),
        }

    async def save(self, owner, game, reference, current=None, *, name="", category="", tags=None):
        media = self._source(owner, game, reference, current)
        src = media.get("src", "") if media and media.get("kind") == "image" else ""
        match = IMAGE_URL.fullmatch(src) if len(src) <= 2_800_000 else None
        if not match:
            raise ValueError("图片不存在、无权访问或超出图库大小限制")
        raw = base64.b64decode(match[2], validate=True)
        digest = hashlib.sha256(raw).hexdigest()
        supplied = self.tags(tags or [])
        params = (owner, digest)
        lookup = "SELECT * FROM agent_images WHERE owner=? AND digest=?"
        row = self.db.execute(lookup, params).fetchone()
        if not row:
            scan = await describe_image(self.app.profile(owner), src, "", self.app.model_url,
                audit=self.app.call_logs.callback(owner, "vision"), catalog=True)
            self.db.execute("""INSERT OR IGNORE INTO agent_images
                (owner,digest,src,name,category,description,tags,sources) VALUES(?,?,?,?,?,?,?,?)""",
                (*params, src, (name.strip() or scan["name"])[:80], (category.strip() or scan["category"])[:40],
                 scan["description"], json.dumps(self.tags(supplied + scan["tags"])), "[]"))
            row = self.db.execute(lookup, params).fetchone()
        source = {"kind": "private" if reference == "current" else reference.split(":")[0],
                  "reference": reference}
        if source["kind"] == "private":
            source["game"] = game
        sources = json.loads(row["sources"])
        if source not in sources:
            sources = [*sources, source][-30:]
        self.db.execute("UPDATE agent_images SET sources=?,tags=? WHERE id=? AND owner=?",
                        (json.dumps(sources), json.dumps(self.tags(supplied + json.loads(row["tags"]))), row["id"], owner))
        return self.metadata(self.get(owner, row["id"]))

    def get(self, owner, image_id):
        row = self.db.execute("SELECT * FROM agent_images WHERE owner=? AND id=?", (owner, image_id)).fetchone()
        if not row:
            raise web.HTTPNotFound(reason="图库中没有这张图片")
        return row

    def search(self, owner, query="", category="", tag="", source="", since=0, *, limit=30, before=0):
        terms = query.casefold().split()[:12]
        rows = self.db.execute("""SELECT id,name,category,description,tags,sources,created FROM agent_images
            WHERE owner=? AND created>=? AND (?=0 OR id<?) ORDER BY id DESC""", (owner, since, before, before))
        result = []
        for row in rows:
            item = self.metadata(row)
            text = " ".join((item["name"], item["category"], item["description"], *item["tags"])).casefold()
            if (all(term in text for term in terms) and (not category or item["category"] == category)
                    and (not tag or tag in item["tags"])
                    and (not source or any(origin["kind"] == source for origin in item["sources"]))):
                result.append(item)
                if len(result) >= limit:
                    break
        return result

    def update(self, owner, image_id, body):
        self.get(owner, image_id)
        fields = [body.get(key) for key in ("name", "category", "description")]
        if any(not isinstance(value, str) for value in fields) or not fields[0].strip():
            raise ValueError("请填写图片名称")
        name, category, description = (value.strip()[:limit] for value, limit in zip(fields, (80, 40, 1800)))
        self.db.execute("UPDATE agent_images SET name=?,category=?,description=?,tags=? WHERE owner=? AND id=?",
                        (name, category, description, json.dumps(self.tags(body.get("tags"))), owner, image_id))
