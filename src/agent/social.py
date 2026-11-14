"""Background group replies and daily agent patrols."""
import asyncio
import logging
import random
import re
from datetime import datetime
import time
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
    def integer(name, default, low, high):
        value = body.get(name, default)
        if type(value) is not int or not low <= value <= high:
            raise ValueError("请输入有效的频率或未回复上限")
        return value

    minimum = integer("interval_min", 35, 1, 1440)
    maximum = integer("interval_max", 120, minimum, 1440)
    direct_start = body.get("direct_start", "10:00")
    direct_end = body.get("direct_end", "22:00")
    if not all(isinstance(value, str) and re.fullmatch(r"(?:[01][0-9]|2[0-3]):[0-5][0-9]", value)
               for value in (direct_start, direct_end)) or direct_start >= direct_end:
        raise ValueError("主动消息的结束时间须晚于开始时间")
    direct_probability = body.get("direct_probability", 0)
    if type(direct_probability) not in (int, float) or not 0 <= direct_probability <= 100:
        raise ValueError("主动消息概率须在 0～100% 之间")
    return dict(probability=probability, patrol=mode, start=start, end=end,
                interval_min=minimum, interval_max=maximum, direct_start=direct_start,
                direct_end=direct_end, direct_probability=direct_probability,
                direct_limit=integer("direct_limit", 1, 1, 20))


