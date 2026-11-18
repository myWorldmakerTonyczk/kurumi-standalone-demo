"""Authenticated community forum and shared rooms.

The module owns the small SQLite schema used by the standalone demo. Schema
changes are additive so an existing local data/ledger.sqlite3 keeps working.
"""

import base64
import binascii
from io import BytesIO
import json
import re
import time

from aiohttp import web
from PIL import Image


def _json(value, default):
    try:
        result = json.loads(value)
    except (TypeError, ValueError):
        return default
    return result


def webp_data_url(value):
    """Return a WEBP data URL that fits community storage limits."""
    prefix = "data:image/webp;base64,"
    if not isinstance(value, str):
        raise ValueError("图片格式无效")
    if value.startswith(prefix) and len(value) <= 56000:
        return value
    match = re.fullmatch(r"data:image/(?:webp|png|jpe?g);base64,([A-Za-z0-9+/=]+)", value)
    if not match:
        raise ValueError("图片格式无效")
    try:
        raw = base64.b64decode(match[1], validate=True)
        with Image.open(BytesIO(raw)) as source:
            if getattr(source, "n_frames", 1) != 1:
                raise ValueError("图片须为静态图片")
            source.load()
            image = source.convert("RGBA" if "A" in source.getbands() else "RGB")
            image.thumbnail((960, 960))
            for quality in (85, 75, 65, 55, 45, 35):
                output = BytesIO()
                image.save(output, "WEBP", quality=quality, method=4)
                candidate = prefix + base64.b64encode(output.getvalue()).decode("ascii")
                if len(candidate) <= 56000:
                    return candidate
    except (binascii.Error, OSError, Image.DecompressionBombError) as exc:
        raise ValueError("图片无法读取") from exc
    raise ValueError("图片过大，无法转为社区图片")


def community_image(value):
    prefix = "data:image/webp;base64,"
    if not isinstance(value, str) or not value.startswith(prefix) or len(value) > 56000:
        raise ValueError("图片格式无效或图片过大")
    try:
        raw = base64.b64decode(value[len(prefix):], validate=True)
        with Image.open(BytesIO(raw), formats=["WEBP"]) as image:
            if max(image.size) > 960 or image.n_frames != 1:
                raise ValueError("图片须为最长边不超过 960 像素的静态图片")
            image.load()
    except (binascii.Error, OSError, Image.DecompressionBombError) as exc:
        raise ValueError("图片无法读取") from exc
    return {"kind": "image", "src": value}


