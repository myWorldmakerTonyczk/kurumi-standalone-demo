import json
import math
import time

from src.core.engine import dump


def build(app, owner, gid, message_ids):
    async def write_memory(text: str, subject: str, tags: list[str], strength: float) -> str:
        """保存值得长期记住的明确信息。subject 用 self 或已有的 user:/agent: 人物 ID；tags 填 1～5 个标签，strength 为大于 0 且小于 1 的初始强度。持续规律交给后台整理。"""
        try:
            if not app.profile(owner).get("chat_memory", {}).get("enabled", True):
                raise ValueError("长期记忆已关闭")
            text = text.strip()
            if not 1 <= len(text) <= 300:
                raise ValueError("记忆内容须为 1～300 字")
            role, _, person = subject.partition(":")
            if subject != "self" and (role not in ("user", "agent") or not person or not app.db.execute(
                "SELECT 1 FROM sim_users WHERE id=?", (person,)
            ).fetchone()):
                raise ValueError("人物须为 self 或有效的 user:/agent: ID")
            if not 1 <= len(tags) <= 5 or any(not 1 <= len(tag.strip()) <= 24 for tag in tags):
                raise ValueError("请填写 1～5 个标签，每个 1～24 字")
            if type(strength) not in (float, int) or not math.isfinite(strength) or not 0 < strength < 1:
                raise ValueError("强度须大于 0 且小于 1")
            now = time.time()
            with app.engine.transaction():
                row = app.db.execute(
                    "SELECT id,tags,initial FROM agent_memory WHERE owner=? AND subject=? AND text=?",
                    (owner, subject, text),
                ).fetchone()
                if row:
                    ident = row["id"]
                    combined = list(dict.fromkeys(json.loads(row["tags"]) + [tag.strip() for tag in tags]))[:6]
                    app.db.execute("UPDATE agent_memory SET tags=?,initial=?,updated=? WHERE id=?",
                                   (json.dumps(combined, ensure_ascii=False), max(strength, row["initial"]), now, ident))
                else:
                    total = app.db.execute("SELECT count(*) FROM agent_memory WHERE owner=?", (owner,)).fetchone()[0]
                    if total >= 200:
                        raise ValueError("最多保存 200 条记忆")
                    ident = app.db.execute("""INSERT INTO agent_memory(owner,subject,text,updated,created,kind,tags,initial)
                        VALUES(?,?,?,?,?,'knowledge',?,?)""",
                        (owner, subject, text, now, now, json.dumps(list(dict.fromkeys(tag.strip() for tag in tags)),
                         ensure_ascii=False), strength)).lastrowid
                app.db.execute("""INSERT OR IGNORE INTO agent_memory_sources(memory,stream,messages,created)
                    VALUES(?,?,?,?)""", (ident, f"private:{gid}", json.dumps(message_ids), now))
            return dump({"ok": True, "id": ident, "merged": row is not None})
        except ValueError as error:
            return dump({"ok": False, "error": str(error)})

    return write_memory
