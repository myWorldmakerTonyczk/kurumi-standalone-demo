"""Standalone Kurumi demo."""
import asyncio
import base64
import binascii
import hmac
import json
import logging
from logging.handlers import RotatingFileHandler
import traceback
import os
import secrets
import random
import time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from aiohttp import ClientConnectorError, web
from yarl import URL

from src.core.engine import Engine, PAIRS, dump
from src.core.trading import execute, public_snapshot, lock_for
from src.market_data.market import Market, market_error, source
from src.portal.access import PortalAccess
from src.users.ownership import issue_id
from src.portal.accounts import PortalAccounts, password_hash
from dashboard_api import Dashboard
from src.agent.image_library import ImageLibrary
from src.agent.market_bulletin import MarketBulletin
from src.agent.inbox import AgentInbox
from src.agent.timeline import AgentTimeline, task_trace
from src.agent.task_trace_middleware import TraceTools
from src.agent.task_schedule import ACCOUNT_EVENTS, TaskSchedule
from src.agent.mcp.load_tool_group import INSTRUCTIONS as TOOL_INSTRUCTIONS, ToolGroups, tool_name
from src.agent.mcp.registry import flatten, user_workspace_groups
from src.agent.workflow import agent_real_name, character_persona, persona_body, social_system_prompt, workflow
from src.agent.chat_debounce import debounce_config
from src.agent.social import Social, social_config
from src.agent.call_logs import CallLogs, CallTrace
from src.agent.stickers import Stickers
from src.agent.memory import MEMORY_DAYS, Memory
from src.portal.community import Community, community_image
from src.portal.leaderboard import standings
from src.external_agent.api import ExternalAgent
from src.agent.model_registry import connections, model_client, public_models, retryable, selected, update as update_models
from src.agent.decision_settings import settings as rest_settings
from src.core.availability import automatic_break
from src.core.replay import review
from src.core.replay_records import decision_scope
from src.diary.store import redact
from src.diary.settings import settings as diary_settings, bounds, due_before
from src.notifications.settings import validate as notice_settings, delivery_time
from src.portal.server import ASSETS, SECURITY_HEADERS

PERSONA_MAX_LENGTH = 10_000
def listen_port(admin):
    name, default = ("KURUMI_ADMIN_PORT", 8790) if admin else ("KURUMI_USER_PORT", 8791)
    port = int(os.getenv(name, str(default)))
    if not 1 <= port <= 65535:
        raise ValueError(f"{name} must be between 1 and 65535")
    return port


DEFAULT_PERSONA = (Path(__file__).resolve().parent / "config" / "default_persona.md").read_text(encoding="utf-8").strip()
MODEL_PRESETS = [
    dict(name="DeepSeek", url="https://api.deepseek.com", model="deepseek-flash"),
    dict(name="OpenAI", url="https://api.openai.com/v1", model="gpt-4o-mini"),
    dict(name="Gemini", url="https://generativelanguage.googleapis.com/v1beta/openai/", model="gemini-3.8-flash"),
]

BULLETIN_MODEL_PRESETS = [
    MODEL_PRESETS[0] | {"name": "DeepSeek Flash"},
    MODEL_PRESETS[0] | {"name": "DeepSeek V4 Pro", "model": "deepseek-v4-pro"},
    MODEL_PRESETS[1] | {"name": "OpenAI GPT-5 mini", "model": "gpt-5-mini"},
    MODEL_PRESETS[2] | {"name": "Gemini 3.8 Flash"},
]

# LangGraph defaults to 25; the chat agent needs room for a few tool calls
# before it writes the reply. 10 was low enough to abort normal requests.
AGENT_RECURSION_LIMIT = 25


async def invoke_agent(agent, messages, *, limit=AGENT_RECURSION_LIMIT):
    """Run the chat agent, turning LangGraph's step limit into a readable 400."""
    from langgraph.errors import GraphRecursionError
    try:
        return await agent.ainvoke({"messages": messages}, config={"recursion_limit": limit})
    except GraphRecursionError:
        raise ValueError("这次请求的步骤太多，已中止。请把问题拆小一点再试。") from None

VIEWS = Path(__file__).with_name("views") / "dashboard"