class Community:
    def __init__(self, db, stickers=None, agent_inbox=None):
        self.db = db
        self.stickers = stickers
        self.agent_inbox = agent_inbox
        db.executescript("""
        CREATE TABLE IF NOT EXISTS community_posts(
          id INTEGER PRIMARY KEY, owner TEXT REFERENCES sim_users(id) ON DELETE CASCADE,
          title TEXT NOT NULL, body TEXT NOT NULL, created REAL NOT NULL DEFAULT (unixepoch()),
          updated REAL, tags TEXT NOT NULL DEFAULT '[]', status TEXT NOT NULL DEFAULT 'active',
          pinned INTEGER NOT NULL DEFAULT 0, featured INTEGER NOT NULL DEFAULT 0,
          reference_type TEXT NOT NULL DEFAULT '', reference_id TEXT NOT NULL DEFAULT '', pair TEXT NOT NULL DEFAULT '');
        CREATE TABLE IF NOT EXISTS community_messages(
          id INTEGER PRIMARY KEY, owner TEXT REFERENCES sim_users(id) ON DELETE CASCADE,
          post INTEGER REFERENCES community_posts(id) ON DELETE CASCADE,
          body TEXT NOT NULL, created REAL NOT NULL DEFAULT (unixepoch()),
          status TEXT NOT NULL DEFAULT 'active');
        CREATE TABLE IF NOT EXISTS community_groups(
          id INTEGER PRIMARY KEY, owner TEXT NOT NULL REFERENCES sim_users(id) ON DELETE CASCADE,
          name TEXT NOT NULL, created REAL NOT NULL DEFAULT (unixepoch()));
        CREATE TABLE IF NOT EXISTS community_group_members(
          room INTEGER REFERENCES community_groups(id) ON DELETE CASCADE,
          owner TEXT REFERENCES sim_users(id) ON DELETE CASCADE,
          role TEXT NOT NULL DEFAULT 'member', joined REAL NOT NULL DEFAULT (unixepoch()),
          last_read_message INTEGER NOT NULL DEFAULT 0, muted INTEGER NOT NULL DEFAULT 0,
          PRIMARY KEY(room,owner));
        CREATE TABLE IF NOT EXISTS community_group_messages(
          id INTEGER PRIMARY KEY, room INTEGER REFERENCES community_groups(id) ON DELETE CASCADE,
          owner TEXT REFERENCES sim_users(id) ON DELETE CASCADE, body TEXT NOT NULL,
          created REAL NOT NULL DEFAULT (unixepoch()), status TEXT NOT NULL DEFAULT 'active');
        CREATE TABLE IF NOT EXISTS community_comment_likes(
          comment INTEGER REFERENCES community_messages(id) ON DELETE CASCADE,
          owner TEXT REFERENCES sim_users(id) ON DELETE CASCADE, PRIMARY KEY(comment,owner));
        CREATE TABLE IF NOT EXISTS community_post_votes(
          post INTEGER REFERENCES community_posts(id) ON DELETE CASCADE,
          owner TEXT REFERENCES sim_users(id) ON DELETE CASCADE,
          vote INTEGER NOT NULL CHECK(vote IN (-1,1)), PRIMARY KEY(post,owner));
        CREATE TABLE IF NOT EXISTS community_inbox_seen(
          owner TEXT REFERENCES sim_users(id) ON DELETE CASCADE,
          kind TEXT, source INTEGER, PRIMARY KEY(owner,kind,source));
        CREATE INDEX IF NOT EXISTS community_group_history ON community_group_messages(room,id);
        CREATE INDEX IF NOT EXISTS community_room ON community_messages(post,id);
        CREATE INDEX IF NOT EXISTS community_post_created ON community_posts(created DESC,id DESC);
        """)
        db.executescript("""CREATE TABLE IF NOT EXISTS community_group_requests(
          room INTEGER REFERENCES community_groups(id) ON DELETE CASCADE,
          owner TEXT REFERENCES sim_users(id) ON DELETE CASCADE, PRIMARY KEY(room,owner));
        """)
        db.execute("""CREATE TABLE IF NOT EXISTS community_group_agents(
            room INTEGER REFERENCES community_groups(id) ON DELETE CASCADE,
            owner TEXT REFERENCES sim_users(id) ON DELETE CASCADE, PRIMARY KEY(room,owner))""")
        self._migrate()
        # Existing databases need the new columns before their indexes are built.
        db.executescript("""
        CREATE INDEX IF NOT EXISTS community_group_message_status ON community_group_messages(room,status,id DESC);
        CREATE INDEX IF NOT EXISTS community_message_status ON community_messages(post,status,id DESC);
        CREATE INDEX IF NOT EXISTS community_post_status ON community_posts(status,id DESC);
        CREATE INDEX IF NOT EXISTS community_member_owner ON community_group_members(owner,room);
        """)
        db.execute("""CREATE TABLE IF NOT EXISTS community_seed(
          owner TEXT PRIMARY KEY REFERENCES sim_users(id) ON DELETE CASCADE,
          created REAL NOT NULL DEFAULT (unixepoch()))""")

    def _migrate(self):
        migrations = {
            "community_posts": {
                "is_agent": "INTEGER NOT NULL DEFAULT 0", "title_media": "TEXT", "media": "TEXT", "updated": "REAL", "tags": "TEXT NOT NULL DEFAULT '[]'",
                "status": "TEXT NOT NULL DEFAULT 'active'", "pinned": "INTEGER NOT NULL DEFAULT 0",
                "featured": "INTEGER NOT NULL DEFAULT 0", "reference_type": "TEXT NOT NULL DEFAULT ''",
                "reference_id": "TEXT NOT NULL DEFAULT ''", "pair": "TEXT NOT NULL DEFAULT ''",
            },
            "community_messages": {"reply_to": "INTEGER", "status": "TEXT NOT NULL DEFAULT 'active'", "is_agent": "INTEGER NOT NULL DEFAULT 0", "media": "TEXT"},
            "community_comment_likes": {"vote": "INTEGER NOT NULL DEFAULT 1 CHECK(vote IN (-1,1))"},
            "community_groups": {"private": "INTEGER NOT NULL DEFAULT 0", "created": "REAL NOT NULL DEFAULT (unixepoch())"},
            "community_group_members": {
                "role": "TEXT NOT NULL DEFAULT 'member'", "joined": "REAL NOT NULL DEFAULT (unixepoch())",
                "last_read_message": "INTEGER NOT NULL DEFAULT 0", "muted": "INTEGER NOT NULL DEFAULT 0",
            },
            "community_group_messages": {"is_agent": "INTEGER NOT NULL DEFAULT 0", "status": "TEXT NOT NULL DEFAULT 'active'",
                                         "attachment": "TEXT", "reply_to": "INTEGER",
                                         "reply_media": "INTEGER NOT NULL DEFAULT 0",
                                         "mentions": "TEXT",
                                         "mention_depth": "INTEGER NOT NULL DEFAULT 0"},
        }
        for table, columns in migrations.items():
            existing = {row[1] for row in self.db.execute(f"PRAGMA table_info({table})")}
            for name, definition in columns.items():
                if name not in existing:
                    # SQLite forbids nonconstant defaults when adding a column
                    # to a populated table. Backfill legacy timestamps once;
                    # insert paths provide timestamps explicitly for new rows.
                    added_definition = definition.replace("DEFAULT (unixepoch())", "DEFAULT 0")
                    self.db.execute(f"ALTER TABLE {table} ADD COLUMN {name} {added_definition}")
                    if added_definition != definition:
                        self.db.execute(f"UPDATE {table} SET {name}=unixepoch() WHERE {name}=0")
        self.db.execute("""UPDATE community_group_members SET role='owner'
          WHERE EXISTS (SELECT 1 FROM community_groups g
                        WHERE g.id=room AND g.owner=community_group_members.owner)""")
        self.db.execute("UPDATE community_posts SET tags='[]' WHERE tags IS NULL OR tags=''")
        self.db.execute("UPDATE community_posts SET status='active' WHERE status IS NULL OR status=''")
        self.db.execute("UPDATE community_messages SET status='active' WHERE status IS NULL OR status=''")
        self.db.execute("UPDATE community_group_messages SET status='active' WHERE status IS NULL OR status=''")

    def seed_demo(self, owner):
        """Populate one small, clearly marked offline-demo community for first entry."""
        if self.db.execute("SELECT 1 FROM community_seed WHERE owner=?", (owner,)).fetchone():
            return
        if self.db.execute("SELECT 1 FROM community_posts LIMIT 1").fetchone():
            self.db.execute("INSERT OR IGNORE INTO community_seed(owner) VALUES(?)", (owner,))
            return
        posts = [
            ("新手交流：如何看待模拟盘的风险", "先看保证金和回撤，再决定是否开仓。这里是离线演示内容，欢迎分享你的观察。", ["新手", "风控"]),
            ("今日行情观察室", "可以在这里记录 EUR/USD、GBP/USD 的走势判断，发帖后大家一起讨论。", ["行情", "EUR/USD"]),
            ("社区使用说明", "论坛适合沉淀观点，群聊适合实时交流。所有交易都只写入模拟账本。", ["公告"]),
        ]
        for title, body, tags in posts:
            post = self.db.execute(
                "INSERT INTO community_posts(owner,title,body,tags,updated) VALUES(?,?,?,?,?)",
                (owner, title, body, json.dumps(tags, ensure_ascii=False), time.time()),
            ).lastrowid
            self.db.execute("INSERT INTO community_messages(owner,post,body) VALUES(?,?,?)",
                             (owner, post, "这是离线 Demo 的示例评论。"))
        room = self.db.execute("INSERT INTO community_groups(owner,name,created) VALUES(?,?,unixepoch())",
                               (owner, "行情观察室（示例群）")).lastrowid
        self.db.execute("INSERT INTO community_group_members(room,owner,role,joined) VALUES(?,?,?,unixepoch())", (room, owner, "owner"))
        message = self.db.execute("INSERT INTO community_group_messages(room,owner,body) VALUES(?,?,?)",
                                   (room, owner, "欢迎来到示例群，大家可以交流今日行情和风控思路。")).lastrowid
        self.db.execute("UPDATE community_group_members SET last_read_message=? WHERE room=? AND owner=?",
                        (message, room, owner))
        self.db.execute("INSERT INTO community_seed(owner) VALUES(?)", (owner,))

    def summary(self, owner):
        row = self.db.execute("""
          SELECT
            (SELECT count(*) FROM community_posts WHERE status='active') AS posts,
            (SELECT count(*) FROM community_messages WHERE status='active') AS comments,
            (SELECT count(*) FROM community_groups) AS groups,
            (SELECT count(*) FROM community_group_members WHERE owner=?) AS joined_groups,
            (SELECT count(*) FROM community_group_messages WHERE status='active' AND created >= unixepoch()-86400) AS messages_24h,
            (SELECT count(*) FROM community_posts WHERE status='active' AND created >= unixepoch()-86400) AS posts_24h
        """, (owner,)).fetchone()
        return dict(row)

    @staticmethod
    def _tags(value):
        tags = _json(value, [])
        return [str(tag)[:40] for tag in tags if isinstance(tag, str)][:10] if isinstance(tags, list) else []

    def _post_dict(self, row):
        result = dict(row)
        result["tags"] = self._tags(result.get("tags"))
        result["pinned"] = bool(result.get("pinned"))
        result["featured"] = bool(result.get("featured"))
        for key in ("title_media", "media"):
            if key in result:
                result[key] = _json(result[key], None)
        return result

    def forum_media(self, owner, body, prefix=""):
        image = body.get(prefix + "image")
        sticker_id = body.get(prefix + "sticker_id")
        if image is not None and sticker_id:
            raise ValueError("每处只能附一张图片或表情包")
        if image is not None:
            return json.dumps(community_image(image))
        if sticker_id:
            entry = self.stickers.find(owner, sticker_id) if self.stickers and isinstance(sticker_id, str) else None
            if not entry:
                raise ValueError("找不到该表情包")
            return json.dumps({"kind": "sticker", "label": entry["label"], "src": entry["src"]}, ensure_ascii=False)
        return None

    def handle(self, owner, method, post, body, query=None, *, is_agent=False):
        query = query or {}
        if post and (not str(post).isdecimal() or len(str(post)) > 18):
            raise ValueError("帖子编号无效")
        if post and not self.db.execute("SELECT 1 FROM community_posts WHERE id=? AND status='active'", (post,)).fetchone():
            raise web.HTTPNotFound(reason="帖子不存在")
        if method == "GET":
            if post == "":
                keyword = str(query.get("keyword", "")).strip()[:80]
                tag = str(query.get("tag", "")).strip()[:40]
                sort = query.get("sort", "latest")
                if sort not in {"latest", "hot"}:
                    raise ValueError("帖子排序无效")
                try:
                    limit = min(50, max(1, int(query.get("limit", 20))))
                    before = max(0, int(query.get("before", 0)))
                except (TypeError, ValueError):
                    raise ValueError("论坛分页参数无效")
                clauses = ["p.status='active'"]
                params = []
                if keyword:
                    clauses.append("(p.title LIKE ? OR p.body LIKE ?)")
                    params.extend([f"%{keyword}%", f"%{keyword}%"])
                if tag:
                    clauses.append("EXISTS (SELECT 1 FROM json_each(p.tags) WHERE json_each.value=?)")
                    params.append(tag)
                if before:
                    clauses.append("p.id<?")
                    params.append(before)
                order = "comments DESC, p.id DESC" if sort == "hot" else "p.pinned DESC, p.id DESC"
                rows = self.db.execute(f"""
                  SELECT p.id,p.title,substr(p.body,1,220) AS body,p.body AS full_body,p.created,p.updated,
                    p.tags,p.pinned,p.featured,p.reference_type,p.reference_id,p.pair,p.owner,p.is_agent,p.owner=? AS mine,u.nickname,p.title_media,
                    CASE WHEN p.title_media IS NULL THEN p.media END AS media,
                    (SELECT count(*) FROM community_messages WHERE post=p.id AND status='active') AS comments,
                    coalesce((SELECT max(created) FROM community_messages WHERE post=p.id AND status='active'),p.updated,p.created) AS last_activity
                  FROM community_posts p JOIN sim_users u ON u.id=p.owner
                  WHERE {' AND '.join(clauses)}
                  ORDER BY {order} LIMIT ?""", (owner, *params, limit + 1)).fetchall()
                has_more = len(rows) > limit
                entries = []
                for row in rows[:limit]:
                    item = self._post_dict(row)
                    item.pop("full_body", None)
                    item.update(self.vote_stats(owner, row["id"]))
                    entries.append(item)
                return {"entries": entries, "next_before": entries[-1]["id"] if has_more and entries else None,
                        "has_more": has_more}
            rows = self.db.execute("""
                  SELECT m.id,m.body,m.created,u.nickname,m.owner,m.is_agent,m.owner=? AS mine,m.media,m.reply_to,
                         (SELECT count(*) FROM community_comment_likes WHERE comment=m.id AND vote=1) AS likes,
                         (SELECT count(*) FROM community_comment_likes WHERE comment=m.id AND vote=-1) AS dislikes,
                         coalesce((SELECT vote FROM community_comment_likes WHERE comment=m.id AND owner=?),0) AS vote
                  FROM community_messages m JOIN sim_users u ON u.id=m.owner
                  WHERE m.post IS ? AND m.status='active' ORDER BY m.id DESC
                """, (owner, owner, int(post) if post else None)).fetchall()
            return {"entries": [dict(row, media=_json(row["media"], None), liked=row["vote"] == 1) for row in rows]}
        if method == "DELETE":
            comment = query.get("comment")
            if comment:
                try:
                    comment_id = int(comment)
                except (TypeError, ValueError):
                    raise ValueError("评论编号无效")
                self.delete_comment(owner, comment_id)
                return {"ok": True}
            if post == "":
                raise ValueError("请指定帖子或评论")
            self.delete_post(owner, int(post))
            return {"ok": True}
        if method != "POST":
            raise web.HTTPMethodNotAllowed(method, ["GET", "POST", "DELETE"])
        if body.get("action") in ("vote", "like"):
            if not post:
                raise ValueError("请指定帖子")
            comment = self._reply_target(post, body["comment"]) if "comment" in body else None
            if "comment" in body and comment is None:
                raise ValueError("请指定评论")
            if body["action"] == "like":
                if comment is None or type(body.get("liked")) is not bool:
                    raise ValueError("点赞参数无效")
                if not body["liked"]:
                    removed = self.db.execute("DELETE FROM community_comment_likes WHERE comment=? AND owner=? AND vote=1",
                                              (comment, owner))
                    if removed.rowcount and self.agent_inbox:
                        self.agent_inbox.forum(owner, post, None, vote=0, target=comment)
                    return {"ok": True, **self.vote_stats(owner, comment, comment=True)}
                vote = 1
            else:
                vote = body.get("vote")
                if type(vote) is not int or vote not in (-1, 0, 1):
                    raise ValueError("投票值须为 -1、0 或 1")
            table, key = self._vote_target(comment is not None)
            target = int(post) if comment is None else comment
            if vote:
                self.db.execute(f"""INSERT INTO {table}({key},owner,vote) VALUES(?,?,?)
                    ON CONFLICT({key},owner) DO UPDATE SET vote=excluded.vote""", (target, owner, vote))
            else:
                self.db.execute(f"DELETE FROM {table} WHERE {key}=? AND owner=?", (target, owner))
            if self.agent_inbox:
                self.agent_inbox.forum(owner, post, None, vote=vote, target=comment)
            return {"ok": True, **self.vote_stats(owner, target, comment=comment is not None)}
        content = body.get("body", "")
        media = self.forum_media(owner, body)
        title_media = self.forum_media(owner, body, "title_") if post == "" else None
        limit = 6000 if post == "" else 1500
        if not isinstance(content, str) or len(content.strip()) > limit or not (content.strip() or media or title_media):
            raise ValueError(f"请输入 1～{limit} 字，或添加图片或表情包")
        if post == "":
            title = body.get("title", "")
            if not isinstance(title, str) or len(title.strip()) > 100 or not (title.strip() or title_media):
                raise ValueError("请填写 1～100 字的标题，或添加标题图片或表情包")
            tags = body.get("tags") or []
            if isinstance(tags, str):
                tags = [item.strip() for item in tags.split(",") if item.strip()]
            if not isinstance(tags, list) or len(tags) > 10 or any(not isinstance(tag, str) or not tag.strip() for tag in tags):
                raise ValueError("标签格式无效")
            cursor = self.db.execute("""INSERT INTO community_posts(owner,title,body,tags,updated,reference_type,reference_id,pair,is_agent,title_media,media)
                                       VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                                     (owner, title.strip(), content.strip(),
                                      json.dumps([tag.strip()[:40] for tag in tags], ensure_ascii=False),
                                      time.time(), str(body.get("reference_type", ""))[:40],
                                      str(body.get("reference_id", ""))[:100], str(body.get("pair", ""))[:20], is_agent, title_media, media))
            return {"id": cursor.lastrowid}
        reply_to = self._reply_target(post, body.get("reply_to")) if post else None
        cursor = self.db.execute("INSERT INTO community_messages(owner,post,body,is_agent,media,reply_to) VALUES(?,?,?,?,?,?)",
                                 (owner, int(post) if post else None, content.strip(), is_agent, media, reply_to))
        self.db.execute("UPDATE community_posts SET updated=? WHERE id=?", (time.time(), int(post) if post else None))
        if post and self.agent_inbox:
            self.agent_inbox.forum(owner, post, cursor.lastrowid)
        return {"id": cursor.lastrowid}

    def _reply_target(self, post, value):
        if value is None:
            return None
        if type(value) is not int or not 0 < value < 2**63:
            raise ValueError("评论编号无效")
        if not self.db.execute(
            "SELECT 1 FROM community_messages WHERE post=? AND id=? AND status='active'", (post, value)
        ).fetchone():
            raise web.HTTPNotFound(reason="评论不存在或已删除")
        return value

    def inbox_rows(self, owner, *, unread_only, bulk=False):
        account = self.db.execute("""
            SELECT u.nickname, (SELECT count(*) FROM sim_users other
                  WHERE other.nickname=u.nickname COLLATE NOCASE AND other.id<u.id) AS duplicate
            FROM sim_users u WHERE u.id=?
        """, (owner,)).fetchone()
        nickname = account["nickname"]
        if account["duplicate"]:
            nickname += f"（{account['duplicate'] + 1}）"
        mention = re.compile(r"@" + re.escape(nickname) + r"(?=$|[\s，。！？、,.!?：:])", re.IGNORECASE)
        forum = self.db.execute("""
            SELECT 'forum' AS kind,m.id,m.owner,m.is_agent,u.nickname,m.body,m.created,
                   m.post AS target,p.title AS label,
                   p.owner=? OR parent.owner=? AS addressed
            FROM community_messages m JOIN community_posts p ON p.id=m.post
            JOIN sim_users u ON u.id=m.owner
            LEFT JOIN community_messages parent ON parent.id=m.reply_to
            WHERE m.status='active' AND p.status='active' AND (m.owner<>? OR m.is_agent)
              AND (p.owner=? OR parent.owner=? OR instr(m.body, '@')>0)
            ORDER BY m.id DESC LIMIT ?
        """, (owner, owner, owner, owner, owner, -1 if bulk else 1000)).fetchall()
        forum = [row for row in forum if row["addressed"] or mention.search(row["body"])]
        private = self.db.execute("""
            SELECT 'private' AS kind,c.id,? AS owner,1 AS is_agent,u.nickname,
                   c.content AS body,c.created,c.game AS target,'' AS label
            FROM demo_chat c JOIN games g ON g.id=c.game JOIN sim_users u ON u.id=?
            WHERE json_extract(g.data,'$.owner_id')=? AND c.role='assistant'
              AND (NOT ? OR NOT EXISTS (
                  SELECT 1 FROM community_inbox_seen s WHERE s.owner=? AND s.kind='private' AND s.source=c.id
              ))
            ORDER BY c.id DESC LIMIT ?
        """, (owner, owner, owner, unread_only, owner, -1 if bulk else 100)).fetchall()
        group = self.db.execute("""
            SELECT 'group' AS kind,m.id,m.owner,m.is_agent,u.nickname,m.body,m.created,
                   m.room AS target,g.name AS label,m.mentions
            FROM community_group_members gm JOIN community_groups g ON g.id=gm.room
            JOIN community_group_messages m ON m.room=g.id JOIN sim_users u ON u.id=m.owner
            WHERE gm.owner=? AND m.status='active' AND (m.owner<>? OR m.is_agent) AND m.created>=gm.joined
              AND instr(m.body, '@')>0
            ORDER BY m.id DESC LIMIT ?
        """, (owner, owner, -1 if bulk else 1000)).fetchall()
        group_mentions = []
        for row in group:
            targets = _json(row["mentions"], None)
            if (f"user:{owner}" in targets if targets is not None else mention.search(row["body"])):
                group_mentions.append(row)
        return [*forum, *private, *group_mentions]

    def inbox(self, owner, *, unread_only=False):
        entries = sorted(self.inbox_rows(owner, unread_only=unread_only),
                         key=lambda row: (row["created"] or 0, row["id"]), reverse=True)
        seen = {(row["kind"], row["source"]) for row in self.db.execute(
            "SELECT kind,source FROM community_inbox_seen WHERE owner=?", (owner,))}
        result = [dict(row, body=row["body"][:180], unread=(row["kind"], row["id"]) not in seen)
                  for row in entries if not unread_only or (row["kind"], row["id"]) not in seen][:100]
        for entry in result:
            entry.pop("addressed", None)
            entry.pop("mentions", None)
        return result

    def mark_inbox_read(self, owner, kind, source):
        if kind not in ("forum", "group", "private") or type(source) is not int or not 0 < source < 2**63:
            raise ValueError("消息编号无效")
        if kind == "private":
            self.db.execute("""
                INSERT OR IGNORE INTO community_inbox_seen(owner,kind,source)
                SELECT ?, 'private', c.id FROM demo_chat c JOIN games g ON g.id=c.game
                WHERE json_extract(g.data,'$.owner_id')=? AND c.role='assistant' AND c.id<=?
                  AND c.game=(SELECT game FROM demo_chat WHERE id=? AND role='assistant')
            """, (owner, owner, source, source))
        else:
            self.db.execute("INSERT OR IGNORE INTO community_inbox_seen VALUES(?,?,?)", (owner, kind, source))

    def mark_inbox_read_all(self, owner):
        self.db.execute("SAVEPOINT mark_all_inbox_read")
        try:
            rows = self.inbox_rows(owner, unread_only=True, bulk=True)
            marked = self.db.executemany(
                "INSERT OR IGNORE INTO community_inbox_seen(owner,kind,source) VALUES(?,?,?)",
                ((owner, row["kind"], row["id"]) for row in rows),
            ).rowcount
            self.db.execute("""UPDATE community_group_members AS member SET last_read_message=max(
                member.last_read_message, coalesce((SELECT max(id) FROM community_group_messages
                    WHERE room=member.room AND status='active'), 0))
                WHERE member.owner=?""", (owner,))
            return marked
        except BaseException:
            self.db.execute("ROLLBACK TO mark_all_inbox_read")
            raise
        finally:
            self.db.execute("RELEASE mark_all_inbox_read")

    @staticmethod
    def _vote_target(comment):
        return ("community_comment_likes", "comment") if comment else ("community_post_votes", "post")

    def vote_stats(self, viewer, target, *, comment=False):
        table, key = self._vote_target(comment)
        counts = self.db.execute(f"""SELECT count(CASE WHEN vote=1 THEN 1 END),
            count(CASE WHEN vote=-1 THEN 1 END), coalesce(max(CASE WHEN owner=? THEN vote END),0)
            FROM {table} WHERE {key}=?""", (viewer, target)).fetchone()
        likes, dislikes, vote = counts
        return dict(likes=likes, dislikes=dislikes, vote=vote, liked=vote == 1)

    def thread(self, post, *, viewer=None):
        if not str(post).isdecimal() or len(str(post)) > 18:
            raise ValueError("帖子编号无效")
        row = self.db.execute("""
          SELECT p.id,p.owner,p.title,p.body,p.created,p.updated,p.tags,p.pinned,p.featured,
                 p.reference_type,p.reference_id,p.pair,p.is_agent,u.nickname,p.title_media,p.media
          FROM community_posts p JOIN sim_users u ON u.id=p.owner
          WHERE p.id=? AND p.status='active'""", (post,)).fetchone()
        if not row:
            raise web.HTTPNotFound(reason="帖子不存在")
        result = self._post_dict(row)
        result["comments"] = self.db.execute(
            "SELECT count(*) FROM community_messages WHERE post=? AND status='active'", (post,)
        ).fetchone()[0]
        if viewer is not None:
            result.update(self.vote_stats(viewer, post))
        return result

    def _require_member(self, owner, room):
        member = self.db.execute("""SELECT gm.*,g.owner AS group_owner,g.name
                                  FROM community_group_members gm JOIN community_groups g ON g.id=gm.room
                                  WHERE gm.room=? AND gm.owner=?""", (room, owner)).fetchone()
        if not member:
            raise web.HTTPForbidden(reason="请先加入群聊")
        return member

    def group_attachment(self, owner, room, message):
        self._require_member(owner, room)
        row = self.db.execute("""SELECT attachment FROM community_group_messages
            WHERE room=? AND id=? AND status='active'""", (room, message)).fetchone()
        media = _json(row["attachment"], None) if row else None
        prefix = "data:image/webp;base64,"
        if not media or not media["src"].startswith(prefix):
            raise web.HTTPNotFound()
        return base64.b64decode(media["src"][len(prefix):], validate=True)

    def _add_member(self, room, owner):
        self.db.execute("""INSERT OR IGNORE INTO community_group_members(room,owner,role,last_read_message,joined)
            VALUES(?,?,'member',(SELECT coalesce(max(id),0) FROM community_group_messages WHERE room=?),unixepoch())""",
            (room, owner, room))

    def groups(self, owner, method, room, body, query=None):
        query = query or {}
        if method == "GET" and not room:
            rows = self.db.execute("""
                SELECT g.id,g.name,g.created,g.private,u.nickname AS leader,g.owner=? AS mine,
                    EXISTS(SELECT 1 FROM community_group_requests WHERE room=g.id AND owner=?) AS pending,
                    EXISTS(SELECT 1 FROM community_group_agents WHERE room=g.id AND owner=?) AS agent_joined,
                    EXISTS(SELECT 1 FROM community_group_members WHERE room=g.id AND owner=?) AS joined,
                    CASE WHEN gm.owner IS NULL THEN 0 ELSE (
                        SELECT count(*) FROM community_group_messages m
                        WHERE m.room=g.id AND m.status='active' AND m.id>gm.last_read_message AND (m.owner<>? OR m.is_agent)
                    ) END AS unread,
                    (SELECT count(*) FROM community_group_members WHERE room=g.id) AS members,
                    (SELECT count(*) FROM community_group_messages WHERE room=g.id AND status='active') AS messages,
                    (SELECT max(created) FROM community_group_messages WHERE room=g.id AND status='active') AS last_activity
                FROM community_groups g JOIN sim_users u ON u.id=g.owner
                LEFT JOIN community_group_members gm ON gm.room=g.id AND gm.owner=?
                ORDER BY g.id DESC LIMIT 100
            """, (owner, owner, owner, owner, owner, owner))
            return {"groups": [dict(row) for row in rows]}
        if method == "POST" and not room:
            name = body.get("name")
            if not isinstance(name, str) or not 1 <= len(name.strip()) <= 40:
                raise ValueError("群名须为 1～40 字")
            room_id = self.db.execute("INSERT INTO community_groups(owner,name,private,created) VALUES(?,?,?,unixepoch())", (owner, name.strip(), body.get("private") is True)).lastrowid
            self.db.execute("INSERT INTO community_group_members(room,owner,role,joined) VALUES(?,?,?,unixepoch())", (room_id, owner, "owner"))
            return {"id": room_id}
        if not str(room).isdecimal() or len(str(room)) > 18:
            raise ValueError("群编号无效")
        room_id = int(room)
        group = self.db.execute("SELECT owner,name,created,private FROM community_groups WHERE id=?", (room_id,)).fetchone()
        if not group:
            raise web.HTTPNotFound(reason="群聊不存在")
        operation = body.get("action") if method in {"POST", "DELETE"} else None
        if operation in {"requests", "approve", "reject", "invite-member"}:
            if owner != group["owner"]:
                raise web.HTTPForbidden(reason="只有群主可以管理成员")
            if operation == "requests":
                rows = self.db.execute("""SELECT u.id,u.nickname FROM community_group_requests r
                    JOIN sim_users u ON u.id=r.owner WHERE r.room=? ORDER BY u.nickname""", (room_id,))
                return {"requests": [dict(row) for row in rows]}
            applicant = body.get("applicant")
            if operation == "invite-member":
                user = self.db.execute("SELECT id FROM sim_users WHERE nickname=? COLLATE NOCASE", (str(applicant).strip(),)).fetchall()
                if len(user) != 1:
                    raise ValueError("请输入唯一匹配的成员昵称")
                applicant = user[0][0]
            elif not self.db.execute("SELECT 1 FROM community_group_requests WHERE room=? AND owner=?", (room_id, applicant)).fetchone():
                raise web.HTTPNotFound(reason="入群申请已处理")
            if operation != "reject":
                self._add_member(room_id, applicant)
            self.db.execute("DELETE FROM community_group_requests WHERE room=? AND owner=?", (room_id, applicant))
            return {"ok": True}
        if operation in {"invite-agent", "remove-agent"}:
            self._require_member(owner, room_id)
            if operation == "invite-agent":
                self.db.execute("INSERT OR IGNORE INTO community_group_agents VALUES(?,?)", (room_id, owner))
            else:
                self.db.execute("DELETE FROM community_group_agents WHERE room=? AND owner=?", (room_id, owner))
            return {"ok": True}
        if operation == "join":
            if group["private"] and not self.db.execute("SELECT 1 FROM community_group_members WHERE room=? AND owner=?", (room_id, owner)).fetchone():
                self.db.execute("INSERT OR IGNORE INTO community_group_requests VALUES(?,?)", (room_id, owner))
                return {"pending": True}
            self._add_member(room_id, owner)
            return {"ok": True}
        if operation == "leave":
            if owner == group["owner"]:
                raise ValueError("群主可以解散群聊")
            self.db.execute("DELETE FROM community_group_agents WHERE room=? AND owner=?", (room_id, owner))
            self.db.execute("DELETE FROM community_group_members WHERE room=? AND owner=?", (room_id, owner))
            return {"ok": True}
        if operation == "delete":
            if owner != group["owner"]:
                raise web.HTTPForbidden(reason="只有群主可以解散群聊")
            self.db.execute("DELETE FROM community_groups WHERE id=?", (room_id,))
            return {"ok": True}
        member = self._require_member(owner, room_id)
        if method == "GET":
            try:
                limit = min(100, max(1, int(query.get("limit", 50))))
                after = int(query.get("after", 0))
                before = int(query.get("before", 0))
                if not 0 <= max(after, before) < 2**63 or min(after, before) < 0 or (after and before):
                    raise ValueError
            except (TypeError, ValueError):
                raise ValueError("群聊分页参数无效") from None
            lazy_media = query.get("media") == "lazy"
            order = "ASC" if after else "DESC"
            messages = self.db.execute(f"""
                SELECT m.id,m.body,m.created,u.nickname,m.owner,m.is_agent,m.attachment,
                    m.owner=? AND NOT m.is_agent AS mine,m.reply_to,m.reply_media,
                    q.owner AS quote_owner,qu.nickname AS quote_nickname,q.is_agent AS quote_agent,
                    substr(q.body,1,160) AS quote_text,json_extract(q.attachment,'$.kind') AS quote_kind
                FROM community_group_messages m JOIN sim_users u ON u.id=m.owner
                LEFT JOIN community_group_messages q ON q.id=m.reply_to AND q.room=m.room AND q.status='active'
                LEFT JOIN sim_users qu ON qu.id=q.owner
                WHERE m.room=? AND m.status='active' AND m.id>? AND (?=0 OR m.id<?)
                ORDER BY m.id {order} LIMIT ?""", (owner, room_id, after, before, before, limit + 1)).fetchall()
            has_more = len(messages) > limit
            messages = messages[:limit]
            if after:
                messages.reverse()
            members = self.db.execute("""
                SELECT gm.owner,gm.role,gm.joined,gm.muted,u.nickname
                FROM community_group_members gm JOIN sim_users u ON u.id=gm.owner
                WHERE gm.room=? ORDER BY gm.role='owner' DESC,gm.joined ASC""", (room_id,)).fetchall()
            unread = self.db.execute("""SELECT count(*) FROM community_group_messages
                WHERE room=? AND status='active' AND id>? AND (owner<>? OR is_agent)""",
                                     (room_id, member["last_read_message"], owner)).fetchone()[0]
            entries = []
            for row in messages:
                entry = {key: row[key] for key in ("id", "body", "created", "nickname", "owner", "is_agent", "mine")}
                attachment = _json(row["attachment"], None)
                if lazy_media and attachment:
                    attachment = {"kind": attachment["kind"], "label": attachment.get("label", ""),
                                  "url": f"/api/groups?room={room_id}&attachment={row['id']}"}
                entry["attachment"] = attachment
                if row["reply_to"] is not None:
                    entry["quote"] = dict(id=row["reply_to"], owner=row["quote_owner"], nickname=row["quote_nickname"],
                                          is_agent=row["quote_agent"], body=row["quote_text"],
                                          kind=row["quote_kind"] if row["reply_media"] or not row["quote_text"] else None)
                entries.append(entry)
            return {"group": dict(id=room_id,name=group["name"],created=group["created"],owner=group["owner"],
                                  role=member["role"],muted=bool(member["muted"]),unread=unread,
                                  members_count=len(members),messages_count=self.db.execute(
                                      "SELECT count(*) FROM community_group_messages WHERE room=? AND status='active'", (room_id,)).fetchone()[0]),
                    "members": [dict(row, muted=bool(row["muted"])) for row in members],
                    "entries": entries, "has_more": has_more}
        if method != "POST":
            raise web.HTTPMethodNotAllowed(method, ["GET", "POST"])
        if operation in {"read", "mark-read"}:
            message_id = body.get("message_id")
            if message_id is None:
                message_id = self.db.execute("SELECT coalesce(max(id),0) FROM community_group_messages WHERE room=?", (room_id,)).fetchone()[0]
            try:
                message_id = max(0, int(message_id))
            except (TypeError, ValueError):
                raise ValueError("消息编号无效")
            latest = self.db.execute("SELECT coalesce(max(id),0) FROM community_group_messages WHERE room=?", (room_id,)).fetchone()[0]
            message_id = min(message_id, latest)
            self.db.execute("UPDATE community_group_members SET last_read_message=max(last_read_message,?) WHERE room=? AND owner=?",
                            (message_id, room_id, owner))
            unread = self.db.execute("""SELECT count(*) FROM community_group_messages m
                JOIN community_group_members member ON member.room=m.room AND member.owner=?
                WHERE m.room=? AND m.status='active' AND m.id>member.last_read_message
                  AND (m.owner<>member.owner OR m.is_agent)""", (owner, room_id)).fetchone()[0]
            return {"ok": True, "last_read_message": message_id, "unread": unread}
        content = body.get("body", "")
        attachment = community_image(body["image"]) if body.get("image") is not None else None
        if not isinstance(content, str) or len(content.strip()) > 1500 or not (content.strip() or attachment):
            raise ValueError("请输入 1～1500 字或添加一张图片")
        reply_to = body.get("reply_to")
        reply_media = body.get("reply_media", False)
        if type(reply_media) is not bool or (reply_to is None and reply_media):
            raise ValueError("引用格式无效")
        if reply_to is not None:
            if type(reply_to) is not int or not 0 < reply_to <= 2**63 - 1:
                raise ValueError("引用消息编号无效")
            target = self.db.execute("""SELECT attachment FROM community_group_messages
                WHERE id=? AND room=? AND status='active'""", (reply_to, room_id)).fetchone()
            if not target or (reply_media and not target["attachment"]):
                raise ValueError("引用的消息或图片不可用")
        cursor = self.db.execute("""INSERT INTO community_group_messages
            (room,owner,body,created,attachment,reply_to,reply_media) VALUES(?,?,?,?,?,?,?)""",
            (room_id, owner, content.strip(), time.time(), json.dumps(attachment) if attachment else None,
             reply_to, reply_media))
        return {"ok": True, "id": cursor.lastrowid, "message_id": cursor.lastrowid}

    def delete_comment(self, owner, comment_id, *, actor_is_admin=False, reason=""):
        row = self.db.execute("SELECT owner,post FROM community_messages WHERE id=? AND status='active'", (comment_id,)).fetchone()
        if not row:
            raise web.HTTPNotFound(reason="评论不存在")
        if not actor_is_admin and row["owner"] != owner:
            raise web.HTTPForbidden(reason="只能删除自己的评论")
        self.db.execute("UPDATE community_messages SET status='deleted',body='该评论已删除' WHERE id=?", (comment_id,))
        self.db.execute("UPDATE community_posts SET updated=? WHERE id=?", (time.time(), row["post"]))
        return {"ok": True}

    def delete_post(self, owner, post_id, *, actor_is_admin=False, reason=""):
        row = self.db.execute("SELECT owner FROM community_posts WHERE id=? AND status='active'", (post_id,)).fetchone()
        if not row:
            raise web.HTTPNotFound(reason="帖子不存在")
        if not actor_is_admin and row["owner"] != owner:
            raise web.HTTPForbidden(reason="只能删除自己的帖子")
        self.db.execute("UPDATE community_posts SET status='deleted',updated=? WHERE id=?", (time.time(), post_id))
        self.db.execute("UPDATE community_messages SET status='deleted',body='该评论已删除' WHERE post=?", (post_id,))
        return {"ok": True}

    def share_review(self, owner, review, title="", body="", tags=None, pair=""):
        review_stats = review.get("stats") or {}
        stats = review.get("pair_stats") or []
        current = next((item for item in stats if item.get("pair") == pair), None) if pair else (stats[0] if stats else None)
        pair = pair or (current or {}).get("pair", review_stats.get("pair", review.get("pair", "")))
        title = (title or f"{pair} {review.get('days', 1)}日交易复盘")[:100]
        body = (body or review.get("publishable_body") or review.get("summary") or "")[:6000]
        if not body:
            raise ValueError("复盘内容不能为空")
        return self.handle(owner, "POST", "", {"title": title, "body": body,
            "tags": tags or ["交易复盘"], "reference_type": "trade_review",
            "reference_id": str(review.get("end", review_stats.get("end", ""))), "pair": pair})

    def group_messages_for_summary(self, owner, room, limit=80):
        member = self._require_member(owner, int(room))
        rows = self.db.execute("""SELECT m.id,m.body,m.created,u.nickname
            FROM community_group_messages m JOIN sim_users u ON u.id=m.owner
            WHERE m.room=? AND m.status='active' ORDER BY m.id DESC LIMIT ?""",
                               (int(room), min(100, max(1, limit)))).fetchall()
        return dict(room=int(room), name=member["name"], messages=[dict(row) for row in reversed(rows)])

    def daily_snapshot(self, owner):
        posts = self.handle(owner, "GET", "", {}, {"sort": "hot", "limit": 8})["entries"]
        rooms = self.groups(owner, "GET", "", {})["groups"][:8]
        return {"posts": posts, "groups": rooms}

