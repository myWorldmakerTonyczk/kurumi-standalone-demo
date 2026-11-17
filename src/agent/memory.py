"""Account-scoped long-term chat memory."""
import asyncio
import json
import logging
import math
from collections import Counter
import re
import time

from aiohttp import web
from langchain_core.messages import HumanMessage, SystemMessage

from .workflow import workflow
from .timeline import task_trace
from .memory_profiles import MemoryProfiles
from .model_registry import model_client

log = logging.getLogger(__name__)
MEMORY_DAYS = 90
BATCH_THRESHOLD = 5
FORUM_COMMENT_THRESHOLD = 6
CHARACTER_MEMORIES = (
    ("ikuo", "福贺郁夫", ("爸爸", "父亲", "家人"),
     "福贺郁夫是我爸。我读初中时他曾在外地工作。上大学后我瞒着他拿了家里柜子里的三百万日元做 FX，想赚到钱再还回去。他发现后哭着求我停手。后来我和他坐下来谈过。"),
    ("kozue", "福贺梢", ("妈妈", "母亲", "家人"),
     "福贺梢是我妈妈。我初三时得知她做 FX 亏掉家里的两千万日元。妈妈去世后，我一直放不下这件事，上大学后想靠自己把这笔钱赚回来。这段往事平时留在心里。"),
    ("mochiko", "小金萌智子", ("萌智子", "同校", "投资朋友"),
     "小金萌智子和我同校，是我的投资朋友。她懂股票和外汇，说话直，也喜欢可爱的东西。她推荐过妈妈做过的澳元兑日元。我们会一起讨论，也会暗暗较劲。我低谷时，她拉过我一把。"),
    ("mebuki", "山师芽吹", ("芽吹", "朋友", "投资前后辈"),
     "山师芽吹是萌智子的朋友。她受萌智子影响开始做 FX，叫我投资前辈，刚入门时常来找我商量。她开朗，有自己的主意，也曾兴冲冲提议大家来一场投资比赛。"),
    ("yasuko", "高根安子", ("高根やす子", "あふぃちゃん", "投资博客"),
     "高根安子（日文名高根やす子）是芽吹的朋友，以「あふぃちゃん」为名写投资博客。她做 FX 吃过亏，嘴上不客气，朋友真要冒险时又放不下。我和她的交集主要来自芽吹和几个人一起参与的事。"),
)
QUIET_SECONDS = 12
MAX_BATCH = 48


def strength(stamp, now, initial=1.0):
    return max(0.0, min(1.0, initial - (now - stamp) / (MEMORY_DAYS * 86400)))


def ranked_tags(rows):
    strongest = {}
    for row in rows:
        current = row["strength"]
        if current > 0:
            for tag in row["tags"]:
                strongest[tag] = max(current, strongest.get(tag, 0))
    ordered = sorted(strongest.items(), key=lambda item: (-item[1], item[0]))
    return [{"tag": tag, "strength": strength} for tag, strength in ordered[:100]]


# Split Chinese text into overlapping pairs and keep Latin words intact.
_RECALL_FILLERS = (
    "请问", "你好", "谢谢", "好的", "你还记得", "还记得", "记不记得",
    "你知道", "我想问", "能不能", "告诉我", "帮我", "上次", "之前",
    "那个", "这个", "刚才", "怎么说", "怎么样",
)


def terms(text):
    for token in re.findall(r"[a-z0-9]+|[\u3400-\u9fff]+", text.casefold()):
        if token.isascii():
            if len(token) > 1 and token not in {"the", "is", "it", "you", "me", "to", "do"}:
                yield token
        else:
            yield from (token[i:i + 2] for i in range(len(token) - 1))


def query_terms(text):
    for filler in _RECALL_FILLERS:
        text = text.replace(filler, " ")
    return set(terms(text))


class Memory:
    def __init__(self, db, call_logs=None, timeline=None):
        self.db = db
        self.call_logs = call_logs
        self.timeline = timeline
        self.profiles = MemoryProfiles(db, call_logs)
        self.tasks = {}
        self.last_seen = {}
        self.results = {}
        self.slots = asyncio.Semaphore(8)
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

        db.execute("""CREATE TABLE IF NOT EXISTS agent_memory_sources(
            memory INTEGER NOT NULL REFERENCES agent_memory(id) ON DELETE CASCADE,
            stream TEXT NOT NULL, messages TEXT NOT NULL, created REAL NOT NULL,
            PRIMARY KEY(memory,stream,messages))""")
        columns = {row[1] for row in db.execute("PRAGMA table_info(agent_memory)")}
        for name, definition in (
            ("kind", "TEXT NOT NULL DEFAULT 'knowledge'"),
            ("tags", "TEXT NOT NULL DEFAULT '[]'"),
            ("initial", "REAL NOT NULL DEFAULT 1"),
            ("created", "REAL NOT NULL DEFAULT 0"),
        ):
            if name not in columns:
                db.execute(f"ALTER TABLE agent_memory ADD COLUMN {name} {definition}")
        db.execute("""CREATE TABLE IF NOT EXISTS agent_memory_events(
            seq INTEGER PRIMARY KEY,
            owner TEXT NOT NULL REFERENCES sim_users(id) ON DELETE CASCADE,
            stream TEXT NOT NULL, event TEXT NOT NULL, subject TEXT NOT NULL,
            text TEXT NOT NULL, reply TEXT NOT NULL, done INTEGER NOT NULL DEFAULT 0,
            UNIQUE(owner,stream,event)
        )""")
        db.execute("""CREATE INDEX IF NOT EXISTS agent_memory_events_pending
            ON agent_memory_events(owner,stream,done,seq)""")

        db.execute("""CREATE TABLE IF NOT EXISTS agent_character_memory(
            id INTEGER PRIMARY KEY, subject TEXT NOT NULL UNIQUE, name TEXT NOT NULL,
            text TEXT NOT NULL, tags TEXT NOT NULL, created REAL NOT NULL
        )""")
        db.executemany("""INSERT OR IGNORE INTO agent_character_memory(id,subject,name,text,tags,created)
            VALUES(?,?,?,?,?,?)""", [
            (-index, "character:" + key, name, text, json.dumps([name, *tags], ensure_ascii=False), time.time())
            for index, (key, name, tags, text) in enumerate(CHARACTER_MEMORIES, 1)
        ])

    def character_subjects(self):
        return dict(self.db.execute("SELECT subject,name FROM agent_character_memory ORDER BY id DESC").fetchall())

    def character_entries(self):
        return [
            dict(row) | {"tags": json.loads(row["tags"]), "updated": row["created"],
                         "kind": "knowledge", "initial": 1.0, "strength": 1.0, "permanent": True}
            for row in self.db.execute("SELECT * FROM agent_character_memory ORDER BY id DESC")
        ]

    def user_entries(self, owner):
        now = time.time()
        return [dict(row) | {"strength": strength(row["updated"], now, row["initial"]),
                             "tags": json.loads(row["tags"]), "created": row["created"] or row["updated"]} for row in self.db.execute(
            "SELECT id,text,subject,updated,kind,tags,initial,created FROM agent_memory WHERE owner=? ORDER BY id DESC LIMIT 200", (owner,)
        )]

    def list(self, owner):
        return self.character_entries() + self.user_entries(owner)

    def recall(self, owner, message, subject="self", *, recent=()):
        query = query_terms(message[:2000])
        # Only borrow the preceding topic for a short, referential follow-up.
        if len(query) <= 1 and message.lstrip().startswith(("那", "这", "它", "继续")):
            for previous in recent:
                query.update(query_terms(previous[:400]))
        if not query:
            return []

        rows = self.db.execute(
            """SELECT text,tags,updated,created,initial FROM agent_memory
               WHERE owner=? AND subject=? AND initial > 0 AND updated > ?""",
            (owner, subject, time.time() - MEMORY_DAYS * 86400),
        )
        now = time.time()
        documents = []
        frequency = Counter()
        candidates = [
            dict(row) | {"tags": json.loads(row["tags"]), "strength": strength(row["updated"], now, row["initial"])}
            for row in rows
        ]
        candidates.extend(self.character_entries())
        for row in candidates:
            active = row["strength"]
            if active <= 0:
                continue
            counts = Counter(terms(row["text"]))
            tags = Counter(term for tag in row["tags"] for term in terms(tag))
            matches = query & (counts.keys() | tags.keys())
            frequency.update(matches)
            documents.append((row, active, counts, tags, matches, sum(counts.values())))
        if not frequency:
            return []

        size = len(documents)
        average = max(1, sum(doc[-1] for doc in documents) / size)
        idf = {term: math.log1p((size - count + .5) / (count + .5))
               for term, count in frequency.items()}
        ranked = []
        for row, active, counts, tags, matches, length in documents:
            score = 0.0
            for term in sorted(matches & idf.keys()):
                weight = counts[term] + 2 * bool(tags[term])
                score += idf[term] * 2.2 * weight / (weight + 1.2 * (.25 + .75 * length / average))
            if score > 0:
                ranked.append((score, active, row["updated"], row))
        ranked.sort(key=lambda item: item[:3], reverse=True)
        result, seen = [], set()
        for _, _, _, row in ranked:
            text = row["text"][:300]
            normalized = re.sub(r"[\W_]+", "", text.casefold())
            if normalized in seen:
                continue
            seen.add(normalized)
            result.append({"text": text, "created": int(row["created"] or row["updated"])})
            if len(result) == 5:
                break
        return result

    def cancel(self, owner):
        for key, task in list(self.tasks.items()):
            if key[0] == owner:
                self.tasks.pop(key)
                self.last_seen.pop(key, None)
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
        if action != "delete" and subject.startswith("character:"):
            raise ValueError("原作人物是系统记忆对象")
        text = body.get("text")
        if action != "delete" and (not isinstance(text, str) or not 1 <= len(text.strip()) <= 300):
            raise ValueError("记忆内容须为 1～300 字")
        if action == "add":
            if len(self.user_entries(owner)) >= 200:
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

    def include_private_history(self, owner, gid):
        stream = f"private:{gid}"
        self.db.execute("""INSERT OR IGNORE INTO agent_memory_events
            (owner,stream,event,subject,text,reply)
            SELECT ?,?,CAST(a.id AS TEXT),'self',substr(u.content,1,3000),substr(a.content,1,1500)
            FROM demo_chat u JOIN demo_chat a ON a.id=(
                SELECT next.id FROM demo_chat next WHERE next.game=u.game AND next.id>u.id
                ORDER BY next.id LIMIT 1
            )
            WHERE u.game=? AND u.role='user' AND a.role='assistant' AND trim(u.content)<>''
              AND NOT EXISTS (SELECT 1 FROM agent_memory_events e
                  WHERE e.owner=? AND e.stream=? AND e.event='user:' || u.id)
            ORDER BY u.id""", (owner, stream, gid, owner, stream))

    def progress(self, owner):
        pending, done = self.db.execute("""SELECT count(*)-coalesce(sum(done),0),coalesce(sum(done),0)
            FROM agent_memory_events WHERE owner=?""", (owner,)).fetchone()
        return dict(pending=pending, done=done, active=any(key[0] == owner for key in self.tasks),
                    failed=any(key[0] == owner and result is not None for key, result in self.results.items()),
                    threshold=BATCH_THRESHOLD, forum_comment_threshold=FORUM_COMMENT_THRESHOLD)

    def schedule(self, owner, model, validate_url, message, reply="", *, subject="self", stream, event):
        if not model.get("key") or not model.get("model") or not message.strip():
            return
        cursor = self.db.execute("""INSERT OR IGNORE INTO agent_memory_events(owner,stream,event,subject,text,reply)
            VALUES(?,?,?,?,?,?)""", (owner, stream, str(event), subject, message[:3000], reply[:1500]))
        key = (owner, stream)
        if cursor.rowcount:
            self.last_seen[key] = asyncio.get_running_loop().time()
        self.start(owner, stream, model, validate_url)

    def start(self, owner, stream, model, validate_url):
        key = (owner, stream)
        if key in self.tasks or time.monotonic() - self.results.get(key, float("-inf")) < 60:
            return
        if self.ready(stream, self.batch(key)) or self.profiles.pending(owner, stream):
            self.tasks[key] = asyncio.create_task(self.drain(key, model, validate_url))
        else:
            self.last_seen.pop(key, None)

    async def trace_batch(self, key, kind, operation):
        if self.timeline:
            return await self.timeline.trace_background(key[0], kind, {"stream": key[1]}, operation)
        return await operation()

    async def drain(self, key, model, validate_url):
        loop = asyncio.get_running_loop()
        try:
            while True:
                rows = self.batch(key)
                if not self.ready(key[1], rows):
                    async with self.slots:
                        if self.profiles.pending(*key):
                            await self.trace_batch(key, "memory_portrait",
                                                   lambda: self.profiles.consolidate(*key, model, validate_url))
                    self.results.pop(key, None)
                    if not self.ready(key[1], self.batch(key)):
                        return
                    continue
                remaining = QUIET_SECONDS - (loop.time() - self.last_seen.get(key, 0))
                if remaining > 0 and len(rows) < MAX_BATCH:
                    await asyncio.sleep(remaining)
                    continue
                async with self.slots:
                    await self.trace_batch(key, "memory_generation",
                                           lambda: self.extract(key, model, validate_url, self.batch(key)))
                    self.results.pop(key, None)
        except Exception as exc:
            self.results[key] = time.monotonic()
            log.warning("Chat memory batch failed: %s", type(exc).__name__)
        finally:
            if self.tasks.get(key) is asyncio.current_task():
                self.tasks.pop(key, None)
                self.last_seen.pop(key, None)

    @staticmethod
    def ready(stream, rows):
        if stream.startswith("forum:"):
            return sum(row["event"].startswith("comment:") for row in rows) >= FORUM_COMMENT_THRESHOLD
        return len(rows) >= BATCH_THRESHOLD

    def batch(self, key):
        return self.db.execute("""SELECT seq,event,subject,text,reply FROM agent_memory_events
            WHERE owner=? AND stream=? AND done=0 ORDER BY CASE WHEN event LIKE 'user:%' THEN CAST(substr(event,6) AS INTEGER)
                WHEN stream LIKE 'private:%' THEN CAST(event AS INTEGER) ELSE seq END LIMIT ?""", (*key, MAX_BATCH)).fetchall()

    async def extract(self, key, model, validate_url, rows):
        owner, stream = key
        subjects = {row["subject"] for row in rows}
        memories = self.list(owner)
        known = [row for row in memories if row["subject"] in subjects]
        audit = self.call_logs.callback(owner, "memory") if self.call_logs else None
        result = await model_client(model, validate_url, audit=audit).ainvoke([
            SystemMessage(content=workflow("memory")),
            HumanMessage(content=json.dumps({
                "source": stream.split(":", 1)[0], "events": [{field: row[field] for field in ("seq", "subject", "text", "reply")} for row in rows],
                "available_tags": [item["tag"] for item in ranked_tags(memories)],
                "known": [{k: row[k] for k in ("id", "subject", "text")} for row in known],
            }, ensure_ascii=False)),
        ])
        raw = result.text.strip()
        if raw.startswith("```") and raw.endswith("```"):
            raw = raw.partition("\n")[2][:-3]
        items = json.loads(raw)
        if not isinstance(items, list):
            raise ValueError("Expected memory array")
        evidence = {row["seq"]: row["subject"] for row in rows}
        existing = {row["id"]: row for row in known}
        now = time.time()
        trace = task_trace(owner)
        saved = []
        self.db.execute("SAVEPOINT save_memory_batch")
        try:
            seen = set()
            for item in items[:12]:
                if not isinstance(item, dict):
                    continue
                subject = evidence.get(item.get("event")) if type(item.get("event")) is int else None
                text = item.get("text")
                if not subject or not isinstance(text, str) or not 1 <= len(text.strip()) <= 300:
                    continue
                text = text.strip()
                match = existing.get(item.get("match")) if type(item.get("match")) is int else None
                if not match or match["subject"] != subject:
                    match = next((row for row in known if row["subject"] == subject and row["text"] == text), None)
                token = (subject, match["id"] if match else text)
                if token in seen:
                    continue
                seen.add(token)
                initial = 0.95 if item.get("important") is True else 0.45
                kind = item.get("kind")
                if kind not in ("knowledge", "event", "emotion"):
                    kind = "knowledge"
                tags = item.get("tags")
                tags = list(dict.fromkeys(tag.strip()[:24] for tag in tags
                            if isinstance(tag, str) and tag.strip()))[:6] if isinstance(tags, list) else []
                if match:
                    combined = list(dict.fromkeys(match["tags"] + tags))[:6]
                    strength_after = min(1, max(initial, match["strength"] + 0.2))
                    cursor = self.db.execute("UPDATE agent_memory SET updated=?,initial=?,tags=? WHERE id=? AND owner=?",
                                             (now, strength_after, json.dumps(combined), match["id"], owner))
                    if trace and cursor.rowcount:
                        saved.append({"action": "reinforced", "id": match["id"], "subject": subject,
                                      "text": match["text"], "tags": combined, "strength": strength_after,
                                      "event": item["event"]})
                elif len(self.user_entries(owner)) < 200:
                    cursor = self.db.execute("""INSERT OR IGNORE INTO agent_memory(owner,subject,text,updated,created,kind,tags,initial)
                        VALUES(?,?,?,?,?,?,?,?)""", (owner, subject, text, now, now, kind, json.dumps(tags), initial))
                    if trace and cursor.rowcount:
                        saved.append({"action": "created", "id": cursor.lastrowid, "subject": subject,
                                      "text": text, "kind": kind, "tags": tags, "strength": initial,
                                      "event": item["event"]})
            self.db.executemany("UPDATE agent_memory_events SET done=1,text='',reply='' WHERE seq=?",
                                [(row["seq"],) for row in rows])
        except BaseException:
            self.db.execute("ROLLBACK TO save_memory_batch")
            raise
        finally:
            self.db.execute("RELEASE save_memory_batch")
        if trace:
            trace.event("memory_write", "memory", {"candidates": len(items), "processed_events": len(rows), "saved": saved})