class Demo:
    def __init__(self, path):
        self.engine = Engine(path)
        self.db = self.engine.db
        self.default_persona = DEFAULT_PERSONA
        self.timeline = AgentTimeline(self.db, self.profile)
        self.task_schedule = TaskSchedule(self)
        self.social = Social(self)
        self.stickers = Stickers(self.db)
        self.agent_inbox = AgentInbox(self.engine)
        self.community = Community(self.db, stickers=self.stickers, agent_inbox=self.agent_inbox)
        self.engine.on_log = self.record_account_event
        self.call_logs = CallLogs(self.db, self.profile)
        self.images = ImageLibrary(self)
        self.market_bulletin = MarketBulletin(self)
        self.memory = Memory(self.db, call_logs=self.call_logs, timeline=self.timeline)
        self.external = ExternalAgent(self)
        self.db.executescript("""
        CREATE TABLE IF NOT EXISTS demo_profiles(owner TEXT PRIMARY KEY, data TEXT);
        CREATE TABLE IF NOT EXISTS demo_chat(id INTEGER PRIMARY KEY, game TEXT, role TEXT, content TEXT);
        CREATE TABLE IF NOT EXISTS demo_schedule(game TEXT PRIMARY KEY, due REAL);
        """)
        if "created" not in {row["name"] for row in self.db.execute("PRAGMA table_info(demo_chat)")}:
            self.db.execute("ALTER TABLE demo_chat ADD COLUMN created REAL")
        if "reasoning" not in {row["name"] for row in self.db.execute("PRAGMA table_info(demo_chat)")}:
            self.db.execute("ALTER TABLE demo_chat ADD COLUMN reasoning TEXT")
        if "proactive" not in {row["name"] for row in self.db.execute("PRAGMA table_info(demo_chat)")}:
            self.db.execute("ALTER TABLE demo_chat ADD COLUMN proactive INTEGER NOT NULL DEFAULT 0")
        if "attachments" not in {row["name"] for row in self.db.execute("PRAGMA table_info(demo_chat)")}:
            self.db.execute("ALTER TABLE demo_chat ADD COLUMN attachments TEXT NOT NULL DEFAULT '[]'")
        self.accounts = PortalAccounts(PortalAccess(self.engine))
        self.market = Market(self.engine)
        self.instance_locks = {}
        self.report_times = {}
        self.admin_file = Path(path).with_name("admin.json")
        self.admin_config = json.loads(self.admin_file.read_text(encoding="utf-8")) if self.admin_file.exists() else None
        self.admin_setup = None if self.admin_config else secrets.token_urlsafe(32)
        self.admin = secrets.token_urlsafe(32)
        self.admin_attempts = asyncio.Lock()
        self.demo = os.getenv("KURUMI_LIVE") != "1"
        self.dashboard = Dashboard(self)
        self.history = self.dashboard
        self.diary = self.dashboard.diary

    def settings(self):
        return self.dashboard.get("market", {"history_source": "market"})

    def public_url(self):
        return os.getenv("KURUMI_PUBLIC_URL") or self.dashboard.get("portal", {}).get("origin") or f"http://127.0.0.1:{listen_port(False)}"

    def record_account_event(self, game, entry):
        self.agent_inbox.system(game, entry)
        self.task_schedule.account_event(game, entry)
        if game.get("owner_id"):
            self.timeline.reference(game["owner_id"], {"type": "ledger", "game_id": game["id"],
                                                       "id": entry["id"], "action": entry["action"], "status": entry["status"]})

    async def run_decision(self, game, *, manual=False):
        _, future = self.enqueue_decision(game, manual=manual)
        return await asyncio.shield(future)

    def enqueue_decision(self, game, *, manual=False):
        kind = "manual_watch" if manual else "watch"
        return self.timeline.enqueue(game["owner_id"], kind,
            lambda: self._run_decision(self.engine.get(game["id"]), manual=manual),
            key=None if manual else ("watch", game["id"]),
            outcome=lambda result: "已完成看盘" if result else "已跳过")

    async def _run_decision(self, game, *, manual):
        if game["status"] == "paused" and not manual:
            return ""
        if game["status"] == "ended":
            raise ValueError("本局已结束")
        if manual and lock_for(self, game["id"]).locked():
            raise ValueError("智能体正在处理任务，请稍后再试")
        now = time.time()
        cursor = self.db.execute(
            "INSERT INTO decision_jobs(game,trigger,events,state,created,updated) VALUES(?,?,?,?,?,?)",
            (game["id"], "manual" if manual else "scheduled", "[]", "running", now, now),
        )
        scope = decision_scope.set((game["id"], cursor.lastrowid))
        try:
            reply = await self._agent(game)
        except BaseException as exc:
            self.db.execute("UPDATE decision_jobs SET state='failed',updated=?,error=? WHERE id=?",
                            (time.time(), "看盘任务未完成", cursor.lastrowid))
            if isinstance(exc, Exception):
                trace = CallTrace(self.call_logs, game["owner_id"], "watch")
                details = redact(trace.clean("".join(traceback.format_exception(exc))))
                logging.getLogger("kurumi").error(
                    "看盘任务异常 job=%s game=%s trigger=%s %s\n%s",
                    cursor.lastrowid, game["id"], "manual" if manual else "scheduled",
                    trace.error_label(exc), details,
                )
            raise
        finally:
            decision_scope.reset(scope)
        self.db.execute("UPDATE decision_jobs SET state='success',updated=?,result=? WHERE id=?",
                        (time.time(), dump({"summary": reply}), cursor.lastrowid))
        return reply

    def remember_community(self, owner, speaker, text, source, *, stream, event, is_agent=False):
        if owner == speaker and is_agent or self.external.settings(owner)["mode"] != "platform":
            return
        settings = self.profile(owner)
        memory = settings.get("chat_memory", {})
        if not memory.get("enabled", True) or not memory.get("automatic", True):
            return
        model = next((item for item in connections(settings) if item["id"] == memory.get("model")
                      and item.get("kind", "chat") == "chat" and item.get("key")), selected(settings))
        subject = "self" if owner == speaker else ("agent:" if is_agent else "user:") + speaker
        self.memory.schedule(owner, model, self.model_url, text, subject=subject, stream=source + ":" + str(stream), event=event)

    def profile(self, owner):
        row = self.db.execute("SELECT data FROM demo_profiles WHERE owner=?", (owner,)).fetchone()
        return json.loads(row[0]) if row else {}

    def agent_persona(self, owner, profile):
        relation = profile.get("user_relationship") or "朋友"
        username = self.accounts.username(owner)
        if not username:
            user = self.db.execute("SELECT nickname FROM sim_users WHERE id=?", (owner,)).fetchone()
            username = user["nickname"] if user else owner
        return (f"你最好的{relation}是「{username}」（用户 ID：{owner}）。\n\n"
                "模拟账户的余额、净值、盈亏、保证金、费用与借还款均以日元（JPY）计。货币对的报价单位照常。\n\n"
                + character_persona(profile, self.default_persona))

    def preferences(self, owner, game=None):
        values = self.profile(owner).get("preferences", {})
        mode = self.external.settings(owner)
        if mode["mode"] == "external":
            values = values | {"auto": mode["auto"], "diary": (values.get("diary") or {}) | {"enabled": mode["diary"]}}
        return dict(interval=values.get("interval", (game or {}).get("interval", 3600)),
                    auto=values.get("auto", bool((game or {}).get("auto"))),
                    rest=rest_settings(values.get("rest")),
                    diary=diary_settings(values.get("diary")),
                    notices=notice_settings(values.get("notices") or (game or {}).get("notifications")))

    async def generate_trade_review(self, game, *, days=1, pair="", end=0):
        """Build a safe, publishable review; model text is optional in offline mode."""
        try:
            days = int(days)
        except (TypeError, ValueError):
            raise ValueError("复盘周期无效")
        data = await review(self, game["id"], pair, days, int(end or 0), private=True, include_candles=False)
        stats = next((item for item in data["pair_stats"] if item["pair"] == data["pair"]), {})
        events = [item for item in data["events"] if item.get("action") == "close" and item.get("status") == "success"]
        wins = sum(1 for item in events if float(item.get("position", {}).get("pnl") or 0) > 0)
        net = float(stats.get("net") or 0)
        fills = int(stats.get("fills") or 0)
        drawdown = max((float(mark.get("drawdown") or 0) for mark in data.get("marks", [])), default=0)
        win_rate = round(wins / len(events), 4) if events else 0
        risk_points = []
        if drawdown > 0:
            risk_points.append(f"最大回撤约 {drawdown:.2f}%")
        if fills == 0:
            risk_points.append("周期内没有完成成交，样本不足")
        if net < 0:
            risk_points.append("周期净结果为负，下一周期应降低仓位并复核止损")
        if not risk_points:
            risk_points.append("继续控制单笔风险，避免仅凭短期收益扩大仓位")
        summary = (f"本次复盘覆盖 {data['day']} 前 {days} 天的 {data['pair']}。"
                   f"共完成 {fills} 次成交，胜率 {win_rate:.0%}，净结果 {net:.2f}。"
                   f"当前数据来自模拟账本，不能代表真实收益。")
        publishable = summary + "\n风险提示：" + "；".join(risk_points) + "。"
        if self.profile(game["owner_id"]).get("key") and self.profile(game["owner_id"]).get("model"):
            prompt = ("请把下面这份模拟交易复盘改写成简洁中文社区摘要，只能使用给出的统计，"
                      "不得添加不存在的交易或账户隐私。\n" + publishable)
            try:
                model_text = await self.agent(game, diary_prompt=prompt, audit_task="trade_review")
                if model_text and not model_text.startswith("离线演示日记："):
                    publishable = model_text[:6000]
                    summary = model_text[:1200]
            except Exception:
                pass
        return dict(title=f"{data['pair']} {days}日交易复盘", summary=summary,
                    risk_points=risk_points, stats=dict(pair=data["pair"], fills=fills,
                    wins=wins, win_rate=win_rate, net=round(net, 6), max_drawdown=round(drawdown, 6),
                    start=data["start"], end=data["end"]), publishable_body=publishable,
                    review=data)

    async def summarize_group(self, game, owner, room, *, limit=80):
        data = self.community.group_messages_for_summary(owner, int(room), limit)
        messages = data["messages"]
        text = "；".join(item["body"] for item in messages[-20:])
        topics = []
        for pair in PAIRS:
            if pair in text:
                topics.append(pair)
        if self.profile(owner).get("key") and self.profile(owner).get("model") and game:
            try:
                generated = await self.agent(game, diary_prompt=("请总结以下公开群聊，输出主要观点、风险提醒、争议点和待验证事项，"
                    "不要暴露账户或私聊信息。\n" + text[:6000]), audit_task="group_summary")
                if generated and not generated.startswith("离线演示日记："):
                    return dict(room=int(room), name=data["name"], messages=len(messages), summary=generated[:6000],
                                topics=topics, risk_points=["AI 摘要仍需人工核对原消息"])
            except Exception:
                pass
        summary = "群聊暂无足够消息。" if not messages else f"群聊共整理 {len(messages)} 条公开消息，最近讨论集中在：{text[-180:]}。"
        return dict(room=int(room), name=data["name"], messages=len(messages), summary=summary,
                    topics=topics, risk_points=["群聊观点仅供讨论，交易前请自行核验行情和风险"])

    async def draft_forum_post(self, game, owner, body):
        body = body or {}
        topic = str(body.get("topic") or "").strip()[:200]
        source = str(body.get("source") or "").strip()[:5000]
        if not topic and not source:
            raise ValueError("请提供主题或素材")
        title = topic or "社区交易观察草稿"
        draft = source or f"围绕“{topic}”整理一条社区讨论，建议补充数据来源、时间范围和风险假设。"
        review_ref = None
        if game and body.get("trade_review") is True:
            review_end = int(time.time())
            review_data = await self.generate_trade_review(game, days=int(body.get("days", 1)),
                                                           pair=body.get("pair", ""), end=review_end)
            title, draft = review_data["title"], review_data["publishable_body"]
            review_ref = dict(days=int(body.get("days", 1)), pair=review_data["stats"]["pair"],
                              end=review_data["stats"]["end"])
        return dict(title=title[:100], body=draft[:6000], tags=body.get("tags") or ["行情观察"],
                    requires_confirmation=True, review=review_ref)

    async def community_daily_summary(self, owner):
        snapshot = self.community.daily_snapshot(owner)
        posts = snapshot["posts"]
        groups = snapshot["groups"]
        hot = [item["title"] for item in posts[:5]]
        return dict(date=time.strftime("%Y-%m-%d"), headline="今日社区行情摘要",
                    hot_posts=hot, active_groups=[item["name"] for item in groups[:5]],
                    market_views=[item["body"] for item in posts[:3]],
                    risk_points=["社区内容是模拟讨论，发布前请核对时间、品种和数据来源"],
                    needs_verification=["行情源是否最新", "观点是否有明确止损和仓位假设"])

    def schedule_diary(self, game, preferences, now):
        config = preferences["diary"]
        if not config["enabled"]:
            return
        gid = game["id"]
        day = due_before(now, config).isoformat()
        store = self.diary.store
        if self.db.execute("SELECT 1 FROM diaries WHERE game=? AND day=?", (gid, day)).fetchone():
            return
        start, end = bounds(day, config["timezone"])
        count = store.count_logs(gid, start, end)
        chat = self.db.execute(
            "SELECT role,content FROM demo_chat WHERE game=? AND created>=? AND created<? ORDER BY id DESC LIMIT ?",
            (gid, start, end, config["history_count"]),
        ).fetchall()
        if count <= config["min_logs"] and not (count == 0 and config["chat_only"] and chat):
            return
        self.timeline.enqueue(game["owner_id"], "diary",
            lambda: self.write_diary(game, config, day, start, end, chat),
            key=("diary", gid, day))

    async def write_diary(self, game, config, day, start, end, chat):
        gid = game["id"]
        store = self.diary.store
        store.enqueue(gid, day, config["timezone"], start, end, time.time())
        self.db.execute("UPDATE diaries SET status='running' WHERE game=? AND day=?", (gid, day))
        try:
            rows = self.db.execute("SELECT data FROM logs WHERE game=? AND created>=? AND created<? ORDER BY seq DESC LIMIT 30",
                                   (gid, start, end)).fetchall()
            reply = await self._agent(game, diary_prompt=workflow("diary") + "日期：" + day +
                                     "。账本记录：" + dump([json.loads(row[0]) for row in reversed(rows)]) +
                                     "。当天聊天：" + dump([dict(row) for row in reversed(chat)]))
            store.finish(gid, day, reply, time.time())
        except (Exception, asyncio.CancelledError):
            self.db.execute("UPDATE diaries SET status='failed',error=? WHERE game=? AND day=?",
                            ("日记生成失败", gid, day))
            raise

    async def agent(self, game, message=None, *, diary_prompt=None, attachments=None, user_image=None, audit_task=None, chat_messages=(), reply_entry=None):
        owner = game["owner_id"]
        if self.timeline.in_task(owner):
            if audit_task not in ("trade_review", "group_summary") or not diary_prompt:
                raise RuntimeError("同一智能体不能等待自己队列中的下一项任务")
            from langchain_core.messages import HumanMessage, SystemMessage

            config = selected(self.profile(owner))
            audit = self.call_logs.callback(owner, audit_task)
            result = await model_client(config, self.model_url, audit=audit).ainvoke([
                SystemMessage(content="按任务要求生成摘要，仅使用提供的资料。"),
                HumanMessage(content=diary_prompt),
            ])
            return result.text
        kind = "private" if message is not None else (audit_task or ("diary" if diary_prompt else "watch"))
        return await self.timeline.run(game["owner_id"], kind,
            lambda: self._agent(game, message, diary_prompt=diary_prompt, attachments=attachments,
                                user_image=user_image, audit_task=audit_task, chat_messages=chat_messages,
                                reply_entry=reply_entry),
            outcome=lambda reply: ("已回复私聊" if reply or attachments else "已跳过") if kind == "private" else "已完成")

    async def _agent(self, game, message=None, *, diary_prompt=None, attachments=None, user_image=None,
                     audit_task=None, chat_messages=(), reply_entry=None, task_instruction="", readonly=False):
        received_at = time.time() if message is not None else None
        gid, owner = game["id"], game["owner_id"]
        lock = lock_for(self, gid)
        async with lock:
            game = self.engine.get(gid)
            settings = self.profile(owner)
            persona = self.agent_persona(owner, settings)
            if received_at is not None:
                local_time = datetime.fromtimestamp(received_at, ZoneInfo("Asia/Shanghai"))
                persona += "\n本轮消息接收时间（北京时间）：" + local_time.isoformat(sep=" ", timespec="seconds")
                user = self.db.execute("SELECT nickname FROM sim_users WHERE id=?", (owner,)).fetchone()
                nickname = user["nickname"] if user else game.get("owner_name", "")
                if nickname:
                    persona += "\n当前用户的资料（仅用作称呼）：" + dump({"昵称": nickname})
            model_settings = selected(settings)
            policy = settings.get("model_policy", {})
            memory = settings.get("chat_memory", {})
            history = []
            recent_user_messages = []
            reasoning = None
            if message is not None:
                rows = self.db.execute(
                    """SELECT role,content,reasoning FROM demo_chat WHERE game=?
                       AND (role<>'user' OR id<?) ORDER BY id DESC LIMIT ?""",
                    (gid, chat_messages[0][0] if chat_messages else 2**63 - 1, -1 if policy.get("rounds") == -1 else 2 * max(
                        100 if policy.get("strategy") == "summary" else 0,
                        policy.get("rounds", 10) + policy.get("discard", 1)))
                ).fetchall()
                history = [{"role": r[0], "content": r[1]} for r in reversed(rows)]
                recent_user_messages = [r[1] for r in rows if r[0] == "user"][:2]
            state = public_snapshot(self, gid, 4)
            revision = state["game"].get("action_revision", 0)
            filled = False

            async def open_trade(side: str, units: int, reason: str) -> str:
                """Submit one paper EUR/USD order. Side is buy or sell. Supply a short reason."""
                nonlocal filled
                if filled:
                    return "本次任务已完成一笔交易"
                if self.engine.get(gid).get("action_revision", 0) != revision:
                    return "账户状态已变化，本次订单未执行"
                try:
                    receipt = await execute(self, gid, "open",
                        dict(pair="EUR/USD", side=side, units=units, reason=reason), already_locked=True)
                except ValueError as exc:
                    return str(exc)
                filled = True
                return dump(receipt)

            if model_settings.get("key") and model_settings.get("model"):
                from langchain.agents import create_agent
                from langchain.agents.middleware import ModelFallbackMiddleware, ModelRetryMiddleware
                from langchain_core.messages import AIMessage, HumanMessage
                from src.agent.context_limit import cap_context, manage_history

                task = audit_task or ("chat" if message is not None else "diary" if diary_prompt else "watch")
                audit = self.call_logs.callback(owner, task)
                model = model_client(model_settings, self.model_url, thinking=True, audit=audit)
                if message is not None:
                    history = [
                        AIMessage(content=row["content"], additional_kwargs={"reasoning_content": row["reasoning"] or ""})
                        if row["role"] == "assistant" else HumanMessage(content=row["content"])
                        for row in reversed(rows)
                    ]
                history = await manage_history(history, policy, selected(settings, "summary"), self.model_url,
                                               audit=self.call_logs.callback(owner, "context_summary"))
                middleware = [TraceTools(), cap_context]
                fallback_id = settings.get("model_roles", {}).get("fallback")
                fallback = next((entry for entry in connections(settings)
                                 if entry["id"] == fallback_id and entry["id"] != model_settings["id"] and entry["key"]), None)
                if fallback:
                    middleware.append(ModelFallbackMiddleware(model_client(fallback, self.model_url, thinking=True, audit=audit)))
                if policy.get("retries"):
                    middleware.append(ModelRetryMiddleware(max_retries=policy["retries"],
                                                           retry_on=retryable, on_failure="error"))
                groups = user_workspace_groups(self, gid, revision=revision, attachments=attachments, user_image=user_image,
                    writable=not readonly and diary_prompt is None and game["status"] == "running"
                    and (message is None or self.accounts.trade_allowed(game)),
                    message_ids=[entry[0] for entry in chat_messages], diary=diary_prompt is not None,
                    handoff=message is None and diary_prompt is None and self.external.settings(owner)["mode"] == "platform")
                tools = flatten(groups)
                instructions = workflow("private") + "\n" + workflow("forum_authorship")
                instructions += "\n近期任务时间线（只用于衔接当前任务）：" + dump(self.timeline.context(owner))
                if attachments is not None:
                    middleware.insert(1, ToolGroups(groups))
                    instructions += "\n" + TOOL_INSTRUCTIONS
                if any(tool_name(tool) == "sticker" for tool in tools):
                    instructions += "\n" + workflow("private_sticker")
                if message is not None and attachments is not None:
                    instructions += "\n当前私聊允许自然结束。如果这一轮无需发消息，只输出 [SKIP]，不附带图片、语音或表情。"
                memory_context = None
                if message is not None and memory.get("enabled", True):
                    recalled = self.memory.recall(owner, message, recent=recent_user_messages)
                    portrait = self.memory.profiles.context(owner, f"private:{gid}", ["self"])
                    seen = {entry["text"] for entry in portrait}
                    memory_context = {
                        "profiles": portrait, "memories": [item for item in recalled if item["text"] not in seen],
                    }
                    if any(memory_context.values()):
                        instructions += "\n" + workflow("private_memory_context") + dump(memory_context)
                image_refs = []
                if message is not None and attachments is not None:
                    instructions += "\n" + workflow("image_library")
                    image_refs = self.images.recent_private(gid)
                    if user_image:
                        image_refs.insert(0, {"reference": "current", "context": "本轮用户发送的图片"})
                if user_image or any(image for _, _, image in chat_messages):
                    instructions += "\n" + workflow("vision_context")
                prompt_persona = social_system_prompt(persona) if message is not None and attachments is not None else persona
                agent = create_agent(model, tools=tools,
                    middleware=middleware,
                    system_prompt=prompt_persona + "\n" + instructions + "\n你的模拟外汇账户快照（话题涉及账户、行情或复盘时参考）：" + dump(state))
                prompt = diary_prompt or (message if message is not None else workflow("watch") +
                    ("\n" + task_instruction if task_instruction else ""))
                if chat_messages and (len(chat_messages) > 1 or chat_messages[0][2]):
                    from src.agent.vision import describe_image

                    incoming = []
                    for message_id, text, image in chat_messages:
                        entry = {"text": text}
                        if image:
                            entry["reference"] = f"private:{message_id}:0"
                            entry["image_description"] = await describe_image(
                                settings, image["src"], text, self.model_url,
                                audit=self.call_logs.callback(owner, "vision"),
                            )
                        incoming.append(entry)
                    prompt = dump({"incoming_messages": incoming, "image_references": image_refs})
                elif user_image:
                    from src.agent.vision import describe_image

                    description = await describe_image(settings, user_image["src"], message, self.model_url,
                                                       audit=self.call_logs.callback(owner, "vision"))
                    prompt = dump({"user_message": message, "image_description": description, "image_references": image_refs})
                elif image_refs:
                    prompt = dump({"user_message": message, "image_references": image_refs})
                if message is not None and (trace := task_trace(owner)):
                    trace.event("memory", "private", {"location": "system_prompt", "enabled": memory_context is not None,
                                                       "injected": memory_context or {}})
                result = await invoke_agent(agent, history + [{"role": "user", "content": prompt}])
                final_message = result["messages"][-1]
                reply = final_message.text[:8000]
                silent = message is not None and attachments is not None and reply.strip() == "[SKIP]"
                if silent:
                    reply = ""
                    attachments.clear()
                if attachments is not None and reply.strip():
                    import re

                    has_table = bool(re.search(r"(?m)^\s*\|?.*---.*\|", reply))
                    market_request = any(word in message for word in ("行情", "走势", "汇率", "复盘", "报价", "数据", "K线", "K 线"))
                    has_image = any(item["kind"] == "image" for item in attachments)
                    if has_table or (market_request and len(reply) > 260) or (has_image and len(reply) > 140):
                        if not has_image or has_table:
                            from src.core.replay_image import render_text_png

                            image = await asyncio.to_thread(render_text_png,
                                dict(title="行情与数据摘要", subtitle="", content=reply[:6000]))
                            attachments.append(dict(kind="image", label="行情与数据摘要",
                                src="data:image/png;base64," + base64.b64encode(image).decode("ascii")))
                        reply = "详细数据见图片。"
                if message is not None and attachments is not None and reply.strip():
                    from src.agent.chat_media import voice_reply

                    audio = await voice_reply(settings, history, message, reply, self.model_url,
                                              audit_factory=lambda task: self.call_logs.callback(owner, task))
                    if audio:
                        attachments.append(audio)
                reasoning = final_message.additional_kwargs.get("reasoning_content")
            elif self.demo:
                if diary_prompt is not None:
                    reply = "离线演示日记：" + diary_prompt[:180]
                elif message is None and (readonly or game["status"] != "running"):
                    reply = "离线演示：已读取账户和行情，当前为只读看盘。"
                elif message is None:
                    reply = "离线演示：" + (await open_trade("buy", 100, "离线演示定时任务")
                                           if not game["positions"] else "已有持仓，继续观察")
                else:
                    previous = next((row["content"] for row in reversed(history) if row["role"] == "user"), "")
                    reply = f"离线演示 · 人格设定：{persona}\n收到：{message}"
                    if previous:
                        reply += f"\n你上一条消息是：{previous}"
            else:
                raise ValueError("请先设置模型和 API Key")
            if diary_prompt is not None:
                return reply
            if message is not None:
                with self.engine.transaction():
                    if not chat_messages:
                        self.db.execute("INSERT INTO demo_chat(game,role,content,created,attachments) VALUES(?,?,?,?,?)",
                            (gid, "user", message, received_at, dump([user_image] if user_image else [])))
                    if reply.strip() or attachments:
                        created = time.time()
                        stored = self.db.execute("INSERT INTO demo_chat(game,role,content,created,reasoning,attachments) VALUES(?,?,?,?,?,?)",
                            (gid, "assistant", reply, created, reasoning, dump(attachments or [])))
                        self.timeline.reference(owner, {"type": "private_message", "game_id": gid,
                                                        "id": stored.lastrowid})
                        if reply_entry is not None:
                            reply_entry.update(id=stored.lastrowid, created=created)
                if memory.get("enabled", True) and memory.get("automatic", True):
                    chosen = next((item for item in connections(settings)
                                   if item["id"] == memory.get("model") and item.get("kind", "chat") == "chat"),
                                  model_settings)
                    if chosen.get("key") and chosen.get("model"):
                        key = (owner, f"private:{gid}")
                        for message_id, text, image in chat_messages:
                            if text.strip() or image:
                                self.memory.schedule(owner, chosen, self.model_url, text or "（发送了一张图片）",
                                                     reply, stream=key[1], event=f"user:{message_id}")
                        self.memory.include_private_history(owner, gid)
                        self.memory.last_seen[key] = asyncio.get_running_loop().time()
                        self.memory.start(*key, chosen, self.model_url)
            elif not readonly and game["status"] == "running":
                await execute(self, gid, "observe", {"reason": reply[:500] or "完成定时看盘"}, already_locked=True)
            return reply

    def reports(self, game):
        if not game:
            return []
        prefs = self.preferences(game.get("owner_id"), game)
        config = prefs["notices"]
        if not config["enabled"]:
            return []
        labels = {"observe": "analysis", "liquidation": "liquidation",
                  "margin_call": "margin_call", "deposit": "deposit", "end": "end"}
        now = time.time()
        reports = []
        def ready(stamp, kind):
            if not config["random_delay"] or kind != "analysis":
                return delivery_time(stamp, config, kind, randomize=False) <= now
            key = (game["id"], stamp, dump(config))
            if len(self.report_times) >= 5000 and key not in self.report_times:
                self.report_times.pop(next(iter(self.report_times)))
            if key not in self.report_times:
                self.report_times[key] = delivery_time(stamp, config, kind)
            return self.report_times[key] <= now
        for row in self.engine.logs(game["id"], limit=50):
            kind = labels.get(row["action"])
            if row["action"] == "close" and row["detail"].get("kind") == "liquidation":
                kind = "liquidation"
            if kind and config[kind] and ready(row["created"], kind):
                reports.append({"title": row["action"], "text": row["reason"], "created": row["created"]})
        if prefs["diary"]["send_to_owner"]:
            for row in self.diary.store.list(game["id"], limit=5, successful_only=True):
                if ready(row["updated"], "diary"):
                    reports.append({"title": "每日日记", "text": row["content"], "created": row["updated"]})
        return sorted(reports, key=lambda item: item["created"], reverse=True)[:15]

    async def refresh(self):
        if self.demo:
            timestamp = time.time()
            self.engine.quotes["EUR/USD"] = dict(bid="1.1000", ask="1.1002", timestamp=timestamp, tradeable=True)
            self.engine.quotes["USD/JPY"] = dict(bid="159.52", ask="159.53", timestamp=timestamp, tradeable=True)
        else:
            await self.market.refresh(self.settings())
        self.dashboard.history.record(self.settings())

    @staticmethod
    def model_url(value):
        value = value or "https://api.openai.com/v1"
        allowed = os.getenv("KURUMI_MODEL_URLS", "").split(",") + [preset["url"] for preset in MODEL_PRESETS]
        url = URL(value)
        local = url.host in {"127.0.0.1", "localhost", "::1"}
        if (value not in {entry.strip() for entry in allowed} or not url.host or url.user is not None
                or url.query_string or url.fragment or (url.scheme != "https" and not (local and url.scheme == "http"))):
            raise ValueError("该接口地址需要管理员加入允许列表")
        return value

    async def prices(self):
        logger = logging.getLogger("kurumi")
        last_failure, last_reported = None, 0.0
        while True:
            try:
                await self.refresh()
            except Exception as exc:
                provider = "demo" if self.demo else source(self.settings())
                host = exc.host if isinstance(exc, ClientConnectorError) else ""
                failure = (provider, host, type(exc).__name__)
                now = time.monotonic()
                self.market.error = market_error(exc)
                if failure != last_failure or now - last_reported >= 60:
                    logger.warning("行情刷新失败 source=%s host=%s error=%s", *failure)
                    last_failure, last_reported = failure, now
            else:
                last_failure = None
                try:
                    for game in self.engine.games():
                        self.engine.tick(game["id"])
                except Exception:
                    logger.exception("模拟结算失败")
            await asyncio.sleep(5)

    async def jobs(self):
        self.task_schedule.recover()
        while True:
            try:
                self.task_schedule.tick()
            except Exception:
                logging.getLogger("kurumi").exception("预约任务调度失败")
            for game in self.engine.games():
                gid = game["id"]
                row = self.db.execute("SELECT due FROM demo_schedule WHERE game=?", (gid,)).fetchone()
                now = time.time()
                prefs = self.preferences(game.get("owner_id"), game)
                try:
                    self.schedule_diary(game, prefs, now)
                except Exception as exc:
                    print("日记任务失败", gid, type(exc).__name__, flush=True)
                if (game["status"] != "running" or not prefs["auto"] or (row and row[0] > now)
                        or ((not self.demo or "preferences" in self.profile(game.get("owner_id"))) and automatic_break(now, prefs["rest"]))):
                    continue
                self.db.execute("INSERT OR REPLACE INTO demo_schedule VALUES(?,?)",
                                (gid, now + (30 if self.demo and "preferences" not in self.profile(game.get("owner_id")) else prefs["interval"])))
                self.enqueue_decision(game)
            await asyncio.sleep(2)

    async def admin_login(self, action, body):
        username, password = body.get("username"), body.get("password")
        if not isinstance(username, str) or not isinstance(password, str):
            raise ValueError("请输入账号和密码")
        username = username.strip()
        if not 1 <= len(username) <= 80 or not 1 <= len(password) <= 128:
            raise ValueError("账号或密码长度无效")
        if self.admin_attempts.locked():
            raise web.HTTPTooManyRequests()
        async with self.admin_attempts:
            if action == "setup":
                if not self.admin_setup or not hmac.compare_digest(
                    str(body.get("token", "")).encode(), self.admin_setup.encode()
                ):
                    raise web.HTTPUnauthorized(reason="初始 Token 无效")
                if len(password) < 12:
                    raise ValueError("管理员密码至少需要 12 个字符")
                salt = secrets.token_bytes(16)
                verifier = await asyncio.to_thread(password_hash, password, salt)
                config = dict(username=username, salt=salt.hex(), verifier=verifier.hex())
                temporary = self.admin_file.with_suffix(".tmp")
                temporary.write_text(dump(config), encoding="utf-8")
                temporary.replace(self.admin_file)
                self.admin_config = config
                self.admin_setup = None
            else:
                config = self.admin_config
                if config and isinstance(config.get("password"), str):
                    authenticated = hmac.compare_digest(password.encode(), config["password"].encode())
                else:
                    salt = bytes.fromhex(config["salt"]) if config else bytes(16)
                    verifier = await asyncio.to_thread(password_hash, password, salt)
                    authenticated = bool(config and hmac.compare_digest(verifier.hex(), config["verifier"]))
                if not config or username != config["username"] or not authenticated:
                    await asyncio.sleep(1)
                    raise web.HTTPUnauthorized(reason="账号或密码错误")
            self.admin = secrets.token_urlsafe(32)
            return {"session": self.admin}

    @staticmethod
    def avatar_value(avatar):
        if avatar in ("avatar.png", "neutral.png", "laugh.png", "user-avatar.svg"):
            return avatar
        from io import BytesIO
        from PIL import Image, ImageOps

        formats = {"data:image/webp;base64": "WEBP", "data:image/png;base64": "PNG",
                   "data:image/jpeg;base64": "JPEG"}
        header, _, encoded = avatar.partition(",") if isinstance(avatar, str) else ("", "", "")
        if header not in formats:
            raise ValueError("头像格式无效")
        try:
            picture = base64.b64decode(encoded, validate=True)
            with Image.open(BytesIO(picture), formats=[formats[header]]) as image:
                if image.width * image.height > 32_000_000:
                    raise ValueError("请先在浏览器中裁剪头像")
                if image.format == "WEBP" and image.size == (96, 96) and len(avatar) <= 24000:
                    image.load()
                    return avatar
                thumbnail = ImageOps.fit(image.convert("RGBA"), (96, 96))
                output = BytesIO()
                thumbnail.save(output, format="WEBP", quality=68)
        except (binascii.Error, OSError, Image.DecompressionBombError):
            raise ValueError("头像格式无效") from None
        return "data:image/webp;base64," + base64.b64encode(output.getvalue()).decode("ascii")

    def unique_name(self, name, owner, *, agent=False):
        used = set()
        for row in self.db.execute("SELECT id,nickname FROM sim_users WHERE id<>?", (owner,)):
            value = self.profile(row["id"]).get("agent_name") if agent else row["nickname"]
            if value:
                used.add(value.casefold())
        candidate = name
        suffix = 1
        while candidate.casefold() in used:
            number = str(suffix)
            candidate = name[:40-len(number)] + number
            suffix += 1
        return candidate

    def validated_agent_name(self, value, owner):
        name = str(value or "").strip()
        if len(name) > 40 or any(ord(char) < 32 for char in name):
            raise ValueError("智能体昵称须为 1～40 字")
        return self.unique_name(name or "久留美", owner, agent=True)

    @staticmethod
    def validated_user_relationship(value):
        if not isinstance(value, str) or len(value) > 40 or (value and not value.isprintable()):
            raise ValueError("关系称呼须为 40 字以内的单行文字")
        return value.strip() or "朋友"

    @staticmethod
    def validated_persona(value):
        text = str(value).strip()
        if len(text) > PERSONA_MAX_LENGTH:
            raise ValueError(f"人格设定最多 {PERSONA_MAX_LENGTH:,} 字符")
        return persona_body(text)

    @staticmethod
    def validated_agent_real_name(value):
        if not isinstance(value, str) or not 1 <= len(value.strip()) <= 40 or not value.isprintable():
            raise ValueError("智能体真实姓名须为 1～40 字的单行文字")
        return value.strip()

    def agent_identity(self, body, owner):
        current = self.profile(owner)
        nickname = self.validated_agent_name(body.get("agent_name", current.get("agent_name")), owner)
        real_name = (self.validated_agent_real_name(body["agent_real_name"]) if "agent_real_name" in body
                     else agent_real_name(current) if current.get("agent_real_name") or current.get("agent_name")
                     else nickname)
        return {"agent_name": nickname, "agent_real_name": real_name,
                "agent_avatar": self.avatar_value(body.get("agent_avatar") or random.choice(("avatar.png", "neutral.png", "laugh.png"))),
                "split_reply": body.get("split_reply") is not False}

    def mode_values(self, body, profile):
        mode = body.get("mode", "platform")
        if mode not in ("platform", "external"):
            raise ValueError("Agent 模式无效")
        auto, diary = body.get("auto", False), body.get("diary", False)
        if type(auto) is not bool or type(diary) is not bool:
            raise ValueError("定时选项无效")
        if mode == "external" and (auto or diary) and not selected(profile).get("key"):
            raise ValueError("请先保存后台模型和 API Key")
        return dict(agent_mode=mode, external_auto=auto, external_diary=diary)

    def app(self, admin):
        @web.middleware
        async def errors(request, handler):
            try:
                if request.method == "POST" and request.content_type != "application/json":
                    raise web.HTTPUnsupportedMediaType()
                origin = request.headers.get("Origin")
                expected = f"{request.scheme}://{request.host}"
                if request.method == "POST" and origin and origin != expected:
                    if admin or origin != self.public_url().rstrip("/"):
                        raise web.HTTPForbidden(reason="请求来源无效")
                response = await handler(request)
                response.headers.update(SECURITY_HEADERS)
                if not admin and request.path in {"/", "/workspace/"}:
                    response.headers["Content-Security-Policy"] += "; img-src 'self' data:; media-src 'self' data:"
                return response
            except ValueError as exc:
                return web.json_response({"error": str(exc)}, status=400)
            except web.HTTPException as exc:
                if exc.status < 400:
                    raise
                return web.json_response({"error": exc.reason}, status=exc.status)
            except Exception as exc:
                incident = secrets.token_hex(4)
                code = getattr(exc, "status_code", None)
                frames = " > ".join(f"{Path(frame.filename).name}:{frame.lineno}:{frame.name}"
                                    for frame in traceback.extract_tb(exc.__traceback__)[-8:])
                logging.getLogger("kurumi").error("[%s] %s %s %s HTTP=%s %s",
                    incident, request.method, request.path, type(exc).__name__, code, frames)
                detail = {400: "模型接口拒绝了请求", 401: "模型 API Key 验证失败",
                          403: "模型 API 无访问权限", 429: "模型接口限流或额度不足"}.get(code)
                if not detail:
                    detail = "模型请求超时" if "Timeout" in type(exc).__name__ else "处理失败"
                return web.json_response({"error": f"{detail}（日志编号 {incident}）"}, status=502)

        app = web.Application(middlewares=[errors], client_max_size=65536)

        async def page(request):
            if admin:
                return web.FileResponse(VIEWS / "admin-login.html")
            self.accounts.access.logout(request.cookies.get("kurumi_session", ""))
            response = web.FileResponse(VIEWS / "portal.html")
            response.del_cookie("kurumi_session")
            return response

        async def asset(request):
            name = request.match_info["file"]
            path = (VIEWS / ASSETS.get(name, name if name in {"standalone-profile.js", "user-preferences.js", "workspace-chat.js", "split-reply.js", "community.js", "agent-mode.js"} else "assets/" + name)).resolve()
            if not path.is_relative_to(VIEWS.resolve()) or not path.is_file():
                raise web.HTTPNotFound()
            return web.FileResponse(path, headers={**SECURITY_HEADERS,
                "Content-Type": "text/javascript"} if path.suffix == ".js" else SECURITY_HEADERS)

        async def dashboard_asset(request):
            path = VIEWS / request.match_info["file"]
            if not path.is_file():
                raise web.HTTPNotFound()
            return web.FileResponse(path, headers={"Content-Type": "text/javascript"} if path.suffix == ".js" else {})

        async def api(request):
            action = request.match_info.get("action")
            if not action and request.match_info.get("subaction"):
                action = "ai-" + request.match_info["subaction"]
            if not action and request.match_info.get("subroute"):
                action = "forum-" + request.match_info["subroute"]
            if action in {"forum", "onboarding", "user-settings", "agent-identity"} and request.method == "POST":
                request = request.clone(client_max_size=160 * 1024)
            body = await request.json() if request.method in {"POST", "DELETE"} and request.content_type == "application/json" else {}
            if admin:
                if request.method == "GET" and action == "login-mode":
                    return web.json_response({"setup": self.admin_config is None})
                if request.method == "POST" and action in {"login", "setup"}:
                    return web.json_response(await self.admin_login(action, body))
                if not hmac.compare_digest(request.headers.get("Authorization", ""), "Bearer " + self.admin):
                    raise web.HTTPUnauthorized()
                if request.method == "GET" and action == "user-credentials":
                    owner = request.query.get("owner", "")
                    username = self.accounts.username(owner)
                    if not username:
                        raise web.HTTPNotFound(reason="用户没有登录账号")
                    return web.json_response(dict(username=username, password=self.accounts.admin_password(owner) or None),
                                             headers={"Cache-Control": "no-store"})
                if action == "market-bulletin":
                    if request.method == "POST":
                        operation = body.get("action", "save")
                        if operation == "save":
                            self.market_bulletin.save(body)
                        elif operation == "check-source":
                            result = await self.market_bulletin.check_source(body.get("source"), body.get("url"))
                            return web.json_response(result, headers={"Cache-Control": "no-store"})
                        elif operation == "scan":
                            if not self.market_bulletin.config()["enabled"]:
                                raise ValueError("请先启用市场播报")
                            await self.market_bulletin.run(operation)
                        elif operation in ("cancel", "retry"):
                            self.market_bulletin.manage_task(operation, body.get("event"))
                        else:
                            raise ValueError("未知播报操作")
                    elif request.method != "GET":
                        raise web.HTTPMethodNotAllowed(request.method, ["GET", "POST"])
                    return web.json_response(self.market_bulletin.admin() | {"presets": BULLETIN_MODEL_PRESETS},
                                             headers={"Cache-Control": "no-store"})
                if request.method == "POST" and action == "create-user":
                    owner = await self.accounts.create_web_user(body.get("username"), body.get("password"))
                    return web.json_response({"id": owner, "username": self.accounts.username(owner)})
                if request.method == "POST" and action == "create":
                    game = self.engine.create(dict(name=str(body.get("name") or "模拟账户"),
                        initial=self.dashboard.initial_funds(), user_nickname=str(body.get("nickname", "")), user_binding={"type": "random", "values": []},
                        _issue_user_token=False, auto=body.get("auto") is True))
                    username, password = await self.accounts.issue(game["owner_id"])
                    self.accounts.allow_trade(game["id"], True)
                    return web.json_response(dict(username=username,password=password))
                if request.method == "GET" and action == "state":
                    return web.json_response(dict(demo=self.demo, portal=os.getenv("KURUMI_PUBLIC_URL",
                        "http://127.0.0.1:8791"), games=[dict(id=g["id"], name=g["name"],
                        owner=g["owner_name"], username=self.accounts.username(g["owner_id"]), auto=g["auto"])
                        for g in self.engine.games()]))
                return web.json_response(await self.dashboard.admin(request, action, body))
            portal = self.dashboard.portal
            if action == "health" and request.method == "GET":
                response = web.json_response({"service": "kurumi-user-portal", "ok": True})
                if origin := request.headers.get("Origin"):
                    if origin == f"http://127.0.0.1:{listen_port(True)}":
                        response.headers["Access-Control-Allow-Origin"] = origin
                return response
            if action == "workspace" and request.method == "GET":
                return await portal.workspace_state(request, controls=self.dashboard.workspace_controls())
            if action == "workspace-control" and request.method == "POST":
                session = portal.identity(request)
                if session["mode"] != "account":
                    raise web.HTTPForbidden()
                game = portal.game(request, session)
                operation = body.get("action")
                if not isinstance(operation, str):
                    raise ValueError("操作无效")
                control = {"pause": "pause", "resume": "pause", "decision": "decision",
                           "borrow": "loan", "repay": "loan"}.get(operation)
                if not control or not self.dashboard.workspace_controls()[control]:
                    raise web.HTTPForbidden(reason="管理员已停用此操作")
                if game["status"] == "ended":
                    raise ValueError("本局已结束")
                if operation == "decision":
                    task = await self.run_decision(game, manual=True)
                    return web.json_response({"message": "看盘已完成", "summary": task})
                return web.json_response(await self.dashboard.command(body | {"game": game["id"]}))
            portal_routes = {"session": portal.session,
                             "review": portal.review, "history": portal.history, "journal": portal.journal,
                             "notebook": portal.notebook, "diaries": portal.diaries, "account": portal.account_state}
            if request.method == "GET" and action in portal_routes:
                return await portal_routes[action](request)
            if request.method == "POST" and action in {"logout", "action"}:
                return await (portal.logout if action == "logout" else portal.action)(request)
            if action == "login" and request.method == "POST":
                username = str(body.get("username", ""))[:300]
                if not self.accounts.rate_limit(username):
                    raise web.HTTPTooManyRequests()
                token = await self.accounts.login(username, body.get("password"))
                if not token:
                    raise web.HTTPUnauthorized()
                response = web.json_response({"ok": True})
                response.set_cookie("kurumi_session", token, httponly=True, samesite="Strict",
                                    secure=self.public_url().startswith("https://"), max_age=28800)
                return response
            if action == "external-tools" and request.method == "POST":
                return web.json_response(await self.external.call(request, body))
            session = self.accounts.access.session(request.cookies.get("kurumi_session", ""))
            if not session:
                raise web.HTTPUnauthorized()
            owner = session["owner"]
            if self.demo and session["mode"] == "account":
                self.community.seed_demo(owner)
            if action == "account-wakeups":
                if session["mode"] != "account":
                    raise web.HTTPForbidden()
                profile = self.profile(owner)
                if request.method == "POST":
                    if any(type(body.get(key)) is not bool for key in ACCOUNT_EVENTS):
                        raise ValueError("账户事件唤起设置无效")
                    profile["account_wakeups"] = {key: body[key] for key in ACCOUNT_EVENTS}
                    self.db.execute("INSERT OR REPLACE INTO demo_profiles VALUES(?,?)", (owner, dump(profile)))
                elif request.method != "GET":
                    raise web.HTTPMethodNotAllowed(request.method, ["GET", "POST"])
                return web.json_response({key: profile.get("account_wakeups", {}).get(key, True)
                                          for key in ACCOUNT_EVENTS}, headers={"Cache-Control": "no-store"})
            if action == "agent-timeline":
                if session["mode"] != "account":
                    raise web.HTTPForbidden()
                profile = self.profile(owner)
                if request.method == "POST":
                    if type(body.get("enabled")) is not bool:
                        raise ValueError("时间线日志开关无效")
                    profile["timeline_debug"] = body["enabled"]
                    self.db.execute("INSERT OR REPLACE INTO demo_profiles VALUES(?,?)", (owner, dump(profile)))
                elif request.method != "GET":
                    raise web.HTTPMethodNotAllowed(request.method, ["GET", "POST"])
                enabled = bool(profile.get("timeline_debug"))
                task_id = request.query.get("task")
                if task_id and enabled:
                    task = self.timeline.report(owner, int(task_id))
                    if not task:
                        raise web.HTTPNotFound()
                    task["trace"] = self.timeline.trace_events(owner, int(task_id))
                    return web.json_response(task, headers={"Cache-Control": "no-store"})
                trace_kind = {"memory": "memory", "memory-generation": "memory-generation"}.get(request.query.get("filter"), "")
                return web.json_response({"enabled": enabled,
                    "tasks": self.timeline.history(owner, limit=50, trace_kind=trace_kind) if enabled else [],
                    "logs": self.timeline.logs(owner) if enabled else []}, headers={"Cache-Control": "no-store"})
            if action == "agent-inbox":
                if session["mode"] != "account":
                    raise web.HTTPForbidden()
                enabled = bool(self.profile(owner).get("show_agent_inbox"))
                if request.method == "POST":
                    if type(body.get("enabled")) is not bool:
                        raise ValueError("邮箱选项无效")
                    enabled = body["enabled"]
                    self.db.execute("INSERT OR REPLACE INTO demo_profiles VALUES(?,?)",
                                    (owner, dump(self.profile(owner) | {"show_agent_inbox": enabled})))
                elif request.method != "GET":
                    raise web.HTTPMethodNotAllowed(request.method, ["GET", "POST"])
                result = {"enabled": enabled}
                if enabled:
                    result.update(self.agent_inbox.history(owner,
                        source=request.query.get("source", ""), before=int(request.query.get("before", "0")),
                        unread=request.query.get("unread") == "1"))
                    result["logs"] = self.agent_inbox.logs(owner)
                return web.json_response(result, headers={"Cache-Control": "no-store"})
            if action == "model-call-logs":
                if session["mode"] != "account":
                    raise web.HTTPForbidden()
                headers = {"Cache-Control": "no-store"}
                if request.method == "POST":
                    if type(body.get("enabled")) is not bool:
                        raise ValueError("日志开关格式无效")
                    self.call_logs.set_enabled(owner, body["enabled"])
                elif request.method == "DELETE":
                    self.call_logs.clear(owner)
                elif request.method != "GET":
                    raise web.HTTPMethodNotAllowed(request.method, ["GET", "POST", "DELETE"])
                entry = request.query.get("id") if request.method == "GET" else None
                if entry is not None:
                    if not entry.isascii() or not entry.isdecimal() or len(entry) > 18:
                        raise ValueError("日志编号无效")
                    detail = self.call_logs.detail(owner, int(entry))
                    if detail is None:
                        raise web.HTTPNotFound()
                    return web.json_response(detail, headers=headers)
                return web.json_response({"enabled": self.call_logs.enabled(owner),
                    "entries": self.call_logs.entries(owner, request.query.get("task", ""))}, headers=headers)
            if action == "market-bulletin" and request.method == "GET":
                return web.json_response(self.market_bulletin.public(), headers={"Cache-Control": "no-store"})
            if action == "agent-images":
                if session["mode"] != "account":
                    raise web.HTTPForbidden()
                image_id = request.query.get("id")
                headers = {"Cache-Control": "private, no-store"}
                if request.method == "GET":
                    if image_id:
                        row = self.images.get(owner, image_id)
                        prefix, payload = row["src"].split(",", 1)
                        return web.Response(body=base64.b64decode(payload),
                            content_type=prefix.split(";", 1)[0][5:], headers=headers)
                    before = int(request.query.get("before", 0))
                    if not 0 <= before < 2**63:
                        raise ValueError("图片分页参数无效")
                    entries = self.images.search(owner, request.query.get("query", "")[:200],
                        request.query.get("category", "")[:40], request.query.get("tag", "")[:32],
                        request.query.get("source", ""), before=before, limit=31)
                    return web.json_response({"entries": entries[:30], "has_more": len(entries) > 30}, headers=headers)
                if not image_id:
                    raise ValueError("请指定图片")
                if request.method == "POST":
                    self.images.update(owner, image_id, body)
                elif request.method == "DELETE":
                    self.images.get(owner, image_id)
                    self.db.execute("DELETE FROM agent_images WHERE owner=? AND id=?", (owner, image_id))
                else:
                    raise web.HTTPMethodNotAllowed(request.method, ["GET", "POST", "DELETE"])
                return web.json_response({"ok": True}, headers=headers)
            if action == "agent-leaderboard":
                if session["mode"] != "account":
                    raise web.HTTPForbidden()
                if request.method != "GET":
                    raise web.HTTPMethodNotAllowed(request.method, ["GET"])
                return web.json_response(standings(self, owner, request.query.get("board", "wealth")),
                                         headers={"Cache-Control": "no-store"})
            if action == "community-summary" and request.method == "GET":
                if session["mode"] != "account":
                    raise web.HTTPForbidden()
                return web.json_response(self.community.summary(owner))
            if action in {"agent-mode", "external-token"}:
                if session["mode"] != "account":
                    raise web.HTTPForbidden()
                config = self.external.settings(owner)
                if action == "external-token":
                    if request.method != "POST" or config["mode"] != "external":
                        raise web.HTTPForbidden()
                    if body.get("action") == "revoke":
                        self.external.revoke(owner)
                        return web.json_response({"ok": True})
                    if body.get("action") != "issue":
                        raise ValueError("无效的令牌操作")
                    return web.json_response(self.external.issue(owner))
                if request.method == "POST":
                    current = self.profile(owner)
                    settings = self.mode_values(body, current)
                    self.db.execute("INSERT OR REPLACE INTO demo_profiles VALUES(?,?)",
                                    (owner, dump(current | settings)))
                    config = self.external.settings(owner)
                    if config["mode"] == "platform":
                        self.external.revoke(owner)
                expiry = self.db.execute("SELECT expires FROM external_tokens WHERE owner=?", (owner,)).fetchone()
                return web.json_response(config | {"username": self.accounts.username(owner),
                    "endpoint": self.public_url().rstrip("/") + "/api/external-tools",
                    "expires": expiry[0] if expiry and expiry[0] > time.time() else None})
            if action == "groups":
                if session["mode"] != "account":
                    raise web.HTTPForbidden()
                room = request.query.get("room", "")
                attachment = request.query.get("attachment")
                if request.method == "GET" and attachment is not None:
                    if not all(value.isascii() and value.isdecimal() and 0 < len(value) <= 18
                               for value in (room, attachment)):
                        raise ValueError("群聊附件编号无效")
                    return web.Response(body=self.community.group_attachment(owner, int(room), int(attachment)),
                                        content_type="image/webp", headers={"Cache-Control": "private, no-store"})
                if body.get("action") == "invite-agent" and self.external.settings(owner)["mode"] != "platform":
                    raise web.HTTPForbidden(reason="请先启用平台智能体")
                result = self.community.groups(owner, request.method, room, body, request.query)
                if result.get("message_id"):
                    self.social.on_message(room, result["message_id"])
                if "entries" in result:
                    result["entries"] = self.social.decorate(result["entries"])
                    for entry in result["entries"]:
                        quote = entry.get("quote")
                        if quote:
                            if quote["owner"] is not None:
                                self.social.decorate([quote])
                                quote.pop("avatar", None)
                            else:
                                quote.pop("owner")
                    result["agents"] = self.social.members(room)
                    result["members"] = [row | self.social.decorate([row | {"is_agent": False}])[0]
                                         for row in result["members"]]
                for group in result.get("groups", []):
                    group["agent_joined"] = self.social.joined(str(group["id"]), owner)
                return web.json_response(result, headers={"Cache-Control": "no-store"})
            if action == "inbox" and session["mode"] == "account":
                if request.method == "POST":
                    if body.get("all") is True:
                        return web.json_response({"ok": True, "marked": self.community.mark_inbox_read_all(owner)})
                    self.community.mark_inbox_read(owner, body.get("kind"), body.get("id"))
                    return web.json_response({"ok": True})
                if request.method == "GET":
                    return web.json_response({"entries": self.social.decorate(
                        self.community.inbox(owner, unread_only=request.query.get("unread") == "1"))},
                                             headers={"Cache-Control": "no-store"})
            if action in {"forum", "group-chat"}:
                if session["mode"] != "account":
                    raise web.HTTPForbidden()
                post = request.query.get("post", "") if action == "forum" else None
                result = self.community.handle(owner, request.method, post, body, request.query)
                if post and request.method == "POST" and "id" in result:
                    thread = self.community.thread(post)
                    if thread["is_agent"] and body.get("body", "").strip():
                        self.remember_community(thread["owner"], owner, body["body"], "forum",
                                                stream=post, event="comment:" + str(result["id"]))
                if post and request.method == "GET":
                    thread = self.community.thread(post, viewer=owner)
                    result["post"] = self.social.decorate([thread | {"mine": thread["owner"] == owner}])[0]
                if request.method == "GET":
                    result["entries"] = self.social.decorate(result["entries"])
                return web.json_response(result)
            games = [g for g in self.engine.games() if g.get("owner_id") == owner]
            game = portal.game(request, session) if request.query.get("game") else next(iter(games), None)
            if action == "ai" or action.startswith("ai-"):
                if session["mode"] != "account":
                    raise web.HTTPForbidden()
                ai_action = action[3:] if action.startswith("ai-") else body.get("action", "")
                if ai_action != "community-daily-summary" and not game:
                    raise ValueError("请先创建模拟交易")
                if ai_action == "trade-review":
                    return web.json_response(await self.generate_trade_review(game, days=body.get("days", 1),
                        pair=body.get("pair", ""), end=body.get("end", 0)))
                if ai_action == "group-summary":
                    return web.json_response(await self.summarize_group(game, owner, body.get("room"), limit=body.get("limit", 80)))
                if ai_action == "forum-draft":
                    return web.json_response(await self.draft_forum_post(game, owner, body))
                if ai_action == "community-daily-summary":
                    return web.json_response(await self.community_daily_summary(owner))
                raise web.HTTPNotFound()
            if action == "forum-share-review" and request.method == "POST":
                if session["mode"] != "account" or not game:
                    raise web.HTTPForbidden()
                generated = await self.generate_trade_review(game, days=body.get("days", 1), pair=body.get("pair", ""), end=body.get("end", 0))
                result = self.community.share_review(owner, generated, body.get("title", ""), body.get("body", ""), body.get("tags"), body.get("pair", ""))
                return web.json_response({"ok": True, "post": result, "review": generated})
            if action == "trading-summary" and request.method == "GET":
                if not game:
                    raise ValueError("请先创建模拟交易")
                return web.json_response(await self.generate_trade_review(game,
                    days=request.query.get("days", 1), pair=request.query.get("pair", ""),
                    end=request.query.get("end", 0)))
            if action == "model-presets" and request.method == "GET":
                return web.json_response(dict(presets=MODEL_PRESETS, persona=DEFAULT_PERSONA, agent_name="久留美", agent_real_name="福贺久留美", user_relationship="朋友",
                                              initial_funds=self.dashboard.initial_funds()))
            if action == "onboarding" and request.method == "POST":
                if games:
                    return web.json_response({"id": games[0]["id"]})
                nickname = body.get("nickname")
                avatar = body.get("avatar") or "user-avatar.svg"
                if not isinstance(nickname, str) or not 1 <= len(nickname.strip()) <= 40 or any(ord(c) < 32 for c in nickname):
                    raise ValueError("请输入 1～40 字的昵称")
                avatar = self.avatar_value(avatar)
                identity = self.agent_identity(body, owner)
                setup = {key: str(body.get(key, "")).strip() for key in ("url", "model", "key")}
                if any(len(value) > 4000 for value in setup.values()):
                    raise ValueError("配置内容过长")
                setup["user_relationship"] = self.validated_user_relationship(body.get("user_relationship", ""))
                setup["persona"] = self.validated_persona(body.get("persona", ""))
                if setup["persona"] == DEFAULT_PERSONA:
                    setup["persona"] = ""
                setup.update(self.mode_values(body, setup))
                if setup["key"]:
                    self.model_url(setup["url"])
                    if not setup["model"]:
                        raise ValueError("请输入模型 ID")
                row = self.db.execute("SELECT token FROM sim_user_tokens WHERE user_id=?", (owner,)).fetchone()
                user_token = row[0] if row else issue_id(self.db, owner)
                self.db.execute("UPDATE sim_users SET nickname=? WHERE id=?", (self.unique_name(nickname.strip(), owner), owner))
                game = self.engine.create(dict(name="外汇日记", initial=self.dashboard.initial_funds(), pairs=list(PAIRS), auto=True,
                    user_binding={"type": "random", "values": [user_token]}))
                self.accounts.allow_trade(game["id"], True)
                self.db.execute("INSERT OR REPLACE INTO demo_profiles VALUES(?,?)",
                                (owner, dump(self.profile(owner) | setup | identity | {"avatar": avatar})))
                return web.json_response({"id": game["id"]})
            current = self.profile(owner)
            if action == "user-settings":
                if session["mode"] != "account":
                    raise web.HTTPForbidden(reason="请先用账号密码登录")
                if request.method == "POST":
                    nickname = body.get("nickname")
                    if not isinstance(nickname, str) or not 1 <= len(nickname.strip()) <= 40:
                        raise ValueError("昵称须为 1～40 字")
                    avatar = self.avatar_value(body.get("avatar") or current.get("avatar", "user-avatar.svg"))
                    username = body.get("username")
                    if username != self.accounts.username(owner) or body.get("new_password"):
                        await self.accounts.change_credentials(owner, request.cookies.get("kurumi_session", ""),
                            username, body.get("password"), body.get("new_password", ""))
                    self.db.execute("UPDATE sim_users SET nickname=? WHERE id=?",
                                    (self.unique_name(nickname.strip(), owner), owner))
                    current = self.profile(owner) | {"avatar": avatar}
                    self.db.execute("INSERT OR REPLACE INTO demo_profiles VALUES(?,?)", (owner, dump(current)))
                elif request.method != "GET":
                    raise web.HTTPMethodNotAllowed(request.method, ["GET", "POST"])
                return web.json_response(dict(username=self.accounts.username(owner),
                    nickname=self.db.execute("SELECT nickname FROM sim_users WHERE id=?", (owner,)).fetchone()[0],
                    avatar=current.get("avatar", "user-avatar.svg")), headers={"Cache-Control":"no-store"})
            if action == "social-settings":
                if session["mode"] != "account":
                    raise web.HTTPForbidden()
                if request.method not in ("GET", "POST"):
                    raise web.HTTPMethodNotAllowed(request.method, ["GET", "POST"])
                config = social_config(current.get("social", {}))
                if request.method == "POST":
                    config = social_config(body)
                    self.db.execute("INSERT OR REPLACE INTO demo_profiles VALUES(?,?)",
                                    (owner, dump(current | {"social": config})))
                return web.json_response(config)
            if action == "chat-debounce":
                if session["mode"] != "account":
                    raise web.HTTPForbidden()
                if request.method not in ("GET", "POST"):
                    raise web.HTTPMethodNotAllowed(request.method, ["GET", "POST"])
                config = debounce_config(body if request.method == "POST" else current.get("chat_debounce", {}))
                if request.method == "POST":
                    self.db.execute("INSERT OR REPLACE INTO demo_profiles VALUES(?,?)",
                                    (owner, dump(current | {"chat_debounce": config})))
                return web.json_response(config)
            if action == "chat-memory":
                if session["mode"] != "account":
                    raise web.HTTPForbidden()
                config = {"enabled": True, "automatic": True} | current.get("chat_memory", {})
                subjects = {"self": self.db.execute(
                    "SELECT nickname FROM sim_users WHERE id=?", (owner,)
                ).fetchone()[0]}
                for user in self.db.execute("SELECT id,nickname FROM sim_users WHERE id<>? ORDER BY nickname", (owner,)):
                    subjects["user:" + user["id"]] = user["nickname"] + "（用户）"
                    agent_name = self.profile(user["id"]).get("agent_name")
                    if agent_name:
                        subjects["agent:" + user["id"]] = agent_name + "（智能体）"
                subjects.update(self.memory.character_subjects())
                models = [item for item in public_models(current) if item["kind"] == "chat"]
                if request.method == "POST":
                    if body.get("action") == "settings":
                        model = body.get("model") or ""
                        if model and not any(item["id"] == model for item in models):
                            raise ValueError("请选择已配置的对话模型")
                        config = dict(enabled=body.get("enabled") is True,
                                      automatic=body.get("automatic") is True, model=model)
                        self.memory.cancel(owner)
                        self.db.execute("INSERT OR REPLACE INTO demo_profiles VALUES(?,?)",
                                        (owner, dump(current | {"chat_memory": config})))
                    else:
                        self.memory.update(owner, body, subjects)
                elif request.method != "GET":
                    raise web.HTTPMethodNotAllowed(request.method, ["GET", "POST"])
                if config["enabled"] and config["automatic"] and self.external.settings(owner)["mode"] == "platform":
                    chosen = next((item for item in connections(current)
                                   if item["id"] == config.get("model") and item.get("kind", "chat") == "chat"
                                   and item.get("key")), selected(current))
                    if chosen.get("key") and chosen.get("model"):
                        for owned_game in games:
                            self.memory.include_private_history(owner, owned_game["id"])
                        for stream, in self.db.execute("""SELECT stream FROM agent_memory_events
                            WHERE owner=? GROUP BY stream""", (owner,)).fetchall():
                            self.memory.start(owner, stream, chosen, self.model_url)
                return web.json_response(dict(config=config, entries=self.memory.list(owner), subjects=subjects,
                                              profiles=self.memory.profiles.list(owner),
                                              user_id=owner, progress=self.memory.progress(owner),
                                              agent=current.get("agent_name", "久留美"), days=MEMORY_DAYS,
                                              models=[{key: item[key] for key in ("id", "label")} for item in models]))
            if action == "stickers":
                if session["mode"] != "account":
                    raise web.HTTPForbidden()
                if request.method == "POST":
                    if body.get("action") == "settings":
                        probability = body.get("probability")
                        if type(probability) not in (float, int) or not 0 <= probability <= 100:
                            raise ValueError("发送概率须在 0～100% 之间")
                        current = current | {"sticker_probability": probability}
                        self.db.execute("INSERT OR REPLACE INTO demo_profiles VALUES(?,?)", (owner, dump(current)))
                    else:
                        self.stickers.update(owner, body)
                elif request.method != "GET":
                    raise web.HTTPMethodNotAllowed(request.method, ["GET", "POST"])
                return web.json_response({"stickers": self.stickers.list(owner),
                                          "categories": self.stickers.categories(owner),
                                          "probability": current.get("sticker_probability", 100)})
            if action == "models":
                if request.method == "POST":
                    current = update_models(current, body, self.model_url)
                    self.mode_values(self.external.settings(owner), current)
                    self.db.execute("INSERT OR REPLACE INTO demo_profiles VALUES(?,?)", (owner, dump(current)))
                elif request.method != "GET":
                    raise web.HTTPMethodNotAllowed(request.method, ["GET", "POST"])
                return web.json_response(dict(models=public_models(current), roles=current.get("model_roles", {}),
                                              policy=current.get("model_policy", {}), presets=MODEL_PRESETS))
            if action == "reply-settings" and request.method == "POST":
                if type(body.get("split_reply")) is not bool:
                    raise ValueError("短句切分设置无效")
                minimum = body.get("split_delay_min", current.get("split_delay_min", 1.5))
                maximum = body.get("split_delay_max", current.get("split_delay_max", 3))
                if not (type(minimum) in (int, float) and type(maximum) in (int, float)
                        and 0 <= minimum <= maximum <= 60):
                    raise ValueError("延迟范围须在 0～60 秒之间，且最短延迟不能超过最长延迟")
                pattern = body.get("split_pattern", "")
                if not isinstance(pattern, str) or len(pattern) > 240:
                    raise ValueError("切分正则最多 240 个字符")
                self.db.execute("INSERT OR REPLACE INTO demo_profiles VALUES(?,?)",
                                (owner, dump(current | {"split_reply": body["split_reply"], "split_pattern": pattern,
                                                       "split_delay_min": minimum, "split_delay_max": maximum})))
                return web.json_response({"ok": True})
            if action == "agent-identity" and request.method == "GET":
                return web.json_response({
                    "agent_name": current.get("agent_name") or "久留美",
                    "agent_real_name": agent_real_name(current),
                    "avatar": current.get("agent_avatar") or "avatar.png",
                }, headers={"Cache-Control": "no-store"})
            if action == "agent-identity" and request.method == "POST":
                body.setdefault("agent_avatar", current.get("agent_avatar") or "avatar.png")
                identity = self.agent_identity(body, owner)
                self.db.execute("INSERT OR REPLACE INTO demo_profiles VALUES(?,?)",
                                (owner, dump(current | {key: value for key, value in identity.items() if key != "split_reply"})))
                return web.json_response(identity | {"avatar": identity["agent_avatar"]})
            if action == "reports" and request.method == "GET":
                return web.json_response({"entries": self.reports(game)})
            if action == "preferences":
                values = self.preferences(owner, game)
                if request.method == "POST":
                    interval, auto = body.get("interval"), body.get("auto")
                    if type(interval) is not int or not 60 <= interval <= 86400 or type(auto) is not bool:
                        raise ValueError("看盘间隔须为 1～1440 分钟")
                    values = dict(interval=interval, auto=auto, rest=rest_settings(body.get("rest")),
                                  diary=diary_settings(body.get("diary")), notices=notice_settings(body.get("notices")))
                    self.db.execute("INSERT OR REPLACE INTO demo_profiles VALUES(?,?)",
                                    (owner, dump(dict(current, preferences=values))))
                    if game:
                        self.db.execute("DELETE FROM demo_schedule WHERE game=?", (game["id"],))
                return web.json_response(values)
            if action == "profile" and request.method == "POST":
                if "models" in current and ({"url", "model", "key", "clear"} & body.keys()):
                    raise ValueError("请在模型与 API 中更新模型连接")
                settings = {k: str(body.get(k, ""))[:4000].strip() for k in ("url", "model", "key") if k in body}
                if "agent_name" in body:
                    settings["agent_name"] = self.validated_agent_name(body["agent_name"], owner)
                    settings["agent_real_name"] = agent_real_name(current)
                if "agent_real_name" in body:
                    settings["agent_real_name"] = self.validated_agent_real_name(body["agent_real_name"])
                if "user_relationship" in body:
                    settings["user_relationship"] = self.validated_user_relationship(body["user_relationship"])
                if "persona" in body:
                    settings["persona"] = self.validated_persona(body["persona"])
                    if settings["persona"] == self.default_persona:
                        settings["persona"] = ""
                if "url" in settings:
                    self.model_url(settings["url"])
                    if (settings["url"] or MODEL_PRESETS[1]["url"]) != (current.get("url") or MODEL_PRESETS[1]["url"]) and current.get("key") and not settings.get("key"):
                        raise ValueError("更换接口地址时，请填写新接口的 Key，或先清除旧 Key")
                if body.get("clear") is True:
                    settings["key"] = ""
                elif "key" in settings and not settings["key"]:
                    settings.pop("key")
                mode = self.external.settings(owner)
                if mode["mode"] == "external" and (mode["auto"] or mode["diary"]):
                    self.mode_values(mode, current | settings)
                self.db.execute("INSERT OR REPLACE INTO demo_profiles VALUES(?,?)", (owner, dump(current | settings)))
                return web.json_response({"ok": True})
            if action == "proactive-chat" and request.method == "GET":
                if session["mode"] != "account":
                    raise web.HTTPForbidden()
                after = request.query.get("after", "0")
                if not after.isascii() or not after.isdecimal() or len(after) > 18:
                    raise ValueError("消息编号无效")
                rows = self.db.execute("""SELECT id,content,created FROM demo_chat
                    WHERE game=? AND proactive=1 AND id>? ORDER BY id LIMIT 20""",
                    (game["id"], int(after))) if game else ()
                return web.json_response({"entries": [dict(row) for row in rows]},
                                         headers={"Cache-Control": "no-store"})
            if action == "chat" and request.method == "POST":
                if self.external.settings(owner)["mode"] == "external":
                    raise web.HTTPForbidden(reason="当前由外部 Agent 负责对话")
                if not game:
                    raise ValueError("请先创建模拟交易")
                message = str(body.get("message", "")).strip()
                user_image = community_image(body["image"]) if body.get("image") is not None else None
                if len(message) > 2000 or not (message or user_image):
                    raise ValueError("请输入 1～2000 个字符或添加一张图片")
                reply, attachments, entry = await self.social.user_chat(game, message, user_image=user_image)
                return web.json_response({"reply": reply, "attachments": attachments, "entry": entry})
            if action == "chat-history" and request.method == "GET" and session["mode"] == "account" and game:
                before = int(request.query.get("before", "0"))
                if not 0 < before < 2**63:
                    raise ValueError("私聊历史游标无效")
                rows = self.db.execute("""SELECT id,role,content,attachments,created FROM demo_chat
                    WHERE game=? AND id<? ORDER BY id DESC LIMIT 21""", (game["id"], before)).fetchall()
                return web.json_response({
                    "entries": [dict(row, attachments=json.loads(row["attachments"])) for row in reversed(rows[:20])],
                    "has_more": len(rows) > 20,
                }, headers={"Cache-Control": "no-store"})
            if action == "chat-message" and request.method == "GET" and session["mode"] == "account" and game:
                entry = self.db.execute("""SELECT id,role,content,attachments,created FROM demo_chat
                    WHERE game=? AND id=? AND role='assistant'""", (game["id"], request.query.get("id", ""))).fetchone()
                if not entry:
                    raise web.HTTPNotFound(reason="私聊消息不存在")
                return web.json_response({"entry": dict(entry, attachments=json.loads(entry["attachments"]))},
                                         headers={"Cache-Control": "no-store"})
            if action == "state" and request.method == "GET":
                rows = self.db.execute("SELECT id,role,content,attachments,created FROM demo_chat WHERE game=? ORDER BY id DESC LIMIT 21",
                                       (game["id"],)).fetchall() if game else []
                return web.json_response(dict(avatar=current.get("avatar", "user-avatar.svg"),
                    nickname=self.db.execute("SELECT nickname FROM sim_users WHERE id=?", (owner,)).fetchone()[0],
                    agent_real_name=agent_real_name(current),
                    split_delay_min=current.get("split_delay_min", 1.5),
                    split_delay_max=current.get("split_delay_max", 3),
                    agent_mode=current.get("agent_mode", "platform"), split_pattern=current.get("split_pattern", ""), split_reply=current.get("split_reply", True), agent_name=current.get("agent_name", "久留美"), agent_avatar=current.get("agent_avatar", "avatar.png"), state=public_snapshot(self, game["id"], 20) if game else None,
                    chat=[dict(row, attachments=json.loads(row["attachments"])) for row in reversed(rows[:20])],
                    chat_has_more=len(rows) > 20, profile={k: current.get(k, "")
                    for k in ("persona", "url", "model")} | {"persona": persona_body(current.get("persona") or ""),
                        "user_relationship": current.get("user_relationship") or "朋友"},
                    has_key=bool(selected(current).get("key")), demo=self.demo))
            raise web.HTTPNotFound()

        app.router.add_get("/", page)
        if admin:
            async def admin_workspace(request):
                return web.FileResponse(VIEWS / "index.html")
            app.router.add_get("/dashboard", admin_workspace)
        app.router.add_route("*", "/api/{action}", api)
        app.router.add_route("*", "/api/ai/{subaction}", api)
        app.router.add_route("*", "/api/forum/{subroute}", api)
        app.router.add_get("/assets/{file:.+}", asset)
        if admin:
            app.router.add_static("/vendor", VIEWS / "vendor")
            app.router.add_get("/{file:[a-zA-Z0-9_.-]+\\.(?:js|css)}",
                               dashboard_asset)
        else:
            app.router.add_get("/workspace/", self.dashboard.portal.workspace)
            app.router.add_get("/workspace/{file:.+}", self.dashboard.portal.workspace_asset)
        return app


