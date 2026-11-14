"""Authenticated community forum and shared rooms.

The module owns the small SQLite schema used by the standalone demo. Schema
changes are additive so an existing local data/ledger.sqlite3 keeps working.
"""

import json
import time

from aiohttp import web


def _json(value, default):
    try:
        result = json.loads(value)
    except (TypeError, ValueError):
        return default
    return result


class Community:
    def __init__(self, db):
        self.db = db
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
        CREATE TABLE IF NOT EXISTS community_audit_log(
          id INTEGER PRIMARY KEY, actor TEXT NOT NULL, action TEXT NOT NULL,
          target_type TEXT NOT NULL, target_id TEXT NOT NULL, reason TEXT NOT NULL DEFAULT '',
          created REAL NOT NULL DEFAULT (unixepoch()));
        CREATE INDEX IF NOT EXISTS community_group_history ON community_group_messages(room,id);
        CREATE INDEX IF NOT EXISTS community_room ON community_messages(post,id);
        CREATE INDEX IF NOT EXISTS community_post_created ON community_posts(created DESC,id DESC);
        """)
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
                "updated": "REAL", "tags": "TEXT NOT NULL DEFAULT '[]'",
                "status": "TEXT NOT NULL DEFAULT 'active'", "pinned": "INTEGER NOT NULL DEFAULT 0",
                "featured": "INTEGER NOT NULL DEFAULT 0", "reference_type": "TEXT NOT NULL DEFAULT ''",
                "reference_id": "TEXT NOT NULL DEFAULT ''", "pair": "TEXT NOT NULL DEFAULT ''",
            },
            "community_messages": {"status": "TEXT NOT NULL DEFAULT 'active'"},
            "community_groups": {"created": "REAL NOT NULL DEFAULT (unixepoch())"},
            "community_group_members": {
                "role": "TEXT NOT NULL DEFAULT 'member'", "joined": "REAL NOT NULL DEFAULT (unixepoch())",
                "last_read_message": "INTEGER NOT NULL DEFAULT 0", "muted": "INTEGER NOT NULL DEFAULT 0",
            },
            "community_group_messages": {"status": "TEXT NOT NULL DEFAULT 'active'"},
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
        return result

    def handle(self, owner, method, post, body, query=None):
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
                    p.tags,p.pinned,p.featured,p.reference_type,p.reference_id,p.pair,u.nickname,
                    (SELECT count(*) FROM community_messages WHERE post=p.id AND status='active') AS comments,
                    coalesce((SELECT max(created) FROM community_messages WHERE post=p.id AND status='active'),p.updated,p.created) AS last_activity
                  FROM community_posts p JOIN sim_users u ON u.id=p.owner
                  WHERE {' AND '.join(clauses)}
                  ORDER BY {order} LIMIT ?""", (*params, limit + 1)).fetchall()
                has_more = len(rows) > limit
                entries = []
                for row in rows[:limit]:
                    item = self._post_dict(row)
                    item.pop("full_body", None)
                    entries.append(item)
                return {"entries": entries, "next_before": entries[-1]["id"] if has_more and entries else None,
                        "has_more": has_more}
            rows = self.db.execute("""
                  SELECT m.id,m.body,m.created,u.nickname,m.owner=? AS mine
                  FROM community_messages m JOIN sim_users u ON u.id=m.owner
                  WHERE m.post=? AND m.status='active' ORDER BY m.id DESC LIMIT 100
                """, (owner, int(post))).fetchall()
            return {"entries": [dict(row) for row in rows]}
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
        content = body.get("body")
        limit = 6000 if post == "" else 1500
        if not isinstance(content, str) or not 1 <= len(content.strip()) <= limit:
            raise ValueError(f"正文须为 1～{limit} 字")
        if post == "":
            title = body.get("title")
            if not isinstance(title, str) or not 1 <= len(title.strip()) <= 100:
                raise ValueError("标题须为 1～100 字")
            tags = body.get("tags") or []
            if isinstance(tags, str):
                tags = [item.strip() for item in tags.split(",") if item.strip()]
            if not isinstance(tags, list) or len(tags) > 10 or any(not isinstance(tag, str) or not tag.strip() for tag in tags):
                raise ValueError("标签格式无效")
            cursor = self.db.execute("""INSERT INTO community_posts(owner,title,body,tags,updated,reference_type,reference_id,pair)
                                       VALUES(?,?,?,?,?,?,?,?)""",
                                     (owner, title.strip(), content.strip(),
                                      json.dumps([tag.strip()[:40] for tag in tags], ensure_ascii=False),
                                      time.time(), str(body.get("reference_type", ""))[:40],
                                      str(body.get("reference_id", ""))[:100], str(body.get("pair", ""))[:20]))
            return {"id": cursor.lastrowid}
        cursor = self.db.execute("INSERT INTO community_messages(owner,post,body) VALUES(?,?,?)",
                                 (owner, int(post), content.strip()))
        self.db.execute("UPDATE community_posts SET updated=? WHERE id=?", (time.time(), int(post)))
        return {"id": cursor.lastrowid}

    def thread(self, post):
        if not str(post).isdecimal() or len(str(post)) > 18:
            raise ValueError("帖子编号无效")
        row = self.db.execute("""
          SELECT p.id,p.owner,p.title,p.body,p.created,p.updated,p.tags,p.pinned,p.featured,
                 p.reference_type,p.reference_id,p.pair,u.nickname
          FROM community_posts p JOIN sim_users u ON u.id=p.owner
          WHERE p.id=? AND p.status='active'""", (post,)).fetchone()
        if not row:
            raise web.HTTPNotFound(reason="帖子不存在")
        result = self._post_dict(row)
        result["comments"] = self.db.execute(
            "SELECT count(*) FROM community_messages WHERE post=? AND status='active'", (post,)
        ).fetchone()[0]
        result.pop("owner", None)
        return result

    def _require_member(self, owner, room):
        member = self.db.execute("""SELECT gm.*,g.owner AS group_owner,g.name
                                  FROM community_group_members gm JOIN community_groups g ON g.id=gm.room
                                  WHERE gm.room=? AND gm.owner=?""", (room, owner)).fetchone()
        if not member:
            raise web.HTTPForbidden(reason="请先加入群聊")
        return member

    def groups(self, owner, method, room, body, query=None):
        query = query or {}
        if method == "GET" and not room:
            rows = self.db.execute("""
                SELECT g.id,g.name,g.created,u.nickname AS leader,g.owner=? AS mine,
                    EXISTS(SELECT 1 FROM community_group_members WHERE room=g.id AND owner=?) AS joined,
                    CASE WHEN gm.owner IS NULL THEN 0 ELSE (
                        SELECT count(*) FROM community_group_messages m
                        WHERE m.room=g.id AND m.status='active' AND m.id>gm.last_read_message AND m.owner<>?
                    ) END AS unread,
                    (SELECT count(*) FROM community_group_members WHERE room=g.id) AS members,
                    (SELECT count(*) FROM community_group_messages WHERE room=g.id AND status='active') AS messages,
                    (SELECT max(created) FROM community_group_messages WHERE room=g.id AND status='active') AS last_activity
                FROM community_groups g JOIN sim_users u ON u.id=g.owner
                LEFT JOIN community_group_members gm ON gm.room=g.id AND gm.owner=?
                ORDER BY g.id DESC LIMIT 100
            """, (owner, owner, owner, owner))
            return {"groups": [dict(row) for row in rows]}
        if method == "POST" and not room:
            name = body.get("name")
            if not isinstance(name, str) or not 1 <= len(name.strip()) <= 40:
                raise ValueError("群名须为 1～40 字")
            room_id = self.db.execute("INSERT INTO community_groups(owner,name,created) VALUES(?,?,unixepoch())", (owner, name.strip())).lastrowid
            self.db.execute("INSERT INTO community_group_members(room,owner,role,joined) VALUES(?,?,?,unixepoch())", (room_id, owner, "owner"))
            return {"id": room_id}
        if not str(room).isdecimal() or len(str(room)) > 18:
            raise ValueError("群编号无效")
        room_id = int(room)
        group = self.db.execute("SELECT owner,name,created FROM community_groups WHERE id=?", (room_id,)).fetchone()
        if not group:
            raise web.HTTPNotFound(reason="群聊不存在")
        operation = body.get("action") if method in {"POST", "DELETE"} else None
        if operation == "join":
            latest = self.db.execute("SELECT coalesce(max(id),0) FROM community_group_messages WHERE room=?", (room_id,)).fetchone()[0]
            self.db.execute("""INSERT OR IGNORE INTO community_group_members(room,owner,role,last_read_message,joined)
                               VALUES(?,?,'member',?,unixepoch())""", (room_id, owner, latest))
            return {"ok": True}
        if operation == "leave":
            if owner == group["owner"]:
                raise ValueError("群主可以解散群聊")
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
            except (TypeError, ValueError):
                raise ValueError("群聊分页参数无效")
            messages = self.db.execute("""
                SELECT m.id,m.body,m.created,u.nickname,m.owner=? AS mine
                FROM community_group_messages m JOIN sim_users u ON u.id=m.owner
                WHERE m.room=? AND m.status='active' ORDER BY m.id DESC LIMIT ?""", (owner, room_id, limit)).fetchall()
            members = self.db.execute("""
                SELECT gm.owner,gm.role,gm.joined,gm.muted,u.nickname
                FROM community_group_members gm JOIN sim_users u ON u.id=gm.owner
                WHERE gm.room=? ORDER BY gm.role='owner' DESC,gm.joined ASC""", (room_id,)).fetchall()
            unread = self.db.execute("""SELECT count(*) FROM community_group_messages
                WHERE room=? AND status='active' AND id>? AND owner<>?""",
                                     (room_id, member["last_read_message"], owner)).fetchone()[0]
            return {"group": dict(id=room_id,name=group["name"],created=group["created"],owner=group["owner"],
                                  role=member["role"],muted=bool(member["muted"]),unread=unread,
                                  members_count=len(members),messages_count=self.db.execute(
                                      "SELECT count(*) FROM community_group_messages WHERE room=? AND status='active'", (room_id,)).fetchone()[0]),
                    "members": [dict(row, muted=bool(row["muted"])) for row in members],
                    "entries": [dict(row) for row in messages]}
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
            return {"ok": True, "last_read_message": message_id}
        content = body.get("body")
        if not isinstance(content, str) or not 1 <= len(content.strip()) <= 1500:
            raise ValueError("消息须为 1～1500 字")
        cursor = self.db.execute("INSERT INTO community_group_messages(room,owner,body) VALUES(?,?,?)",
                                 (room_id, owner, content.strip()))
        return {"ok": True, "id": cursor.lastrowid}

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

    def moderation(self, actor, body=None, query=None):
        """Small admin moderation surface with an audit trail."""
        body, query = body or {}, query or {}
        if not body:
            posts = self.db.execute("""SELECT p.id,p.title,p.status,p.pinned,p.featured,p.created,u.nickname
                FROM community_posts p JOIN sim_users u ON u.id=p.owner ORDER BY p.id DESC LIMIT 100""").fetchall()
            comments = self.db.execute("""SELECT m.id,m.post,m.body,m.status,m.created,u.nickname
                FROM community_messages m JOIN sim_users u ON u.id=m.owner ORDER BY m.id DESC LIMIT 100""").fetchall()
            return {"posts": [dict(row) for row in posts], "comments": [dict(row) for row in comments]}
        action = body.get("action")
        reason = str(body.get("reason", ""))[:300]
        target_id = body.get("id")
        if action in {"delete-post", "pin-post", "feature-post"}:
            try:
                target_id = int(target_id)
            except (TypeError, ValueError):
                raise ValueError("帖子编号无效")
            if action == "delete-post":
                if not reason.strip():
                    raise ValueError("删除帖子必须填写原因")
                result = self.delete_post(actor, target_id, actor_is_admin=True, reason=reason)
            else:
                column = "pinned" if action == "pin-post" else "featured"
                cursor = self.db.execute(f"UPDATE community_posts SET {column}=1-{column},updated=? WHERE id=? AND status='active'",
                                         (time.time(), target_id))
                if not cursor.rowcount:
                    raise web.HTTPNotFound(reason="帖子不存在")
                result = {"ok": True}
            self.db.execute("INSERT INTO community_audit_log(actor,action,target_type,target_id,reason) VALUES(?,?,?,?,?)",
                            (actor, action, "post", str(target_id), reason))
            return result
        if action == "delete-comment":
            try:
                target_id = int(target_id)
            except (TypeError, ValueError):
                raise ValueError("评论编号无效")
            if not reason.strip():
                raise ValueError("删除评论必须填写原因")
            result = self.delete_comment(actor, target_id, actor_is_admin=True, reason=reason)
            self.db.execute("INSERT INTO community_audit_log(actor,action,target_type,target_id,reason) VALUES(?,?,?,?,?)",
                            (actor, action, "comment", str(target_id), reason))
            return result
        raise ValueError("管理操作无效")
