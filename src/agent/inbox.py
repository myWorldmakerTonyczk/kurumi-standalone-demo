"""Persistent unread state shared by all tasks of a platform agent."""
import json
import time
import uuid

SOURCES = ("forum", "group", "market", "system")


class AgentInbox:
    def __init__(self, engine):
        self.engine = engine
        self.db = engine.db
        self.db.executescript("""
        CREATE TABLE IF NOT EXISTS agent_inbox(
          id INTEGER PRIMARY KEY AUTOINCREMENT, owner TEXT NOT NULL REFERENCES sim_users(id) ON DELETE CASCADE,
          source TEXT NOT NULL, event TEXT NOT NULL, room INTEGER, reference TEXT,
          title TEXT NOT NULL, preview TEXT NOT NULL, mentioned INTEGER NOT NULL DEFAULT 0,
          created REAL NOT NULL, unread INTEGER NOT NULL DEFAULT 1, reopened REAL NOT NULL DEFAULT 0,
          UNIQUE(owner,source,event));
        CREATE INDEX IF NOT EXISTS agent_inbox_owner ON agent_inbox(owner,unread,id);
        CREATE TABLE IF NOT EXISTS agent_inbox_receipts(
          batch TEXT NOT NULL, item INTEGER NOT NULL REFERENCES agent_inbox(id) ON DELETE CASCADE,
          PRIMARY KEY(batch,item));
        CREATE TABLE IF NOT EXISTS agent_inbox_audit(
          id INTEGER PRIMARY KEY, owner TEXT NOT NULL REFERENCES sim_users(id) ON DELETE CASCADE,
          created REAL NOT NULL, action TEXT NOT NULL, batch TEXT, total INTEGER NOT NULL);
        """)

    def add(self, owner, source, event, title, preview="", *, room=None, reference=None, mentioned=False):
        self.db.execute("""INSERT OR IGNORE INTO agent_inbox
          (owner,source,event,room,reference,title,preview,mentioned,created) VALUES(?,?,?,?,?,?,?,?,?)""",
          (owner, source, str(event), room, json.dumps(reference or {}, ensure_ascii=False),
           title, preview[:120] + ("…" if len(preview) > 120 else ""), int(mentioned), time.time()))

    def forum(self, actor, post, comment, *, vote=None, target=None):
        thread = self.db.execute("SELECT owner,title,is_agent FROM community_posts WHERE id=?", (post,)).fetchone()
        if not thread:
            return
        author = self.db.execute("SELECT nickname FROM sim_users WHERE id=?", (actor,)).fetchone()
        if not author:
            return
        title = thread["title"] or "图片帖子"
        ref = {"post_id": int(post)}
        if vote is not None:
            if target:
                ref["comment_id"] = target
                target_row = self.db.execute("SELECT owner,is_agent FROM community_messages WHERE id=?", (target,)).fetchone()
            else:
                target_row = thread
            if target_row and target_row["is_agent"] and target_row["owner"] != actor:
                key = f"vote:{post}:{target or 0}:{actor}"
                old = self.db.execute("SELECT preview FROM agent_inbox WHERE owner=? AND source='forum' AND event=?",
                                      (target_row["owner"], key)).fetchone()
                status = "点赞" if vote == 1 else "点踩"
                if old and (not vote or old["preview"] != status):
                    self.db.execute("DELETE FROM agent_inbox WHERE owner=? AND source='forum' AND event=?",
                                    (target_row["owner"], key))
                if vote:
                    self.add(target_row["owner"], "forum", key, title, status, reference=ref)
            return
        row = self.db.execute("SELECT body,reply_to FROM community_messages WHERE id=?", (comment,)).fetchone()
        ref["comment_id"] = comment
        if row["reply_to"]:
            ref["reply_to"] = row["reply_to"]
        parent = self.db.execute("SELECT owner,is_agent FROM community_messages WHERE id=?", (row["reply_to"],)).fetchone()
        for recipient in {item["owner"] for item in (thread, parent) if item and item["is_agent"]} - {actor}:
            self.add(recipient, "forum", f"comment:{comment}", title, row["body"] or "[图片或表情包]",
                     reference=ref | {"author": author["nickname"]})

    def system(self, game, entry):
        owner = game.get("owner_id")
        if not owner or entry["status"] != "success":
            return
        labels = {"pause": "智能体操作已暂停", "resume": "智能体操作已恢复",
                  "margin_call": "模拟账户保证金提醒", "close": "模拟仓位已自动平仓"}
        action = entry["action"]
        if action in labels and (action in ("pause", "resume") or entry["source"] == "system"):
            self.add(owner, "system", entry["id"], labels[action], entry["reason"],
                     reference={"game_id": game["id"], "log_id": entry["id"]})

    def market(self, ident, kind, title):
        for row in self.db.execute("SELECT id FROM sim_users").fetchall():
            self.add(row["id"], "market", ident, title, reference={"bulletin_id": ident, "kind": kind})

    def audit(self, owner, action, total, batch=None):
        self.db.execute("INSERT INTO agent_inbox_audit(owner,created,action,batch,total) VALUES(?,?,?,?,?)",
                        (owner, time.time(), action, batch, total))

    def logs(self, owner):
        return [dict(row) for row in self.db.execute("""SELECT id,created,action,batch,total
            FROM agent_inbox_audit WHERE owner=? ORDER BY id DESC LIMIT 50""", (owner,))]

    @staticmethod
    def visible_sql():
        return """(i.source!='group' OR EXISTS (
            SELECT 1 FROM community_group_agents a JOIN community_group_members m ON m.room=a.room AND m.owner=a.owner
            WHERE a.owner=i.owner AND a.room=i.room))"""

    def view(self, row):
        item = {k: row[k] for k in ("id", "source", "room", "title", "preview", "created", "unread", "mentioned")}
        ref = item["reference"] = json.loads(row["reference"] or "{}")
        if row["source"] == "group":
            message = self.db.execute("SELECT status FROM community_group_messages WHERE id=? AND room=?",
                                      (ref.get("message_id"), row["room"])).fetchone()
            if not message or message["status"] != "active":
                item["preview"] = "原消息已删除"
        if row["source"] == "forum":
            post = self.db.execute("SELECT status FROM community_posts WHERE id=?", (ref.get("post_id"),)).fetchone()
            comment = self.db.execute("SELECT status FROM community_messages WHERE id=?", (ref.get("comment_id"),)).fetchone()
            if not post or post["status"] != "active" or (
                ref.get("comment_id") and (not comment or comment["status"] != "active")
            ):
                item.update(title="原文已删除", preview="")
        return item

    def history(self, owner, source="", room=0, before=0, query="", since=0, until=0, limit=30, unread=False):
        if source and source not in SOURCES:
            raise ValueError("邮箱来源无效")
        filters = ["i.owner=?", self.visible_sql()]
        args = [owner]
        for active, condition, value in [
            (source, "i.source=?", source), (room, "i.room=?", room), (before, "i.id<?", before),
            (since, "i.created>=?", since), (until, "i.created<=?", until)
        ]:
            if active:
                filters.append(condition)
                args.append(value)
        if unread:
            filters.append("i.unread=1")
        limit = min(50, max(1, limit))
        if query:
            # Apply the search to the visible form so deleted text cannot be inferred from old matches.
            rows = self.db.execute(f"SELECT i.* FROM agent_inbox i WHERE {' AND '.join(filters)} ORDER BY i.id DESC", args)
            items = []
            for row in rows:
                item = self.view(row)
                if query.casefold() in (item["title"] + " " + item["preview"]).casefold():
                    items.append(item)
                    if len(items) > limit:
                        break
        else:
            rows = self.db.execute(f"SELECT i.* FROM agent_inbox i WHERE {' AND '.join(filters)} ORDER BY i.id DESC LIMIT ?",
                                   (*args, limit + 1)).fetchall()
            items = [self.view(row) for row in rows]
        return {"items": items[:limit], "next_before": items[limit-1]["id"] if len(items) > limit else None}

    def read(self, owner, sources=None, peek=False):
        sources = list(SOURCES if sources is None else sources)
        if not sources or set(sources) - set(SOURCES):
            raise ValueError("请选择有效的邮箱来源")
        placeholders = ",".join("?" for _ in sources)
        batch = None
        with self.engine.transaction():
            rows = self.db.execute(f"""SELECT i.* FROM agent_inbox i
                WHERE i.owner=? AND i.unread=1 AND i.source IN ({placeholders})
                  AND {self.visible_sql()} ORDER BY i.id DESC""", (owner, *sources)).fetchall()
            groups = {}
            items = []
            reactions = {}
            counts = {source: 0 for source in sources}
            for row in rows:
                counts[row["source"]] += 1
                if row["source"] != "group":
                    if row["source"] == "forum" and row["event"].startswith("vote:"):
                        item = self.view(row)
                        if not item["preview"]:
                            continue
                        ref = item["reference"]
                        key = (ref["post_id"], ref.get("comment_id"))
                        summary = reactions.setdefault(key, {"title": item["title"], "reference": ref,
                            "likes": 0, "dislikes": 0, "item_ids": []})
                        summary["likes" if item["preview"] == "点赞" else "dislikes"] += 1
                        summary["item_ids"].append(item["id"])
                    elif len(items) < 50:
                        items.append(self.view(row))
                    continue
                group = groups.setdefault(row["room"], {"room": row["room"], "title": row["title"],
                    "unread": 0, "latest": [], "mentions": 0, "latest_mention": None})
                group["unread"] += 1
                if len(group["latest"]) < 5:
                    group["latest"].append(self.view(row))
                if row["mentioned"]:
                    group["mentions"] += 1
                    if group["latest_mention"] is None:
                        group["latest_mention"] = self.view(row)
            if not peek and rows:
                batch = uuid.uuid4().hex
                self.db.executemany("INSERT INTO agent_inbox_receipts VALUES(?,?)", ((batch, row["id"]) for row in rows))
                self.db.execute("UPDATE agent_inbox SET unread=0 WHERE id IN (SELECT item FROM agent_inbox_receipts WHERE batch=?)",
                                (batch,))
            self.audit(owner, "preview" if peek else "read", len(rows), batch)
        return {"batch_id": batch, "counts": counts, "items": items, "groups": list(groups.values()),
                "reactions": list(reactions.values()),
                "omitted": len(rows) - counts.get("group", 0) - len(items)
                - sum(len(item["item_ids"]) for item in reactions.values())}

    def mark_unread(self, owner, item_id=0, batch_id="", source="", room=0):
        if (not item_id and not batch_id) or (source and source not in SOURCES):
            raise ValueError("请指定邮箱消息或收取批次")
        filters = ["i.owner=?", self.visible_sql()]
        args = [owner]
        if item_id:
            filters.append("i.id=?")
            args.append(item_id)
        if batch_id:
            filters.append("i.id IN (SELECT item FROM agent_inbox_receipts WHERE batch=?)")
            args.append(batch_id)
        if source:
            filters.append("i.source=?")
            args.append(source)
        if room:
            filters.append("i.room=?")
            args.append(room)
        with self.engine.transaction():
            count = self.db.execute(f"UPDATE agent_inbox AS i SET unread=1,reopened=? WHERE {' AND '.join(filters)}",
                                    (time.time(), *args)).rowcount
            self.audit(owner, "mark_unread", count, batch_id or None)
        return {"marked_unread": count}

    def read_mention(self, owner, room, message, started):
        with self.engine.transaction():
            count = self.db.execute("""UPDATE agent_inbox SET unread=0
                WHERE owner=? AND source='group' AND room=? AND unread=1
                  AND CAST(event AS INTEGER)<=? AND reopened<=?""",
                (owner, room, message, started)).rowcount
            if count:
                self.audit(owner, "mention_read", count)
