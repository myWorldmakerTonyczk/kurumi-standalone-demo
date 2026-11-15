"""Background group replies and daily agent patrols."""
import asyncio
import json
import logging
import random
import re
from collections import Counter
from datetime import datetime
import time
from zoneinfo import ZoneInfo

from aiohttp import web
from langchain_core.messages import HumanMessage, SystemMessage

from src.core.engine import dump
from .chat_debounce import ChatDebounce, debounce_config
from .image_library import image_reference
from .model_registry import model_client, selected
from .tool.bulletins import build_tool as build_bulletins_tool
from .workflow import social_system_prompt, workflow

log = logging.getLogger(__name__)
ZONE = ZoneInfo("Asia/Shanghai")


def social_config(body):
    probability = body.get("probability", 50)
    mode = body.get("patrol", "random")
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
    direct_mode = body.get("direct_mode", "random" if body.get("direct_probability") else "off")
    if direct_mode not in ("off", "random"):
        raise ValueError("请选择有效的主动消息模式")
    direct_min = integer("direct_interval_min", 35, 1, 1440)
    return dict(probability=probability, patrol=mode, start=start, end=end,
                interval_min=minimum, interval_max=maximum, direct_start=direct_start,
                direct_end=direct_end, direct_mode=direct_mode, direct_interval_min=direct_min,
                direct_interval_max=integer("direct_interval_max", 60, direct_min, 1440),
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
        self.chat_pending = Counter()
        self.chat_batches = ChatDebounce()
        self.chat_received = {}
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
        rows = self.db.execute("""SELECT a.owner,u.nickname,1 AS is_agent FROM community_group_agents a
            JOIN sim_users u ON u.id=a.owner JOIN community_group_members gm ON gm.room=a.room AND gm.owner=a.owner
            WHERE a.room=?""", (room,))
        return self.decorate([dict(row) for row in rows])

    def group_roster(self, room):
        rows = self.db.execute("""SELECT m.owner,u.nickname,
                EXISTS(SELECT 1 FROM community_group_agents a WHERE a.room=m.room AND a.owner=m.owner) AS agent
            FROM community_group_members m JOIN sim_users u ON u.id=m.owner
            WHERE m.room=? ORDER BY m.owner""", (room,))
        roster = {}
        for row in rows:
            owner = row["owner"]
            display = self.group_display_nickname(owner, row["nickname"])
            roster[f"user:{owner}"] = {"type": "user", "name": display, "mention": "@" + display}
            if row["agent"]:
                agent = self.identity(owner)
                roster[f"agent:{owner}"] = {
                    "type": "agent", "name": agent, "owner_nickname": display, "owner_id": f"user:{owner}",
                    "mention": f"@{agent}（{display}）",
                }
        return roster

    @staticmethod
    def group_mentions(text, roster):
        aliases = {}
        for member_id, member in roster.items():
            labels = [member["mention"][1:]]
            if member["type"] == "agent":
                labels.extend((member["name"], f'{member["name"]}({member["owner_nickname"]})'))
            for label in labels:
                aliases.setdefault(label.casefold(), set()).add(member_id)
        if not aliases or "@" not in text:
            return []
        labels = sorted(aliases, key=len, reverse=True)
        pattern = re.compile(r"@(" + "|".join(map(re.escape, labels)) + r")(?=$|[\s，。！？、,.!?：:；;])", re.IGNORECASE)
        return list(dict.fromkeys(
            next(iter(ids)) for match in pattern.finditer(text)
            if len(ids := aliases[match[1].casefold()]) == 1
        ))

    def group_display_nickname(self, owner, nickname):
        duplicate = self.db.execute("""SELECT count(*) FROM sim_users
            WHERE nickname=? COLLATE NOCASE AND id<?""", (nickname, owner)).fetchone()[0]
        return nickname + (f"（{duplicate + 1}）" if duplicate else "")

    def decorate(self, entries):
        for entry in entries:
            owner = entry.pop("owner")
            profile = self.app.profile(owner)
            nickname = self.group_display_nickname(owner, entry["nickname"])
            if entry["is_agent"]:
                entry["mention_name"] = self.identity(owner)
                nickname = f'{entry["mention_name"]}（{nickname}）'
            entry["nickname"] = nickname
            entry["avatar"] = profile.get("agent_avatar", "avatar.png") if entry["is_agent"] else profile.get("avatar", "user-avatar.svg")
        return entries

    def forum_entry_for_agent(self, viewer, entry):
        data = dict(entry)
        author_account = data.pop("owner")
        nickname = data.pop("nickname")
        is_agent = bool(data.pop("is_agent"))
        data.pop("mine", None)
        for key in ("title_media", "media"):
            if data.get(key):
                reference = f"post:{data['id']}" if "title" in data else f"comment:{data['id']}"
                if key == "title_media":
                    reference += ":title"
                info = image_reference(data[key], reference)
                data[key] = {field: value for field, value in data[key].items() if field != "src"}
                if info:
                    data[key].update(info)
        author_type = "agent" if is_agent else "user"
        relation = ("self" if is_agent else "my_user") if author_account == viewer else (
            "other_agent" if is_agent else "other_user"
        )
        data["author"] = {
            "id": f"{author_type}:{author_account}",
            "type": author_type,
            "name": self.identity(author_account) if is_agent else nickname,
            "relation": relation,
        }
        if is_agent:
            data["author"]["owner_nickname"] = nickname
        return data

    def forum_stickers(self, owner, category_id=""):
        return [{"id": item["id"], "label": item["label"], "category": item["category"]}
                for item in self.app.stickers.list(owner) if not category_id or item["category"] == category_id]

    def forum_list_for_agent(self, owner):
        result = self.app.community.handle(owner, "GET", "", {}, {})
        return result | {"entries": [self.forum_entry_for_agent(owner, row) for row in result["entries"]]}

    def submit(self, job, *args):
        if len(self.tasks) >= 64:
            return False
        task = asyncio.create_task(job(*args))
        self.tasks.add(task)
        task.add_done_callback(self.tasks.discard)
        return True

    async def close(self):
        await self.chat_batches.close()
        tasks = list(self.tasks)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    def on_message(self, room, message_id):
        if not room:
            return
        room = int(room)
        message = self.db.execute("""SELECT m.body,m.owner,m.is_agent,m.mentions,m.mention_depth,
                q.owner AS reply_owner,q.is_agent AS reply_agent
            FROM community_group_messages m LEFT JOIN community_group_messages q
              ON q.id=m.reply_to AND q.room=m.room AND q.status='active'
            WHERE m.id=? AND m.room=? AND m.status='active'""", (message_id, room)).fetchone()
        if not message:
            return
        roster = self.group_roster(room)
        if message["mentions"] is None:
            mentions = self.group_mentions(message["body"], roster)
            self.db.execute("UPDATE community_group_messages SET mentions=? WHERE id=?", (dump(mentions), message_id))
        else:
            mentions = json.loads(message["mentions"])
        targets = set(mentions)
        if message["reply_owner"]:
            role = "agent" if message["reply_agent"] else "user"
            targets.add(f'{role}:{message["reply_owner"]}')
        agent_addressed = any(target.startswith("agent:") and target in roster for target in targets)
        for member_id, member in roster.items():
            if member["type"] != "agent":
                continue
            owner = member_id.partition(":")[2]
            if message["is_agent"] and message["owner"] == owner:
                continue
            directed = member_id in targets
            if directed:
                self.app.remember_community(owner, message["owner"], message["body"], "group",
                                            stream=room, event=message_id, is_agent=bool(message["is_agent"]))
            if message["is_agent"] and (
                message["mention_depth"] >= 1 or not agent_addressed
            ):
                continue
            profile = self.app.profile(owner)
            config = debounce_config(profile.get("chat_debounce", {}))
            quiet = config["group_quiet"] if config["enabled"] else 0
            maximum = config["group_max"] if config["enabled"] else 0
            self.chat_batches.enqueue(("group", owner, room), (message_id, directed),
                                      quiet, maximum, self.group_batch)

    async def group_batch(self, key, messages):
        _, owner, room = key
        directed = any(item[1] for item in messages)
        probability = self.app.profile(owner).get("social", {}).get("probability", 50)
        if directed or random.random() * 100 < probability:
            await self.reply(owner, room, messages[-1][0], directed,
                             incoming_ids=[message_id for message_id, _ in messages])

    async def reply(self, owner, room, message_id, followup=False, *, incoming_ids=()):
        async with self.slots:
            try:
                if not self.joined(room, owner):
                    return
                profile = self.app.profile(owner)
                config = selected(profile)
                if not config.get("key") or self.app.external.settings(owner)["mode"] != "platform":
                    return
                first_id = incoming_ids[0] if incoming_ids else message_id
                rows = self.db.execute("""WITH history AS (
                        SELECT * FROM community_group_messages
                        WHERE room=? AND id<? AND status='active' ORDER BY id DESC LIMIT 15
                    ), context AS (
                        SELECT * FROM history UNION ALL
                        SELECT * FROM community_group_messages WHERE room=? AND id BETWEEN ? AND ? AND status='active'
                    )
                    SELECT m.id,m.body,m.owner,m.is_agent,u.nickname,m.attachment,m.mentions,m.reply_to,
                           q.owner AS reply_owner,q.is_agent AS reply_agent
                    FROM context m JOIN sim_users u ON u.id=m.owner
                    LEFT JOIN community_group_messages q ON q.id=m.reply_to AND q.room=m.room AND q.status='active'
                    ORDER BY m.id DESC""",
                    (room, first_id, room, first_id, message_id)).fetchall()
                if not rows:
                    return
                roster = self.group_roster(room)
                self_id = f"agent:{owner}"
                context = []
                for row in reversed(rows):
                    role = "agent" if row["is_agent"] else "user"
                    author_id = f'{role}:{row["owner"]}'
                    author = dict(roster.get(author_id, {"type": role, "name": row["nickname"]}))
                    if row["is_agent"] and author_id not in roster:
                        author.update(name=self.identity(row["owner"]), owner_nickname=row["nickname"],
                                      owner_id=f'user:{row["owner"]}')
                    reply_role = "agent" if row["reply_agent"] else "user"
                    reply_author = f'{reply_role}:{row["reply_owner"]}' if row["reply_owner"] else None
                    context.append({"id": row["id"], "author": author | {"id": author_id, "is_self": author_id == self_id},
                                    "text": row["body"], "reply_to": row["reply_to"], "reply_to_author": reply_author,
                                    "mentions": json.loads(row["mentions"]) if row["mentions"] is not None
                                    else self.group_mentions(row["body"], roster)})
                has_images = False
                for entry, row in zip(context, reversed(rows)):
                    ref = image_reference(json.loads(row["attachment"] or "null"), f"group:{room}:{row['id']}")
                    if ref:
                        entry["image"] = ref
                        has_images = True
                task_prompt = workflow("group_followup" if followup else "group")
                model = model_client(config, self.app.model_url, audit=self.app.call_logs.callback(owner, "group_chat"))
                persona = self.app.agent_persona(owner, profile)
                prompt = social_system_prompt(persona) + "\n" + task_prompt
                portrait = []
                if profile.get("chat_memory", {}).get("enabled", True):
                    subjects = ["self" if row["owner"] == owner else "user:" + row["owner"]
                                for row in rows if not row["is_agent"]]
                    portrait = self.app.memory.profiles.context(owner, f"group:{room}", subjects)
                    if portrait:
                        prompt += "\n" + workflow("private_memory_context")
                from .vision import describe_image

                incoming_ids = set(incoming_ids or (message_id,))
                incoming = []
                history = []
                for entry, row in zip(context, reversed(rows)):
                    if row["id"] not in incoming_ids:
                        history.append(entry)
                        continue
                    attachment = json.loads(row["attachment"] or "null")
                    if attachment and attachment["kind"] == "image":
                        entry["image_description"] = await describe_image(
                            profile, attachment["src"], row["body"], self.app.model_url,
                            audit=self.app.call_logs.callback(owner, "vision"),
                        )
                    incoming.append(entry)
                if not incoming:
                    return
                if any("image_description" in entry for entry in incoming):
                    prompt += "\n" + workflow("vision_context")
                human = HumanMessage(content=dump({"you": roster[self_id] | {"id": self_id},
                    "members": [member | {"id": member_id} for member_id, member in roster.items()],
                    "history": history, "incoming_messages": incoming, "profiles": portrait}))

                async def mention_group_member(member_id: str) -> str:
                    """按 members 里的成员 ID 获取本群的正确 @ 写法。把返回的 mention 放入最终回复才会发送。"""
                    member = roster.get(member_id)
                    if not member or member_id == self_id:
                        return dump({"error": "请选择当前群里的其他成员"})
                    return dump({"member_id": member_id, "mention": member["mention"]})

                attachments = []
                tools = [build_bulletins_tool(self.app), mention_group_member]
                if has_images:
                    from .tool.images import build_tools

                    tools.extend(build_tools(self.app, owner, None, None, None))
                    prompt += "\n可用 save_image 收藏当前群聊中值得留存的图片，使用消息附带的 reference。"
                if random.random() * 100 < profile.get("sticker_probability", 100):
                    from .tool.stickers import sticker_tools

                    tools.extend(sticker_tools(self.app, owner, attachments))
                    prompt += "\n" + workflow("private_sticker")
                from langchain.agents import create_agent

                agent = create_agent(model, tools=tools, system_prompt=prompt)
                result = (await agent.ainvoke({"messages": [human]}, config={"recursion_limit": 12}))["messages"][-1]
                content = result.text.strip()
                if content != "[SKIP]" and (content or attachments):
                    replying_to_agent = any(entry["author"]["type"] == "agent" for entry in incoming)
                    if self.send(owner, room, content, attachments[0] if attachments else None,
                                 mention_depth=int(replying_to_agent)):
                        for row in rows:
                            if row["id"] in incoming_ids:
                                self.app.remember_community(owner, row["owner"], row["body"], "group",
                                    stream=room, event=row["id"], is_agent=bool(row["is_agent"]))
            except Exception:
                log.exception("Group agent reply failed")

    def send(self, owner, room, content, attachment=None, *, mention_depth=0):
        if self.joined(room, owner) and self.app.external.settings(owner)["mode"] == "platform":
            content = content[:1500]
            mentions = self.group_mentions(content, self.group_roster(room))
            cursor = self.db.execute("""INSERT INTO community_group_messages
                (room,owner,body,is_agent,created,attachment,mentions,mention_depth) VALUES(?,?,?,1,?,?,?,?)""",
                (room, owner, content, time.time(), dump(attachment) if attachment else None, dump(mentions), mention_depth))
            self.on_message(room, cursor.lastrowid)
            return True
        return False

    async def run_patrol(self, owner):
        from langchain.agents import create_agent
        from langchain.agents.middleware import ModelCallLimitMiddleware
        from langgraph.errors import GraphRecursionError

        async with self.slots:
            try:
                profile = self.app.profile(owner)
                config = selected(profile)
                if not config.get("key") or self.app.external.settings(owner)["mode"] != "platform":
                    return

                published = False

                async def market() -> str:
                    """查看当前公开外汇报价。"""
                    return dump(self.app.engine.quotes)

                async def forum(action: str, title: str = "", body: str = "", sticker_id: str = "",
                                title_sticker_id: str = "", post_id: int = 0, reply_to: int = 0) -> str:
                    """list 看帖子列表，read 按 post_id 看原帖和评论，stickers 看表情包。
                    回应原帖用 comment 并传 post_id，回复评论时再传 reply_to。publish 仅用于独立新帖。
                    """
                    nonlocal published
                    try:
                        if action in ("comment", "publish") and published:
                            return "本次巡视已完成一次公开发言，请结束。"
                        if action == "stickers":
                            return dump(self.forum_stickers(owner))
                        if action == "list":
                            return dump(self.forum_list_for_agent(owner))
                        if action == "read" and post_id > 0:
                            post = self.app.community.thread(str(post_id))
                            result = self.app.community.handle(owner, "GET", str(post_id), {})
                            return dump({
                                "post": self.forum_entry_for_agent(owner, post),
                                "entries": [self.forum_entry_for_agent(owner, row) for row in result["entries"]],
                            })
                        if self.app.external.settings(owner)["mode"] == "platform":
                            if action == "comment" and post_id > 0:
                                result = self.app.community.handle(owner, "POST", str(post_id), {
                                    "body": body, "sticker_id": sticker_id, "reply_to": reply_to or None,
                                }, is_agent=True)
                                published = True
                                return dump(result)
                            if action == "publish" and not (post_id or reply_to):
                                result = self.app.community.handle(owner, "POST", "", {
                                    "title": title, "body": body, "sticker_id": sticker_id,
                                    "title_sticker_id": title_sticker_id,
                                }, is_agent=True)
                                published = True
                                return dump(result)
                        return "操作无效。回应已有帖子请使用 comment 并指定原帖 post_id。"
                    except (ValueError, web.HTTPException) as exc:
                        return dump({"ok": False, "error": getattr(exc, "reason", str(exc))})

                async def group(action: str, room: str = "", body: str = "") -> str:
                    """列出已加入的群聊、读群或发言。action 是 list、read 或 send。"""
                    nonlocal published
                    if action == "send" and published:
                        return "本次巡视已完成一次公开发言，请结束。"
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
                        published = self.send(owner, room, body.strip())
                        return "已发送，请结束本次巡视" if published else "智能体已离开"
                    return "操作无效"

                agent = create_agent(model_client(config, self.app.model_url, audit=self.app.call_logs.callback(owner, "patrol")), tools=[market, forum, group, build_bulletins_tool(self.app)],
                    middleware=[ModelCallLimitMiddleware(run_limit=8, exit_behavior="end")],
                    system_prompt=social_system_prompt(self.app.agent_persona(owner, profile)) + "\n" + workflow("patrol")
                    + "\n" + workflow("forum_authorship"))
                await agent.ainvoke({"messages": [HumanMessage(content=datetime.now(ZONE).isoformat())]},
                                    config={"recursion_limit": 48})
            except GraphRecursionError:
                log.warning("巡视达到流程步数上限，已结束本次巡视")
            except Exception:
                log.exception("Agent patrol failed")

    def unanswered(self, gid):
        return self.db.execute("""SELECT count(*) FROM demo_chat WHERE game=? AND proactive=1
            AND id > COALESCE((SELECT max(id) FROM demo_chat WHERE game=? AND role='user'),0)""",
            (gid, gid)).fetchone()[0]

    async def user_chat(self, game, message, *, user_image=None):
        gid, owner = game["id"], game["owner_id"]
        config = debounce_config(self.app.profile(owner).get("chat_debounce", {}))
        now = time.time()
        stored = self.db.execute("""INSERT INTO demo_chat(game,role,content,created,attachments)
            VALUES(?,?,?,?,?)""", (gid, "user", message, now, dump([user_image] if user_image else [])))
        self.chat_received[gid] = now
        self.chat_pending[gid] += 1
        future = asyncio.get_running_loop().create_future()
        quiet = config["private_quiet"] if config["enabled"] else 0
        maximum = config["private_max"] if config["enabled"] else 0
        self.chat_batches.enqueue(("private", gid), (game, stored.lastrowid, message, user_image, future),
                                  quiet, maximum, self.private_batch)
        try:
            return await future
        finally:
            self.chat_pending[gid] -= 1
            if not self.chat_pending[gid]:
                del self.chat_pending[gid]

    async def private_batch(self, key, items):
        game = items[0][0]
        messages = [(message_id, text, image) for _, message_id, text, image, _ in items]
        attachments, entry = [], {}
        try:
            reply = await self.app.agent(game, "\n".join(item[1] for item in messages),
                                         attachments=attachments, chat_messages=messages, reply_entry=entry)
        except asyncio.CancelledError:
            for *_, future in items:
                future.cancel()
            raise
        except Exception as error:
            for *_, future in items:
                if not future.done():
                    future.set_exception(error)
        else:
            recipient = next((item[-1] for item in reversed(items) if not item[-1].done()), None)
            for *_, future in items:
                if not future.done():
                    future.set_result((reply, attachments, entry) if future is recipient else ("", [], {}))

    def direct_activity(self, gid):
        latest = self.db.execute(
            "SELECT created FROM demo_chat WHERE game=? AND role='user' ORDER BY id DESC LIMIT 1",
            (gid,),
        ).fetchone()
        return max(self.chat_received.get(gid, 0), latest[0] if latest else 0)

    async def direct(self, owner, expected_activity):
        async with self.slots:
            try:
                profile = self.app.profile(owner)
                config = social_config(profile.get("social", {}))
                model = selected(profile)
                game = next((item for item in self.app.engine.games() if item["owner_id"] == owner), None)
                clock = datetime.now(ZONE).strftime("%H:%M")
                if (not game or not model.get("key") or config["direct_mode"] != "random"
                        or not config["direct_start"] <= clock < config["direct_end"]
                        or self.app.external.settings(owner)["mode"] != "platform"):
                    return
                gid = game["id"]
                activity = self.direct_activity(gid)
                if activity != expected_activity or self.chat_pending.get(gid) or self.unanswered(gid) >= config["direct_limit"]:
                    return
                history = [dict(row) for row in self.db.execute(
                    "SELECT role,content FROM demo_chat WHERE game=? ORDER BY id DESC LIMIT 8", (gid,))]
                reply = await model_client(model, self.app.model_url, audit=self.app.call_logs.callback(owner, "proactive")).ainvoke([
                    SystemMessage(content=social_system_prompt(self.app.agent_persona(owner, profile))
                                  + "\n" + workflow("proactive")),
                    HumanMessage(content=dump({"time": datetime.now(ZONE).isoformat(), "history": history[::-1]})),
                ])
                content = reply.text.strip()
                latest = social_config(self.app.profile(owner).get("social", {}))
                if (content and content != "[SKIP]" and latest == config
                        and not self.chat_pending.get(gid) and self.direct_activity(gid) == activity
                        and latest["direct_start"] <= datetime.now(ZONE).strftime("%H:%M") < latest["direct_end"]
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

    async def run_scheduled(self, owner, kind, action, *args):
        try:
            await action(owner, *args)
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
                                None if mode == "fixed" else interval, self.run_patrol)
            if config["direct_mode"] == "random":
                self.tick_direct(owner, now, config)

    def tick_direct(self, owner, now, config):
        clock = now.strftime("%H:%M")
        if not config["direct_start"] <= clock < config["direct_end"] or (owner, "direct") in self.running:
            return
        game = next((item for item in self.app.engine.games() if item["owner_id"] == owner), None)
        if not game or self.chat_pending.get(game["id"]):
            return
        gid = game["id"]
        if self.unanswered(gid) >= config["direct_limit"]:
            return
        activity = self.direct_activity(gid)
        interval = config["direct_interval_min"], config["direct_interval_max"]
        settings = ["direct-v2", now.date().isoformat(), gid, config["direct_start"],
                    config["direct_end"], list(interval), config["direct_limit"]]
        schedule = dump([*settings, activity])
        row = self.db.execute("SELECT schedule,minute FROM agent_social_clock WHERE owner=? AND kind='direct'",
                              (owner,)).fetchone()
        now_seconds = int(now.timestamp())
        if not row or row["schedule"] != schedule:
            # After a user message, count from its arrival, even if the reply took time.
            start_hour, start_minute = map(int, config["direct_start"].split(":"))
            window_start = int(now.replace(hour=start_hour, minute=start_minute, second=0, microsecond=0).timestamp())
            previous = json.loads(row["schedule"]) if row else []
            reset = previous[:-1] == settings and activity > previous[-1]
            base = max(window_start, int(activity)) if reset else now_seconds
            target = base + 60 * random.randint(*interval)
            self.queue_clock(owner, "direct", schedule, target)
        else:
            target = row["minute"]
        if now_seconds >= target and self.submit(self.run_scheduled, owner, "direct", self.direct, activity):
            self.running.add((owner, "direct"))
            self.queue_clock(owner, "direct", schedule, now_seconds + 60 * random.randint(*interval))

    def tick_clock(self, owner, kind, now, minute, start, end, interval, action):
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
