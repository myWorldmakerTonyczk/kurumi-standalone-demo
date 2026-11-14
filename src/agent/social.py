"""Background group replies and daily agent patrols."""
import asyncio
import logging
import random
import re
from datetime import datetime
from zoneinfo import ZoneInfo

from langchain_core.messages import HumanMessage, SystemMessage

from src.core.engine import dump
from .model_registry import model_client, selected
from .workflow import workflow

log = logging.getLogger(__name__)
ZONE = ZoneInfo("Asia/Shanghai")


def social_config(body):
    probability = body.get("probability", 0)
    mode = body.get("patrol", "off")
    start, end = body.get("start", "10:00"), body.get("end", "22:00")
    if type(probability) not in (int, float) or not 0 <= probability <= 100:
        raise ValueError("主动发言概率须在 0～100% 之间")
    if mode not in ("off", "fixed", "random") or not all(
        isinstance(value, str) and re.fullmatch(r"(?:[01][0-9]|2[0-3]):[0-5][0-9]", value)
        for value in (start, end)
    ) or (mode == "random" and start >= end):
        raise ValueError("请选择有效的巡视时间，随机时段须在同一天")
    return dict(probability=probability, patrol=mode, start=start, end=end)


class Social:
    def __init__(self, app):
        self.app = app
        self.db = app.db
        self.tasks = set()
        self.slots = asyncio.Semaphore(4)
        self.db.execute("""CREATE TABLE IF NOT EXISTS agent_patrol_runs(
            owner TEXT PRIMARY KEY REFERENCES sim_users(id) ON DELETE CASCADE,
            day TEXT NOT NULL, minute INTEGER NOT NULL, ran INTEGER NOT NULL)""")

    def identity(self, owner):
        return self.app.profile(owner).get("agent_name") or "久留美"

    def joined(self, room, owner):
        return bool(self.db.execute("""SELECT 1 FROM community_group_agents a
            JOIN community_group_members m ON m.room=a.room AND m.owner=a.owner
            WHERE a.room=? AND a.owner=?""", (room, owner)).fetchone())

    def members(self, room):
        return [self.identity(row[0]) for row in self.db.execute(
            "SELECT owner FROM community_group_agents WHERE room=?", (room,))]

    def decorate(self, entries):
        for entry in entries:
            owner = entry.pop("owner")
            profile = self.app.profile(owner)
            if not entry["is_agent"]:
                duplicate = self.db.execute("""SELECT count(*) FROM sim_users
                    WHERE nickname=? COLLATE NOCASE AND id<?""", (entry["nickname"], owner)).fetchone()[0]
                if duplicate:
                    entry["nickname"] += f"（{duplicate + 1}）"
            if entry["is_agent"]:
                entry["nickname"] = profile.get("agent_name") or "久留美"
            entry["avatar"] = profile.get("agent_avatar", "avatar.png") if entry["is_agent"] else profile.get("avatar", "user-avatar.svg")
        return entries

    def submit(self, job, *args):
        if len(self.tasks) >= 64:
            return False
        task = asyncio.create_task(job(*args))
        self.tasks.add(task)
        task.add_done_callback(self.tasks.discard)
        return True

    async def close(self):
        tasks = list(self.tasks)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    def on_message(self, room, message_id):
        message = self.db.execute("SELECT body FROM community_group_messages WHERE id=? AND room=? AND NOT is_agent",
                                  (message_id, room)).fetchone()
        if not message:
            return
        for row in self.db.execute("SELECT owner FROM community_group_agents WHERE room=?", (room,)):
            owner = row[0]
            profile = self.app.profile(owner)
            mention = re.search(r"@" + re.escape(self.identity(owner)) + r"(?=$|[\s，。！？、,.!?：:])", message[0])
            if mention or random.random() * 100 < profile.get("social", {}).get("probability", 0):
                self.submit(self.reply, owner, room, message_id)

    async def reply(self, owner, room, message_id):
        async with self.slots:
            try:
                if not self.joined(room, owner):
                    return
                profile = self.app.profile(owner)
                config = selected(profile)
                if not config.get("key") or self.app.external.settings(owner)["mode"] != "platform":
                    return
                rows = self.db.execute("""SELECT m.body,m.owner,m.is_agent,u.nickname FROM community_group_messages m
                    JOIN sim_users u ON u.id=m.owner WHERE m.room=? AND m.id<=? ORDER BY m.id DESC LIMIT 16""",
                    (room, message_id)).fetchall()
                if not rows:
                    return
                context = [{"name": self.identity(row["owner"]) if row["is_agent"] else row["nickname"],
                            "message": row["body"]} for row in reversed(rows)]
                result = await model_client(config, self.app.model_url).ainvoke([
                    SystemMessage(content=(profile.get("persona") or self.app.default_persona) + "\n你的名字是 " + self.identity(owner) + "\n" + workflow("group")),
                    HumanMessage(content=dump({"history": context[:-1], "new_message": context[-1]})),
                ])
                content = result.text.strip()
                if content and content != "[SKIP]":
                    self.send(owner, room, content)
            except Exception:
                log.exception("Group agent reply failed")

    def send(self, owner, room, content):
        if self.joined(room, owner) and self.app.external.settings(owner)["mode"] == "platform":
            self.db.execute("INSERT INTO community_group_messages(room,owner,body,is_agent) VALUES(?,?,?,1)",
                            (room, owner, content[:1500]))
            return True
        return False

    async def run_patrol(self, owner):
        async with self.slots:
            try:
                from langchain.agents import create_agent
                profile = self.app.profile(owner)
                config = selected(profile)
                if not config.get("key") or self.app.external.settings(owner)["mode"] != "platform":
                    return

                async def market() -> str:
                    """查看当前公开外汇报价。"""
                    return dump(self.app.engine.quotes)

                async def forum(action: str, title: str = "", body: str = "") -> str:
                    """逛论坛或发帖。action 是 list 或 publish。"""
                    if action == "list":
                        return dump(self.app.community.handle(owner, "GET", "", {}))
                    if action == "publish" and self.app.external.settings(owner)["mode"] == "platform":
                        return dump(self.app.community.handle(owner, "POST", "", {"title": title, "body": body},
                                                             is_agent=True))
                    return "操作无效"

                async def group(action: str, room: str = "", body: str = "") -> str:
                    """列出已加入的群聊、读群或发言。action 是 list、read 或 send。"""
                    if action == "list":
                        return dump([dict(row) for row in self.db.execute("""SELECT g.id,g.name FROM community_groups g
                            JOIN community_group_agents a ON a.room=g.id WHERE a.owner=?""", (owner,))])
                    if not self.joined(room, owner):
                        return "智能体未加入该群"
                    if action == "read":
                        data = self.app.community.groups(owner, "GET", room, {})
                        return dump([dict(name=self.identity(row["owner"]) if row["is_agent"] else row["nickname"],
                                          message=row["body"]) for row in reversed(data["entries"][:12])])
                    if action == "send" and body.strip():
                        return "已发送" if self.send(owner, room, body.strip()) else "智能体已离开"
                    return "操作无效"

                agent = create_agent(model_client(config, self.app.model_url), tools=[market, forum, group],
                    system_prompt=(profile.get("persona") or self.app.default_persona) + "\n" + workflow("patrol"))
                await agent.ainvoke({"messages": [HumanMessage(content=datetime.now(ZONE).isoformat())]},
                                    config={"recursion_limit": 8})
            except Exception:
                log.exception("Agent patrol failed")

    def tick(self, now):
        day = now.date().isoformat()
        minute = now.hour * 60 + now.minute
        for row in self.db.execute("SELECT owner FROM demo_profiles").fetchall():
            owner = row[0]
            profile = self.app.profile(owner)
            config = profile.get("social", {})
            mode = config.get("patrol", "off")
            if mode == "off" or self.app.external.settings(owner)["mode"] != "platform" or not selected(profile).get("key"):
                continue
            def clock(value):
                hours, minutes = map(int, value.split(":"))
                return hours * 60 + minutes
            start = clock(config["start"])
            end = clock(config["end"]) if mode == "random" else start
            run = self.db.execute("SELECT day,minute,ran FROM agent_patrol_runs WHERE owner=?", (owner,)).fetchone()
            if run and run["day"] == day and run["ran"]:
                continue
            if not run or run["day"] != day or not start <= run["minute"] <= end:
                target = random.randint(start, end)
                self.db.execute("INSERT OR REPLACE INTO agent_patrol_runs VALUES(?,?,?,0)", (owner, day, target))
            else:
                target = run["minute"]
            if target <= minute <= end and self.submit(self.run_patrol, owner):
                self.db.execute("UPDATE agent_patrol_runs SET ran=1 WHERE owner=?", (owner,))

    async def patrol(self):
        while True:
            try:
                self.tick(datetime.now(ZONE))
            except Exception:
                log.exception("Patrol scheduler failed")
            await asyncio.sleep(15)
