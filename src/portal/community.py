"""Authenticated community forum and shared room."""
from aiohttp import web


class Community:
    def __init__(self, db):
        self.db = db
        db.executescript("""
        CREATE TABLE IF NOT EXISTS community_posts(
          id INTEGER PRIMARY KEY, owner TEXT REFERENCES sim_users(id) ON DELETE CASCADE,
          title TEXT NOT NULL, body TEXT NOT NULL, created REAL NOT NULL DEFAULT (unixepoch()));
        CREATE TABLE IF NOT EXISTS community_messages(
          id INTEGER PRIMARY KEY, owner TEXT REFERENCES sim_users(id) ON DELETE CASCADE,
          post INTEGER REFERENCES community_posts(id) ON DELETE CASCADE,
          body TEXT NOT NULL, created REAL NOT NULL DEFAULT (unixepoch()));
        CREATE TABLE IF NOT EXISTS community_groups(
          id INTEGER PRIMARY KEY, owner TEXT NOT NULL REFERENCES sim_users(id) ON DELETE CASCADE,
          name TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS community_group_members(
          room INTEGER REFERENCES community_groups(id) ON DELETE CASCADE,
          owner TEXT REFERENCES sim_users(id) ON DELETE CASCADE, PRIMARY KEY(room,owner));
        CREATE TABLE IF NOT EXISTS community_group_messages(
          id INTEGER PRIMARY KEY, room INTEGER REFERENCES community_groups(id) ON DELETE CASCADE,
          owner TEXT REFERENCES sim_users(id) ON DELETE CASCADE, body TEXT NOT NULL,
          created REAL NOT NULL DEFAULT (unixepoch()));
        CREATE INDEX IF NOT EXISTS community_group_history ON community_group_messages(room,id);
        CREATE INDEX IF NOT EXISTS community_room ON community_messages(post,id);
        CREATE TABLE IF NOT EXISTS community_group_requests(
          room INTEGER REFERENCES community_groups(id) ON DELETE CASCADE,
          owner TEXT REFERENCES sim_users(id) ON DELETE CASCADE, PRIMARY KEY(room,owner));
        """)
        db.execute("""CREATE TABLE IF NOT EXISTS community_group_agents(
            room INTEGER REFERENCES community_groups(id) ON DELETE CASCADE,
            owner TEXT REFERENCES sim_users(id) ON DELETE CASCADE, PRIMARY KEY(room,owner))""")
        for table in ("community_group_messages", "community_posts"):
            if "is_agent" not in {row[1] for row in db.execute(f"PRAGMA table_info({table})")}:
                db.execute(f"ALTER TABLE {table} ADD COLUMN is_agent INTEGER NOT NULL DEFAULT 0")
        columns = {row[1] for row in db.execute("PRAGMA table_info(community_groups)")}
        if "private" not in columns:
            db.execute("ALTER TABLE community_groups ADD COLUMN private INTEGER NOT NULL DEFAULT 0")

    def handle(self, owner, method, post, body, *, is_agent=False):
        if post and (not post.isdecimal() or len(post) > 18):
            raise ValueError("帖子编号无效")
        if post and not self.db.execute("SELECT 1 FROM community_posts WHERE id=?", (post,)).fetchone():
            raise web.HTTPNotFound(reason="帖子不存在")
        if method == "GET":
            if post == "":
                rows = self.db.execute("""
                  SELECT p.id,p.title,substr(p.body,1,220) AS body,p.created,u.nickname,p.owner,p.is_agent,
                    (SELECT count(*) FROM community_messages WHERE post=p.id) AS comments
                  FROM community_posts p JOIN sim_users u ON u.id=p.owner ORDER BY p.id DESC LIMIT 60
                """)
            else:
                rows = self.db.execute("""
                  SELECT m.id,m.body,m.created,u.nickname,m.owner,0 AS is_agent,m.owner=? AS mine
                  FROM community_messages m JOIN sim_users u ON u.id=m.owner
                  WHERE m.post IS ? ORDER BY m.id DESC LIMIT 100
                """, (owner, int(post) if post else None,))
            return {"entries": [dict(row) for row in rows]}
        if method != "POST":
            raise web.HTTPMethodNotAllowed(method, ["GET", "POST"])
        content = body.get("body")
        limit = 6000 if post == "" else 1500
        if not isinstance(content, str) or not 1 <= len(content.strip()) <= limit:
            raise ValueError(f"正文须为 1～{limit} 字")
        if post == "":
            title = body.get("title")
            if not isinstance(title, str) or not 1 <= len(title.strip()) <= 100:
                raise ValueError("标题须为 1～100 字")
            cursor = self.db.execute("INSERT INTO community_posts(owner,title,body,is_agent) VALUES(?,?,?,?)",
                                     (owner, title.strip(), content.strip(), is_agent))
        else:
            cursor = self.db.execute("INSERT INTO community_messages(owner,post,body) VALUES(?,?,?)",
                                     (owner, int(post) if post else None, content.strip()))
        return {"id": cursor.lastrowid}

    def thread(self, post):
        if not post.isdecimal() or len(post) > 18:
            raise ValueError("帖子编号无效")
        row = self.db.execute("""
          SELECT p.id,p.title,p.body,p.created,u.nickname,p.owner,p.is_agent FROM community_posts p
          JOIN sim_users u ON u.id=p.owner WHERE p.id=?
        """, (post,)).fetchone()
        if not row:
            raise web.HTTPNotFound(reason="帖子不存在")
        return dict(row)

    def groups(self, owner, method, room, body):
        if method == "GET" and not room:
            rows = self.db.execute("""
                SELECT g.id,g.name,g.private,u.nickname AS leader,g.owner=? AS mine,
                    EXISTS(SELECT 1 FROM community_group_requests WHERE room=g.id AND owner=?) AS pending,
                    EXISTS(SELECT 1 FROM community_group_members WHERE room=g.id AND owner=?) AS joined
                FROM community_groups g JOIN sim_users u ON u.id=g.owner ORDER BY g.id DESC LIMIT 100
            """, (owner, owner, owner))
            return {"groups": [dict(row) | {"mine": bool(row["mine"])} for row in rows]}
        if method == "POST" and not room:
            name = body.get("name")
            if not isinstance(name, str) or not 1 <= len(name.strip()) <= 40:
                raise ValueError("群名须为 1～40 字")
            self.db.execute("BEGIN")
            try:
                room = self.db.execute("INSERT INTO community_groups(owner,name,private) VALUES(?,?,?)",
                                       (owner, name.strip(), body.get("private") is True)).lastrowid
                self.db.execute("INSERT INTO community_group_members VALUES(?,?)", (room, owner))
                self.db.commit()
            except BaseException:
                self.db.rollback()
                raise
            return {"id": room}
        if not room.isdecimal() or len(room) > 18:
            raise ValueError("群编号无效")
        group = self.db.execute("SELECT owner,private FROM community_groups WHERE id=?", (room,)).fetchone()
        if not group:
            raise web.HTTPNotFound(reason="群聊不存在")
        operation = body.get("action") if method == "POST" else None
        is_owner = owner == group["owner"]
        if operation in ("requests", "approve", "reject", "delete") and not is_owner:
            raise web.HTTPForbidden(reason="只有群主可以审核申请或解散群聊")
        if operation in ("requests", "approve", "reject"):
            if operation == "requests":
                rows = self.db.execute("""
                    SELECT u.id,u.nickname FROM community_group_requests r
                    JOIN sim_users u ON u.id=r.owner WHERE r.room=? ORDER BY u.nickname
                """, (room,))
                return {"requests": [dict(row) for row in rows]}
            applicant = body.get("applicant")
            if not isinstance(applicant, str):
                raise ValueError("申请人无效")
            self.db.execute("BEGIN")
            try:
                if operation == "approve":
                    self.db.execute("""
                        INSERT OR IGNORE INTO community_group_members(room,owner)
                        SELECT room,owner FROM community_group_requests WHERE room=? AND owner=?
                    """, (room, applicant))
                result = self.db.execute("DELETE FROM community_group_requests WHERE room=? AND owner=?",
                                         (room, applicant))
                if not result.rowcount:
                    raise web.HTTPNotFound(reason="入群申请已处理")
                self.db.commit()
            except BaseException:
                self.db.rollback()
                raise
        elif operation == "join":
            if group["private"] and not self.db.execute(
                "SELECT 1 FROM community_group_members WHERE room=? AND owner=?", (room, owner)
            ).fetchone():
                self.db.execute("INSERT OR IGNORE INTO community_group_requests VALUES(?,?)", (room, owner))
                return {"pending": True}
            self.db.execute("INSERT OR IGNORE INTO community_group_members VALUES(?,?)", (room, owner))
        elif operation in ("invite-agent", "remove-agent"):
            if not self.db.execute("SELECT 1 FROM community_group_members WHERE room=? AND owner=?", (room, owner)).fetchone():
                raise web.HTTPForbidden(reason="请先加入群聊")
            if operation == "invite-agent":
                self.db.execute("INSERT OR IGNORE INTO community_group_agents VALUES(?,?)", (room, owner))
            else:
                self.db.execute("DELETE FROM community_group_agents WHERE room=? AND owner=?", (room, owner))
        elif operation == "leave":
            if is_owner:
                raise ValueError("群主可以解散群聊")
            self.db.execute("DELETE FROM community_group_agents WHERE room=? AND owner=?", (room, owner))
            self.db.execute("DELETE FROM community_group_members WHERE room=? AND owner=?", (room, owner))
        elif operation == "delete":
            self.db.execute("DELETE FROM community_groups WHERE id=? AND owner=?", (room, owner))
        else:
            if not self.db.execute("SELECT 1 FROM community_group_members WHERE room=? AND owner=?", (room, owner)).fetchone():
                raise web.HTTPForbidden(reason="请先加入群聊")
            if method == "GET":
                rows = self.db.execute("""
                    SELECT m.id,m.body,m.created,m.owner,u.nickname,m.is_agent,
                        m.owner=g.owner AND NOT m.is_agent AS leader,m.owner=? AND NOT m.is_agent AS mine
                    FROM community_group_messages m JOIN sim_users u ON u.id=m.owner
                    JOIN community_groups g ON g.id=m.room WHERE m.room=? ORDER BY m.id DESC LIMIT 100
                """, (owner, room))
                return {"entries": [dict(row) for row in rows]}
            if method != "POST":
                raise web.HTTPMethodNotAllowed(method, ["GET", "POST"])
            content = body.get("body")
            if not isinstance(content,str) or not 1 <= len(content.strip()) <= 1500:
                raise ValueError("消息须为 1～1500 字")
            cursor = self.db.execute("INSERT INTO community_group_messages(room,owner,body) VALUES(?,?,?)",
                                     (room, owner, content.strip()))
            return {"ok": True, "message_id": cursor.lastrowid}
        return {"ok": True}
