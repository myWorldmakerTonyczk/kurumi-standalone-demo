"""Account-scoped long-term chat memory."""
import asyncio
import json
import logging
import re
import time

from aiohttp import web
from langchain_core.messages import HumanMessage, SystemMessage

from .workflow import workflow
from .model_registry import model_client

log = logging.getLogger(__name__)
MEMORY_DAYS = 90


def strength(stamp, now):
    return max(0.0, min(1.0, 1 - (now - stamp) / (MEMORY_DAYS * 86400)))


def terms(text):
    result = set()
    for token in re.findall(r"[a-z0-9]+|[\u3400-\u9fff]+", text.casefold()):
        if token.isascii():
            result.add(token)
        else:
            result.update(token[i:i + 2] for i in range(len(token) - 1))
    return result


class Memory:
    def __init__(self, db):
        self.db = db
        self.tasks = {}
        db.execute("""CREATE TABLE IF NOT EXISTS agent_memory(
            id INTEGER PRIMARY KEY,
            owner TEXT NOT NULL REFERENCES sim_users(id) ON DELETE CASCADE,
            text TEXT NOT NULL,
            subject TEXT NOT NULL DEFAULT 'self',
            updated REAL NOT NULL,
            UNIQUE(owner,subject,text)
        )""")
        if "subject" not in {row[1] for row in db.execute("PRAGMA table_info(agent_memory)")}:
            with db:
                db.execute("SAVEPOINT migrate_agent_memory")
                try:
                    db.execute("ALTER TABLE agent_memory RENAME TO agent_memory_legacy")
                    db.execute("""CREATE TABLE agent_memory(
                        id INTEGER PRIMARY KEY, owner TEXT NOT NULL REFERENCES sim_users(id) ON DELETE CASCADE,
                        text TEXT NOT NULL, subject TEXT NOT NULL DEFAULT 'self', updated REAL NOT NULL,
                        UNIQUE(owner,subject,text))""")
                    db.execute("""INSERT INTO agent_memory(id,owner,text,updated)
                        SELECT id,owner,text,? FROM agent_memory_legacy""", (time.time(),))
                    db.execute("DROP TABLE agent_memory_legacy")
                except BaseException:
                    db.execute("ROLLBACK TO migrate_agent_memory")
                    raise
                finally:
                    db.execute("RELEASE migrate_agent_memory")

    def list(self, owner):
        now = time.time()
        return [dict(row) | {"strength": strength(row["updated"], now)} for row in self.db.execute(
            "SELECT id,text,subject,updated FROM agent_memory WHERE owner=? ORDER BY id DESC LIMIT 200", (owner,)
        )]

    def recall(self, owner, message, subject="self"):
        query = terms(message)
        ranked = [(len(query & terms(row["text"])) * row["strength"], row["text"])
                  for row in self.list(owner) if row["subject"] == subject and row["strength"] > 0]
        ranked.sort(key=lambda item: item[0], reverse=True)
        return [value for score, value in ranked[:5] if score]

    def cancel(self, owner):
        task = self.tasks.get(owner)
        if task:
            task.cancel()

    async def close(self):
        tasks = list(self.tasks.values())
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    def update(self, owner, body, subjects):
        action = body.get("action")
        if action not in ("add", "edit", "delete"):
            raise ValueError("未知记忆操作")
        subject = body.get("subject", "self")
        if action != "delete" and (not isinstance(subject, str) or subject not in subjects):
            raise ValueError("请选择记忆对象")
        text = body.get("text")
        if action != "delete" and (not isinstance(text, str) or not 1 <= len(text.strip()) <= 300):
            raise ValueError("记忆内容须为 1～300 字")
        if action == "add":
            if len(self.list(owner)) >= 200:
                raise ValueError("最多保存 200 条记忆")
            self.db.execute("INSERT OR IGNORE INTO agent_memory(owner,subject,text,updated) VALUES(?,?,?,?)",
                            (owner, subject, text.strip(), time.time()))
            return
        entry = body.get("id")
        if type(entry) is not int or not 0 < entry < 2**63:
            raise ValueError("记忆编号无效")
        if action == "edit":
            if self.db.execute("SELECT 1 FROM agent_memory WHERE owner=? AND subject=? AND text=? AND id<>?",
                               (owner, subject, text.strip(), entry)).fetchone():
                raise ValueError("已有相同记忆")
            result = self.db.execute("UPDATE agent_memory SET text=?,subject=?,updated=? WHERE owner=? AND id=?",
                                     (text.strip(), subject, time.time(), owner, entry))
        else:
            result = self.db.execute("DELETE FROM agent_memory WHERE owner=? AND id=?", (owner, entry))
        if not result.rowcount:
            raise web.HTTPNotFound(reason="找不到该记忆")
        self.cancel(owner)

    def schedule(self, owner, model, validate_url, message, reply):
        if not model.get("key") or len(message) < 5 or owner in self.tasks or len(self.tasks) >= 8:
            return
        task = asyncio.create_task(self.extract(owner, model, validate_url, message, reply))
        self.tasks[owner] = task
        task.add_done_callback(lambda _: self.tasks.pop(owner, None))

    async def extract(self, owner, model, validate_url, message, reply):
        try:
            known = self.recall(owner, message)
            result = await model_client(model, validate_url).ainvoke([
                SystemMessage(content=workflow("memory")),
                HumanMessage(content=json.dumps(dict(user=message, reply=reply[:600], known=known), ensure_ascii=False)),
            ])
            raw = result.text.strip()
            if raw.startswith("```") and raw.endswith("```"):
                raw = raw.partition("\n")[2][:-3]
            items = json.loads(raw)
            if not isinstance(items, list):
                return
            for item in items[:3]:
                if isinstance(item, str) and 1 <= len(item.strip()) <= 300:
                    if len(self.list(owner)) >= 200:
                        break
                    self.db.execute("INSERT OR IGNORE INTO agent_memory(owner,text,updated) VALUES(?,?,?)",
                                    (owner, item.strip(), time.time()))
        except Exception as exc:
            log.warning("Chat memory extraction failed: %s", type(exc).__name__)