class Social:
    def __init__(self, app):
        self.app = app
        self.db = app.db
        self.tasks = set()
        self.slots = asyncio.Semaphore(4)
        self.db.execute("""CREATE TABLE IF NOT EXISTS agent_patrol_runs(
            owner TEXT PRIMARY KEY REFERENCES sim_users(id) ON DELETE CASCADE,
            day TEXT NOT NULL, minute INTEGER NOT NULL, ran INTEGER NOT NULL)""")

        self.running = set()
        self.db.execute("""CREATE TABLE IF NOT EXISTS agent_social_clock(
            owner TEXT REFERENCES sim_users(id) ON DELETE CASCADE,
            kind TEXT NOT NULL, schedule TEXT NOT NULL, minute INTEGER NOT NULL,
            PRIMARY KEY(owner,kind))""")

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
        message = self.db.execute("SELECT body,created FROM community_group_messages WHERE id=? AND room=? AND NOT is_agent",
                                  (message_id, room)).fetchone()
        if not message:
            return
        for row in self.db.execute("SELECT owner FROM community_group_agents WHERE room=?", (room,)):
            owner = row[0]
            profile = self.app.profile(owner)
            mention = re.search(r"@" + re.escape(self.identity(owner)) + r"(?=$|[\s，。！？、,.!?：:])", message[0])
            followup = self.db.execute("""SELECT 1 FROM community_group_messages
                WHERE room=? AND owner=? AND is_agent=1 AND id<? AND created BETWEEN ? AND ?
                LIMIT 1""", (room, owner, message_id, message["created"] - 5, message["created"])).fetchone() is not None
            if mention or followup or random.random() * 100 < profile.get("social", {}).get("probability", 0):
                self.submit(self.reply, owner, room, message_id, followup)

    async def reply(self, owner, room, message_id, followup=False):
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
                task_prompt = workflow("group_followup" if followup else "group")
                result = await model_client(config, self.app.model_url).ainvoke([
                    SystemMessage(content=(profile.get("persona") or self.app.default_persona) + "\n你的名字是 " + self.identity(owner) + "\n" + task_prompt),
                    HumanMessage(content=dump({"history": context[:-1], "new_message": context[-1]})),
                ])
                content = result.text.strip()
                if content and content != "[SKIP]":
                    self.send(owner, room, content)
            except Exception:
                log.exception("Group agent reply failed")

    def send(self, owner, room, content):
        if self.joined(room, owner) and self.app.external.settings(owner)["mode"] == "platform":
            self.db.execute("INSERT INTO community_group_messages(room,owner,body,is_agent,created) VALUES(?,?,?,1,?)",
                            (room, owner, content[:1500], time.time()))
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

    def unanswered(self, gid):
        return self.db.execute("""SELECT count(*) FROM demo_chat WHERE game=? AND proactive=1
            AND id > COALESCE((SELECT max(id) FROM demo_chat WHERE game=? AND role='user'),0)""",
            (gid, gid)).fetchone()[0]

    async def direct(self, owner):
        async with self.slots:
            try:
                profile = self.app.profile(owner)
                config = social_config(profile.get("social", {}))
                model = selected(profile)
                game = next((item for item in self.app.engine.games() if item["owner_id"] == owner), None)
                clock = datetime.now(ZONE).strftime("%H:%M")
                if (not game or not model.get("key") or not config["direct_probability"]
                        or not config["direct_start"] <= clock <= config["direct_end"]
                        or self.app.external.settings(owner)["mode"] != "platform"):
                    return
                gid = game["id"]
                if self.unanswered(gid) >= config["direct_limit"]:
                    return
                history = [dict(row) for row in self.db.execute(
                    "SELECT role,content FROM demo_chat WHERE game=? ORDER BY id DESC LIMIT 8", (gid,))]
                reply = await model_client(model, self.app.model_url).ainvoke([
                    SystemMessage(content=(profile.get("persona") or self.app.default_persona) + "\n" + workflow("proactive")),
                    HumanMessage(content=dump({"time": datetime.now(ZONE).isoformat(), "history": history[::-1]})),
                ])
                content = reply.text.strip()
                latest = social_config(self.app.profile(owner).get("social", {}))
                if (content and content != "[SKIP]" and latest["direct_probability"]
                        and latest["direct_start"] <= datetime.now(ZONE).strftime("%H:%M") <= latest["direct_end"]
                        and self.unanswered(gid) < latest["direct_limit"]
                        and self.app.external.settings(owner)["mode"] == "platform"
                        and any(item["id"] == gid and item["owner_id"] == owner for item in self.app.engine.games())):
                    self.db.execute("""INSERT INTO demo_chat(game,role,content,created,proactive)
                        VALUES(?,'assistant',?,?,1)""", (gid, content[:1500], time.time()))
            except Exception:
                log.exception("Proactive message failed")

    def queue_clock(self, owner, kind, schedule, target):
        self.db.execute("""INSERT OR REPLACE INTO agent_social_clock(owner,kind,schedule,minute)
            VALUES(?,?,?,?)""", (owner, kind, schedule, target))

    async def run_scheduled(self, owner, kind, action):
        try:
            await action(owner)
        finally:
            self.running.discard((owner, kind))

    def tick(self, now):
        def clock(value):
            hours, minutes = map(int, value.split(":"))
            return hours * 60 + minutes

        minute = now.hour * 60 + now.minute
        for row in self.db.execute("SELECT owner FROM demo_profiles").fetchall():
            owner = row[0]
            profile = self.app.profile(owner)
            if self.app.external.settings(owner)["mode"] != "platform" or not selected(profile).get("key"):
                continue
            config = social_config(profile.get("social", {}))
            mode = config["patrol"]
            interval = (config["interval_min"], config["interval_max"])
            if mode != "off":
                self.tick_clock(owner, "patrol", now, minute, clock(config["start"]),
                                clock(config["end"]) if mode == "random" else clock(config["start"]),
                                None if mode == "fixed" else interval, 100, self.run_patrol)
            if config["direct_probability"]:
                self.tick_clock(owner, "direct", now, minute, clock(config["direct_start"]),
                                clock(config["direct_end"]), interval, config["direct_probability"], self.direct)

    def tick_clock(self, owner, kind, now, minute, start, end, interval, probability, action):
        if not start <= minute <= end or (owner, kind) in self.running:
            return
        schedule = dump((now.date().isoformat(), start, end, interval))
        row = self.db.execute("SELECT schedule,minute FROM agent_social_clock WHERE owner=? AND kind=?",
                              (owner, kind)).fetchone()
        if not row or row["schedule"] != schedule:
            target = minute + random.randint(*interval) if interval else start
            self.queue_clock(owner, kind, schedule, target)
        else:
            target = row["minute"]
        if minute < target:
            return
        if random.random() * 100 < probability:
            if not self.submit(self.run_scheduled, owner, kind, action):
                return
            self.running.add((owner, kind))
        next_target = minute + random.randint(*interval) if interval else end + 1
        self.queue_clock(owner, kind, schedule, next_target)

    async def patrol(self):
        while True:
            try:
                self.tick(datetime.now(ZONE))
            except Exception:
                log.exception("Patrol scheduler failed")
            await asyncio.sleep(15)