def handle_loop_exception(loop, context):
    error = context.get("exception")
    if isinstance(error, ConnectionResetError) and getattr(error, "winerror", None) == 10054:
        frame = error.__traceback__
        # Ignore only the reset raised while Proactor is closing an already-lost connection.
        while frame is not None and frame.tb_next is not None:
            frame = frame.tb_next
        if (frame is not None and frame.tb_frame.f_code.co_name == "_call_connection_lost"
                and frame.tb_frame.f_globals.get("__name__") == "asyncio.proactor_events"):
            logging.getLogger("kurumi").debug("Peer reset a Windows connection during transport cleanup")
            return
    loop.default_exception_handler(context)


async def main():
    asyncio.get_running_loop().set_exception_handler(handle_loop_exception)
    data = Path(os.environ["KURUMI_DATA"]) if os.getenv("KURUMI_DATA") else Path(__file__).resolve().parent / "data"
    data.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger("kurumi")
    logger.setLevel(logging.INFO)
    handler = RotatingFileHandler(data / "kurumi.log", maxBytes=1024 * 1024, backupCount=3, encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    logger.addHandler(handler)
    demo = Demo(data / "ledger.sqlite3")
    demo.demo = os.getenv("KURUMI_LIVE", "1") != "1"
    admin_port, user_port = listen_port(True), listen_port(False)
    runners = []
    tasks = []
    try:
        for admin, port in [(True, admin_port), (False, user_port)]:
            runner = web.AppRunner(demo.app(admin), access_log=None)
            runners.append(runner)
            await runner.setup()
            await web.TCPSite(runner, "127.0.0.1", port).start()
        print(f"管理员入口 http://127.0.0.1:{admin_port}/", flush=True)
        if demo.admin_setup:
            print(f"管理员首次设置 http://127.0.0.1:{admin_port}/#setup={demo.admin_setup}", flush=True)
            print(f"管理员首次设置 Token：{demo.admin_setup}", flush=True)
        print(f"用户入口 http://127.0.0.1:{user_port}/", flush=True)
        tasks = [asyncio.create_task(demo.prices()), asyncio.create_task(demo.jobs()), asyncio.create_task(demo.social.patrol()), asyncio.create_task(demo.market_bulletin.loop())]
        await asyncio.Event().wait()
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        for runner in reversed(runners):
            await runner.cleanup()
        await demo.social.close()
        await demo.timeline.close()
        await demo.memory.close()
        demo.db.close()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass

