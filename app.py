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
from pathlib import Path

from aiohttp import web
from yarl import URL

from src.core.engine import Engine, PAIRS, dump
from src.core.trading import execute, public_snapshot, lock_for
from src.market_data.market import Market
from src.portal.access import PortalAccess
from src.users.ownership import issue_id
from src.portal.accounts import PortalAccounts, password_hash
from dashboard_api import Dashboard
from src.agent.tool import workspace_tools
from src.portal.community import Community
from src.agent.decision_settings import settings as rest_settings
from src.core.availability import automatic_break
from src.diary.settings import settings as diary_settings, bounds, due_before
from src.notifications.settings import validate as notice_settings, delivery_time
from src.portal.server import ASSETS, SECURITY_HEADERS

DEFAULT_PERSONA = "你是久留美，说话轻松俏皮，但对数字很认真。先说明判断依据和风险，行情不足时坦率观望。只做模拟交易，成交以账本记录为准。"
MODEL_PRESETS = [
    dict(name="DeepSeek", url="https://api.deepseek.com", model="deepseek-flash"),
    dict(name="OpenAI", url="https://api.openai.com/v1", model="gpt-4o-mini"),
    dict(name="Gemini", url="https://generativelanguage.googleapis.com/v1beta/openai/", model="gemini-3.8-flash"),
]

VIEWS = Path(__file__).with_name("views") / "dashboard"


class Demo:
    def __init__(self, path):
        self.engine = Engine(path)
        self.db = self.engine.db
        self.community = Community(self.db)
        self.db.executescript("""
        CREATE TABLE IF NOT EXISTS demo_profiles(owner TEXT PRIMARY KEY, data TEXT);
        CREATE TABLE IF NOT EXISTS demo_chat(id INTEGER PRIMARY KEY, game TEXT, role TEXT, content TEXT);
        CREATE TABLE IF NOT EXISTS demo_schedule(game TEXT PRIMARY KEY, due REAL);
        """)
        if "created" not in {row["name"] for row in self.db.execute("PRAGMA table_info(demo_chat)")}:
            self.db.execute("ALTER TABLE demo_chat ADD COLUMN created REAL")
        if "reasoning" not in {row["name"] for row in self.db.execute("PRAGMA table_info(demo_chat)")}:
            self.db.execute("ALTER TABLE demo_chat ADD COLUMN reasoning TEXT")
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
        return os.getenv("KURUMI_PUBLIC_URL") or self.dashboard.get("portal", {}).get("origin") or "http://127.0.0.1:8791"

    async def run_decision(self, game):
        now = time.time()
        cursor = self.db.execute(
            "INSERT INTO decision_jobs(game,trigger,events,state,created,updated) VALUES(?,?,?,?,?,?)",
            (game["id"], "scheduled", "[]", "running", now, now),
        )
        try:
            reply = await self.agent(game)
        except BaseException:
            self.db.execute("UPDATE decision_jobs SET state='failed',updated=?,error=? WHERE id=?",
                            (time.time(), "看盘任务未完成", cursor.lastrowid))
            raise
        self.db.execute("UPDATE decision_jobs SET state='success',updated=?,result=? WHERE id=?",
                        (time.time(), dump({"summary": reply}), cursor.lastrowid))
        return reply

    def profile(self, owner):
        row = self.db.execute("SELECT data FROM demo_profiles WHERE owner=?", (owner,)).fetchone()
        return json.loads(row[0]) if row else {}

    def preferences(self, owner, game=None):
        values = self.profile(owner).get("preferences", {})
        return dict(interval=values.get("interval", (game or {}).get("interval", 3600)),
                    auto=values.get("auto", bool((game or {}).get("auto"))),
                    rest=rest_settings(values.get("rest")),
                    diary=diary_settings(values.get("diary")),
                    notices=notice_settings(values.get("notices") or (game or {}).get("notifications")))

    async def write_diary(self, game, preferences, now):
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
        store.enqueue(gid, day, config["timezone"], start, end, now)
        self.db.execute("UPDATE diaries SET status='running' WHERE game=? AND day=?", (gid, day))
        try:
            rows = self.db.execute("SELECT data FROM logs WHERE game=? AND created>=? AND created<? ORDER BY seq DESC LIMIT 30",
                                   (gid, start, end)).fetchall()
            reply = await self.agent(game, diary_prompt="请用你的人格写这一日的交易日记。日期：" + day +
                                     "。账本记录：" + dump([json.loads(row[0]) for row in reversed(rows)]) +
                                     "。当天聊天：" + dump([dict(row) for row in reversed(chat)]))
            store.finish(gid, day, reply, time.time())
        except Exception:
            self.db.execute("UPDATE diaries SET status='failed',error=? WHERE game=? AND day=?",
                            ("日记生成失败", gid, day))

    async def agent(self, game, message=None, *, diary_prompt=None, attachments=None):
        gid, owner = game["id"], game["owner_id"]
        lock = lock_for(self, gid)
        async with lock:
            settings = self.profile(owner)
            persona = (settings.get("persona") or DEFAULT_PERSONA) + "\n你的显示名称是：" + settings.get("agent_name", "久留美")
            history = []
            reasoning = None
            if message is not None:
                rows = self.db.execute(
                    "SELECT role,content,reasoning FROM demo_chat WHERE game=? ORDER BY id DESC LIMIT 20", (gid,)
                ).fetchall()
                history = [{"role": r[0], "content": r[1]} for r in reversed(rows)]
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

            if settings.get("key") and settings.get("model"):
                from langchain.agents import create_agent
                from langchain_openai import ChatOpenAI
                from src.agent.context_limit import cap_context

                endpoint = self.model_url(settings.get("url", ""))
                model_class = ChatOpenAI
                model_options = {}
                if URL(endpoint).host == "api.deepseek.com":
                    from langchain_core.messages import AIMessage, HumanMessage
                    from src.agent.deepseek_chat import DeepSeekChat

                    model_class = DeepSeekChat
                    model_options = dict(use_responses_api=False, disable_streaming=True,
                                         extra_body={"thinking": {"type": "enabled"}})
                    if message is not None:
                        history = [
                            AIMessage(content=row["content"], additional_kwargs={"reasoning_content": row["reasoning"] or ""})
                            if row["role"] == "assistant" else HumanMessage(content=row["content"])
                            for row in reversed(rows)
                        ]
                model = model_class(model=settings["model"], api_key=settings["key"],
                                    base_url=endpoint, timeout=45, max_retries=0, **model_options)
                tools = workspace_tools(self, gid, revision=revision, attachments=attachments,
                    writable=diary_prompt is None and game["status"] == "running"
                    and (message is None or self.accounts.trade_allowed(game)))
                agent = create_agent(model, tools=tools,
                    middleware=[cap_context],
                    system_prompt=persona + "\n仅使用虚拟账本，工具返回成功才算成交。聊天默认用几个短句回复。长篇数据优先用 send_review_image 发送图表或文字图片，附简短说明。用户要求发图时使用 send_review_image。聊天中只有用户明确要求交易或写入时才调用写入工具。论坛和群聊是多人可见的，未经明确要求不要发帖、评论、发消息或变更成员关系，不要发布私聊或账户私有信息。当前账户：" + dump(state))
                result = await agent.ainvoke({"messages": history + [{"role": "user", "content":
                    diary_prompt or (message if message is not None else "检查行情和账户，按需提交一笔模拟订单或说明观望理由。")} ]},
                    config={"recursion_limit": 10})
                final_message = result["messages"][-1]
                reply = final_message.text[:8000]
                if attachments is not None:
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
                if model_class is not ChatOpenAI:
                    reasoning = final_message.additional_kwargs.get("reasoning_content")
            elif self.demo:
                if diary_prompt is not None:
                    reply = "离线演示日记：" + diary_prompt[:180]
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
                    self.db.executemany("INSERT INTO demo_chat(game,role,content,created,reasoning,attachments) VALUES(?,?,?,?,?,?)",
                        [(gid, "user", message, time.time(), None, "[]"), (gid, "assistant", reply, time.time(), reasoning, dump(attachments or []))])
            else:
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
            self.engine.quotes["EUR/USD"] = dict(bid="1.1000", ask="1.1002",
                                                  timestamp=time.time(), tradeable=True)
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
        while True:
            try:
                await self.refresh()
                for game in self.engine.games():
                    self.engine.tick(game["id"])
            except Exception as exc:
                print("行情或结算失败", type(exc).__name__, flush=True)
            await asyncio.sleep(5)

    async def jobs(self):
        while True:
            for game in self.engine.games():
                gid = game["id"]
                row = self.db.execute("SELECT due FROM demo_schedule WHERE game=?", (gid,)).fetchone()
                now = time.time()
                prefs = self.preferences(game.get("owner_id"), game)
                try:
                    await self.write_diary(game, prefs, now)
                except Exception as exc:
                    print("日记任务失败", gid, type(exc).__name__, flush=True)
                if (game["status"] != "running" or not prefs["auto"] or (row and row[0] > now)
                        or ((not self.demo or "preferences" in self.profile(game.get("owner_id"))) and automatic_break(now, prefs["rest"]))):
                    continue
                self.db.execute("INSERT OR REPLACE INTO demo_schedule VALUES(?,?)",
                                (gid, now + (30 if self.demo and "preferences" not in self.profile(game.get("owner_id")) else prefs["interval"])))
                try:
                    await self.run_decision(game)
                except Exception as exc:
                    print("定时任务失败", gid, type(exc).__name__, flush=True)
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
                salt = bytes.fromhex(config["salt"]) if config else bytes(16)
                verifier = await asyncio.to_thread(password_hash, password, salt)
                if not config or username != config["username"] or not hmac.compare_digest(
                    verifier.hex(), config["verifier"]
                ):
                    await asyncio.sleep(1)
                    raise web.HTTPUnauthorized(reason="账号或密码错误")
            self.admin = secrets.token_urlsafe(32)
            return {"session": self.admin}

    @staticmethod
    def avatar_value(avatar):
        if avatar not in ("avatar.png", "neutral.png", "laugh.png", "user-avatar.svg"):
            prefix = "data:image/webp;base64,"
            if not isinstance(avatar, str) or not avatar.startswith(prefix) or len(avatar) > 24000:
                raise ValueError("头像格式无效或图片过大")
            try:
                picture = base64.b64decode(avatar[len(prefix):], validate=True)
            except (ValueError, binascii.Error):
                raise ValueError("头像格式无效") from None
            if picture[:4] != b"RIFF" or picture[8:12] != b"WEBP":
                raise ValueError("头像格式无效")
        return avatar

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

    def agent_identity(self, body, owner):
        name = str(body.get("agent_name") or "").strip()
        if len(name) > 40 or any(ord(char) < 32 for char in name):
            raise ValueError("智能体名称须为 1～40 字")
        return {"agent_name": self.unique_name(name or "久留美", owner, agent=True),
                "agent_avatar": self.avatar_value(body.get("agent_avatar") or random.choice(("avatar.png", "neutral.png", "laugh.png"))),
                "split_reply": body.get("split_reply") is not False}

    def app(self, admin):
        @web.middleware
        async def errors(request, handler):
            try:
                if request.method == "POST" and request.content_type != "application/json":
                    raise web.HTTPUnsupportedMediaType()
                origin = request.headers.get("Origin")
                expected = f"{request.scheme}://{request.host}"
                if request.method == "POST" and origin and origin != expected:
                    if admin or origin != self.public_url():
                        raise web.HTTPForbidden(reason="请求来源无效")
                response = await handler(request)
                response.headers.update(SECURITY_HEADERS)
                if not admin and request.path in {"/", "/workspace/"}:
                    response.headers["Content-Security-Policy"] += "; img-src 'self' data:"
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
            path = (VIEWS / ASSETS.get(name, name if name in {"standalone-profile.js", "user-preferences.js", "workspace-chat.js", "community.js"} else "assets/" + name)).resolve()
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
            action = request.match_info["action"]
            body = await request.json() if request.method == "POST" else {}
            if admin:
                if request.method == "GET" and action == "login-mode":
                    return web.json_response({"setup": self.admin_config is None})
                if request.method == "POST" and action in {"login", "setup"}:
                    return web.json_response(await self.admin_login(action, body))
                if not hmac.compare_digest(request.headers.get("Authorization", ""), "Bearer " + self.admin):
                    raise web.HTTPUnauthorized()
                if request.method == "POST" and action == "create-user":
                    owner = await self.accounts.create_web_user(body.get("username"), body.get("password"))
                    return web.json_response({"id": owner, "username": self.accounts.username(owner)})
                if request.method == "POST" and action == "create":
                    game = self.engine.create(dict(name=str(body.get("name") or "模拟账户"),
                        user_nickname=str(body.get("nickname", "")), user_binding={"type": "random", "values": []},
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
                    if origin == "http://127.0.0.1:8790":
                        response.headers["Access-Control-Allow-Origin"] = origin
                return response
            portal_routes = {"session": portal.session, "workspace": portal.workspace_state,
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
            session = self.accounts.access.session(request.cookies.get("kurumi_session", ""))
            if not session:
                raise web.HTTPUnauthorized()
            owner = session["owner"]
            if action == "groups":
                if session["mode"] != "account":
                    raise web.HTTPForbidden()
                return web.json_response(self.community.groups(owner, request.method, request.query.get("room", ""), body))
            if action in {"forum", "group-chat"}:
                if session["mode"] != "account":
                    raise web.HTTPForbidden()
                post = request.query.get("post", "") if action == "forum" else None
                result = self.community.handle(owner, request.method, post, body)
                if post and request.method == "GET":
                    result["post"] = self.community.thread(post)
                return web.json_response(result)
            games = [g for g in self.engine.games() if g.get("owner_id") == owner]
            game = portal.game(request, session) if request.query.get("game") else next(iter(games), None)
            if action == "model-presets" and request.method == "GET":
                return web.json_response(dict(presets=MODEL_PRESETS, persona=DEFAULT_PERSONA))
            if action == "onboarding" and request.method == "POST":
                if games:
                    return web.json_response({"id": games[0]["id"]})
                nickname = body.get("nickname")
                avatar = body.get("avatar") or "user-avatar.svg"
                if not isinstance(nickname, str) or not 1 <= len(nickname.strip()) <= 40 or any(ord(c) < 32 for c in nickname):
                    raise ValueError("请输入 1～40 字的昵称")
                avatar = self.avatar_value(avatar)
                identity = self.agent_identity(body, owner)
                setup = {key: str(body.get(key, "")).strip() for key in ("persona", "url", "model", "key")}
                if any(len(value) > 4000 for value in setup.values()):
                    raise ValueError("配置内容过长")
                setup["persona"] = setup["persona"] or DEFAULT_PERSONA
                if setup["key"]:
                    self.model_url(setup["url"])
                    if not setup["model"]:
                        raise ValueError("请输入模型 ID")
                row = self.db.execute("SELECT token FROM sim_user_tokens WHERE user_id=?", (owner,)).fetchone()
                user_token = row[0] if row else issue_id(self.db, owner)
                self.db.execute("UPDATE sim_users SET nickname=? WHERE id=?", (self.unique_name(nickname.strip(), owner), owner))
                game = self.engine.create(dict(name="外汇日记", initial="1000", pairs=list(PAIRS), auto=True,
                    user_binding={"type": "random", "values": [user_token]}))
                self.accounts.allow_trade(game["id"], True)
                self.db.execute("INSERT OR REPLACE INTO demo_profiles VALUES(?,?)",
                                (owner, dump(self.profile(owner) | setup | identity | {"avatar": avatar})))
                return web.json_response({"id": game["id"]})
            current = self.profile(owner)
            if action == "reply-settings" and request.method == "POST":
                if type(body.get("split_reply")) is not bool:
                    raise ValueError("短句切分设置无效")
                self.db.execute("INSERT OR REPLACE INTO demo_profiles VALUES(?,?)",
                                (owner, dump(current | {"split_reply": body["split_reply"]})))
                return web.json_response({"ok": True})
            if action == "agent-identity" and request.method == "POST":
                identity = self.agent_identity(body, owner)
                self.db.execute("INSERT OR REPLACE INTO demo_profiles VALUES(?,?)",
                                (owner, dump(current | {key: value for key, value in identity.items() if key != "split_reply"})))
                return web.json_response(identity)
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
                settings = {k: str(body.get(k, ""))[:4000].strip() for k in ("persona", "url", "model", "key") if k in body}
                if "url" in settings:
                    self.model_url(settings["url"])
                    if (settings["url"] or MODEL_PRESETS[1]["url"]) != (current.get("url") or MODEL_PRESETS[1]["url"]) and current.get("key") and not settings.get("key"):
                        raise ValueError("更换接口地址时，请填写新接口的 Key，或先清除旧 Key")
                if body.get("clear") is True:
                    settings["key"] = ""
                elif "key" in settings and not settings["key"]:
                    settings.pop("key")
                self.db.execute("INSERT OR REPLACE INTO demo_profiles VALUES(?,?)", (owner, dump(current | settings)))
                return web.json_response({"ok": True})
            if action == "chat" and request.method == "POST":
                if not game:
                    raise ValueError("请先创建模拟交易")
                message = str(body.get("message", "")).strip()
                if not 1 <= len(message) <= 2000:
                    raise ValueError("消息须为 1～2000 个字符")
                attachments = []
                reply = await self.agent(game, message, attachments=attachments)
                return web.json_response({"reply": reply, "attachments": attachments})
            if action == "state" and request.method == "GET":
                rows = self.db.execute("SELECT role,content,attachments FROM demo_chat WHERE game=? ORDER BY id DESC LIMIT 20",
                                       (game["id"],)).fetchall() if game else []
                return web.json_response(dict(avatar=current.get("avatar", "user-avatar.svg"),
                    nickname=self.db.execute("SELECT nickname FROM sim_users WHERE id=?", (owner,)).fetchone()[0],
                    split_reply=current.get("split_reply", True), agent_name=current.get("agent_name", "久留美"), agent_avatar=current.get("agent_avatar", "avatar.png"), state=public_snapshot(self, game["id"], 20) if game else None,
                    chat=[dict(row, attachments=json.loads(row["attachments"])) for row in reversed(rows)], profile={k: current.get(k, "")
                    for k in ("persona", "url", "model")}, has_key=bool(current.get("key")), demo=self.demo))
            raise web.HTTPNotFound()

        app.router.add_get("/", page)
        if admin:
            async def admin_workspace(request):
                return web.FileResponse(VIEWS / "index.html")
            app.router.add_get("/dashboard", admin_workspace)
        app.router.add_route("*", "/api/{action}", api)
        app.router.add_get("/assets/{file:.+}", asset)
        if admin:
            app.router.add_static("/vendor", VIEWS / "vendor")
            app.router.add_get("/{file:[a-zA-Z0-9_.-]+\\.(?:js|css)}",
                               dashboard_asset)
        else:
            app.router.add_get("/workspace/", self.dashboard.portal.workspace)
            app.router.add_get("/workspace/{file:.+}", self.dashboard.portal.workspace_asset)
        return app


async def main():
    data = Path(os.environ["KURUMI_DATA"]) if os.getenv("KURUMI_DATA") else Path(__file__).resolve().parent / "data"
    data.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger("kurumi")
    logger.setLevel(logging.INFO)
    handler = RotatingFileHandler(data / "kurumi.log", maxBytes=1024 * 1024, backupCount=3, encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    logger.addHandler(handler)
    demo = Demo(data / "ledger.sqlite3")
    demo.demo = os.getenv("KURUMI_LIVE", "1") != "1"
    runners = []
    tasks = []
    try:
        for admin, port in [(True, 8790), (False, 8791)]:
            runner = web.AppRunner(demo.app(admin), access_log=None)
            runners.append(runner)
            await runner.setup()
            await web.TCPSite(runner, "127.0.0.1", port).start()
        print("管理员入口 http://127.0.0.1:8790/", flush=True)
        if demo.admin_setup:
            print(f"管理员首次设置 http://127.0.0.1:8790/#setup={demo.admin_setup}", flush=True)
            print(f"管理员首次设置 Token：{demo.admin_setup}", flush=True)
        print("用户入口 http://127.0.0.1:8791/", flush=True)
        tasks = [asyncio.create_task(demo.prices()), asyncio.create_task(demo.jobs())]
        await asyncio.Event().wait()
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        for runner in reversed(runners):
            await runner.cleanup()
        demo.db.close()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass

