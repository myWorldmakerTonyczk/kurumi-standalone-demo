"""Serialize platform tasks per account and retain compact activity records."""
import asyncio
import heapq
import itertools
import time
from contextvars import ContextVar

_active_task = ContextVar("active_agent_task", default=None)

PRIORITY = {"private": 0, "group_mention": 1, "manual_watch": 2, "group": 3,
            "private_followup": 3, "group_followup": 3, "watch": 4, "diary": 5,
            "proactive": 6, "patrol": 7}
LABELS = {"private": "私聊", "group_mention": "群聊提及", "group": "群聊",
          "watch": "定时看盘", "manual_watch": "手动看盘", "diary": "日记",
          "proactive": "主动私聊", "patrol": "社区巡视", "private_followup": "私聊跟进",
          "group_followup": "群聊跟进", "trade_review": "交易复盘", "group_summary": "群聊摘要"}


class AgentTimeline:
    def __init__(self, db, preferences):
        self.db, self.preferences = db, preferences
        self.queues, self.workers, self.dedup = {}, {}, {}
        self.order = itertools.count()
        self.db.executescript("""
        CREATE TABLE IF NOT EXISTS agent_tasks(
            id INTEGER PRIMARY KEY AUTOINCREMENT, owner TEXT NOT NULL REFERENCES sim_users(id) ON DELETE CASCADE,
            kind TEXT NOT NULL, state TEXT NOT NULL, created REAL NOT NULL,
            started REAL, ended REAL, outcome TEXT NOT NULL DEFAULT '');
        CREATE INDEX IF NOT EXISTS agent_task_owner ON agent_tasks(owner,id);
        CREATE TABLE IF NOT EXISTS agent_task_debug(
            id INTEGER PRIMARY KEY AUTOINCREMENT, task INTEGER NOT NULL REFERENCES agent_tasks(id) ON DELETE CASCADE,
            state TEXT NOT NULL, created REAL NOT NULL, error_type TEXT NOT NULL DEFAULT '');
        UPDATE agent_tasks SET state='interrupted',ended=unixepoch(),outcome='服务重启，任务中断'
          WHERE state IN ('queued','running');
        """)

    def debug(self, owner, ident, state, error=""):
        if self.preferences(owner).get("timeline_debug", False):
            self.db.execute("INSERT INTO agent_task_debug(task,state,created,error_type) VALUES(?,?,?,?)",
                            (ident, state, time.time(), error))

    def enqueue(self, owner, kind, execute, *, key=None, outcome=None):
        if key is not None and (owner, key) in self.dedup:
            return self.dedup[owner, key]
        ident = self.db.execute("INSERT INTO agent_tasks(owner,kind,state,created) VALUES(?,?,'queued',?)",
                                (owner, kind, time.time())).lastrowid
        self.debug(owner, ident, "queued")
        future = asyncio.get_running_loop().create_future()
        future.add_done_callback(lambda done: None if done.cancelled() else done.exception())
        if key is not None:
            self.dedup[owner, key] = ident, future
        queue = self.queues.setdefault(owner, [])
        heapq.heappush(queue, (PRIORITY.get(kind, 5), next(self.order), ident, execute, future, key, outcome))
        if owner not in self.workers:
            self.workers[owner] = asyncio.create_task(self.drain(owner))
        return ident, future

    async def run(self, owner, kind, execute, **options):
        _, future = self.enqueue(owner, kind, execute, **options)
        return await asyncio.shield(future)

    def in_task(self, owner):
        current = _active_task.get()
        return current is not None and current[0] == owner

    async def drain(self, owner):
        queue = self.queues[owner]
        try:
            while queue:
                _, _, ident, execute, future, key, summarize = heapq.heappop(queue)
                state, outcome, error = "completed", "已完成", ""
                self.db.execute("UPDATE agent_tasks SET state='running',started=? WHERE id=?", (time.time(), ident))
                self.debug(owner, ident, "running")
                token = _active_task.set((owner, ident))
                try:
                    result = await execute()
                    if summarize:
                        outcome = summarize(result)
                    if outcome == "已跳过":
                        state = "skipped"
                    future.set_result(result)
                except asyncio.CancelledError:
                    state, outcome = "interrupted", "服务停止，任务中断"
                    future.cancel()
                    raise
                except Exception as exc:
                    state, outcome, error = "failed", "执行失败", type(exc).__name__
                    future.set_exception(exc)
                finally:
                    _active_task.reset(token)
                    self.db.execute("UPDATE agent_tasks SET state=?,ended=?,outcome=? WHERE id=?",
                                    (state, time.time(), outcome, ident))
                    self.debug(owner, ident, state, error)
                    if key is not None:
                        self.dedup.pop((owner, key), None)
        finally:
            self.workers.pop(owner, None)
            self.queues.pop(owner, None)
            for _, _, ident, _, future, key, _ in queue:
                future.cancel()
                self.db.execute("UPDATE agent_tasks SET state='interrupted',ended=?,outcome='服务停止' WHERE id=?",
                                (time.time(), ident))
                if key is not None:
                    self.dedup.pop((owner, key), None)

    def history(self, owner, limit=30):
        rows = self.db.execute("""SELECT id,kind,state,created,started,ended,outcome FROM agent_tasks
            WHERE owner=? ORDER BY id DESC LIMIT ?""", (owner, limit))
        return [dict(row) | {"label": LABELS.get(row["kind"], row["kind"])} for row in rows]

    def context(self, owner):
        rows = self.db.execute("""SELECT kind,state,ended,outcome FROM agent_tasks
            WHERE owner=? AND ended IS NOT NULL ORDER BY ended DESC,id DESC LIMIT 8""", (owner,))
        recent = [{"task": LABELS.get(row["kind"], row["kind"]), "result": row["outcome"], "ended": row["ended"]}
                  for row in reversed(rows.fetchall())]
        active = self.db.execute("SELECT kind FROM agent_tasks WHERE owner=? AND state='running' ORDER BY id DESC LIMIT 1",
                                 (owner,)).fetchone()
        return {"current": LABELS.get(active["kind"], active["kind"]) if active else None, "recent": recent}

    def logs(self, owner):
        return [dict(row) for row in self.db.execute("""SELECT d.task,d.created,d.state,d.error_type
            FROM agent_task_debug d JOIN agent_tasks t ON t.id=d.task WHERE t.owner=?
            ORDER BY d.id DESC LIMIT 100""", (owner,))]

    async def close(self):
        workers = list(self.workers.values())
        for worker in workers:
            worker.cancel()
        await asyncio.gather(*workers, return_exceptions=True)
