"""Standalone Kurumi demo."""
import asyncio
import hmac
import json
import os
import secrets
import time
from pathlib import Path

from aiohttp import web
from yarl import URL

from src.core.engine import Engine, dump
from src.core.trading import execute, public_snapshot, lock_for
from src.market_data.market import Market
from src.portal.access import PortalAccess
from src.portal.accounts import PortalAccounts


class Demo:
    def __init__(self, path):
        self.engine = Engine(path)
        self.db = self.engine.db
        self.db.executescript("""
        CREATE TABLE IF NOT EXISTS demo_profiles(owner TEXT PRIMARY KEY, data TEXT);
        CREATE TABLE IF NOT EXISTS demo_chat(id INTEGER PRIMARY KEY, game TEXT, role TEXT, content TEXT);
        CREATE TABLE IF NOT EXISTS demo_schedule(game TEXT PRIMARY KEY, due REAL);
        """)
        self.accounts = PortalAccounts(PortalAccess(self.engine))
        self.market = Market(self.engine)
        self.instance_locks = {}
        self.admin = secrets.token_urlsafe(32)
        self.demo = os.getenv("KURUMI_LIVE") != "1"

    def profile(self, owner):
        row = self.db.execute("SELECT data FROM demo_profiles WHERE owner=?", (owner,)).fetchone()
        return json.loads(row[0]) if row else {}

    async def agent(self, game, message=None):
        gid, owner = game["id"], game["owner_id"]
        lock = lock_for(self, gid)
        async with lock:
            settings = self.profile(owner)
            persona = settings.get("persona") or "你是久留美，使用中文交流。"
            history = []
            if message is not None:
                rows = self.db.execute(
                    "SELECT role,content FROM demo_chat WHERE game=? ORDER BY id DESC LIMIT 20", (gid,)
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
                model = ChatOpenAI(model=settings["model"], api_key=settings["key"],
                                   base_url=endpoint, timeout=20, max_retries=0)
                agent = create_agent(model, tools=[open_trade] if message is None else [],
                    middleware=[cap_context],
                    system_prompt=persona + "\n仅使用虚拟账本，工具返回成功才算成交。当前账户：" + dump(state))
                result = await agent.ainvoke({"messages": history + [{"role": "user", "content":
                    message if message is not None else "检查行情和账户，按需提交一笔模拟订单或说明观望理由。"}]},
                    config={"recursion_limit": 10})
                reply = result["messages"][-1].text[:8000]
            elif self.demo:
                if message is None:
                    reply = "离线演示：" + (await open_trade("buy", 100, "离线演示定时任务")
                                           if not game["positions"] else "已有持仓，继续观察")
                else:
                    previous = next((row["content"] for row in reversed(history) if row["role"] == "user"), "")
                    reply = f"离线演示 · 人格设定：{persona}\n收到：{message}"
                    if previous:
                        reply += f"\n你上一条消息是：{previous}"
            else:
                raise ValueError("请先设置模型和 API Key")
            if message is not None:
                with self.engine.transaction():
                    self.db.executemany("INSERT INTO demo_chat(game,role,content) VALUES(?,?,?)",
                        [(gid, "user", message), (gid, "assistant", reply)])
            else:
                await execute(self, gid, "observe", {"reason": reply[:500] or "完成定时看盘"}, already_locked=True)
            return reply

    async def refresh(self):
        if self.demo:
            self.engine.quotes["EUR/USD"] = dict(bid="1.1000", ask="1.1002",
                                                  timestamp=time.time(), tradeable=True)
        else:
            await self.market.refresh({})

    @staticmethod
    def model_url(value):
        value = value or "https://api.openai.com/v1"
        allowed = os.getenv("KURUMI_MODEL_URLS", "https://api.openai.com/v1").split(",")
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
                if game["status"] != "running" or not game["auto"] or (row and row[0] > now):
                    continue
                self.db.execute("INSERT OR REPLACE INTO demo_schedule VALUES(?,?)",
                                (gid, now + (30 if self.demo else 3600)))
                try:
                    await self.agent(game)
                except Exception as exc:
                    print("定时任务失败", gid, type(exc).__name__, flush=True)
            await asyncio.sleep(2)

    def app(self, admin):
        @web.middleware
        async def errors(request, handler):
            try:
                if request.method == "POST" and request.content_type != "application/json":
                    raise web.HTTPUnsupportedMediaType()
                return await handler(request)
            except ValueError as exc:
                return web.json_response({"error": str(exc)}, status=400)
            except web.HTTPException:
                raise
            except Exception as exc:
                print(type(exc).__name__, flush=True)
                return web.json_response({"error": "处理失败，请检查服务器和模型配置"}, status=502)

        app = web.Application(middlewares=[errors], client_max_size=32768)

        async def page(request):
            return web.FileResponse(Path(__file__).with_name("admin.html" if admin else "portal.html"),
                                    headers={"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"})

        async def api(request):
            action = request.match_info["action"]
            body = await request.json() if request.method == "POST" else {}
            if admin:
                if not hmac.compare_digest(request.headers.get("Authorization", ""), "Bearer " + self.admin):
                    raise web.HTTPUnauthorized()
                if request.method == "POST" and action == "create":
                    game = self.engine.create(dict(name=str(body.get("name") or "模拟账户"),
                        user_nickname=str(body.get("nickname", "")), user_binding={"type": "random", "values": []},
                        _issue_user_token=False, auto=body.get("auto") is True))
                    username, password = await self.accounts.issue(game["owner_id"])
                    return web.json_response(dict(username=username,password=password))
                if request.method == "GET" and action == "state":
                    return web.json_response(dict(demo=self.demo, portal=os.getenv("KURUMI_PUBLIC_URL",
                        "http://127.0.0.1:8791"), games=[dict(id=g["id"], name=g["name"],
                        owner=g["owner_name"], username=self.accounts.username(g["owner_id"]), auto=g["auto"])
                        for g in self.engine.games()]))
                raise web.HTTPNotFound()
            if action == "login" and request.method == "POST":
                username = str(body.get("username", ""))[:300]
                if not self.accounts.rate_limit(username):
                    raise web.HTTPTooManyRequests()
                token = await self.accounts.login(username, body.get("password"))
                if not token:
                    raise web.HTTPUnauthorized()
                response = web.json_response({"ok": True})
                response.set_cookie("kurumi_session", token, httponly=True, samesite="Strict",
                                    secure=os.getenv("KURUMI_PUBLIC_URL", "").startswith("https://"), max_age=28800)
                return response
            session = self.accounts.access.session(request.cookies.get("kurumi_session", ""))
            if not session:
                raise web.HTTPUnauthorized()
            owner = session["owner"]
            games = [g for g in self.engine.games() if g.get("owner_id") == owner]
            if not games:
                raise web.HTTPNotFound()
            game = games[0]
            current = self.profile(owner)
            if action == "profile" and request.method == "POST":
                settings = {k: str(body.get(k, ""))[:4000].strip() for k in ("persona", "url", "model", "key")}
                self.model_url(settings["url"])
                if not settings["key"] and not body.get("clear"):
                    settings["key"] = current.get("key", "")
                self.db.execute("INSERT OR REPLACE INTO demo_profiles VALUES(?,?)", (owner, dump(settings)))
                return web.json_response({"ok": True})
            if action == "chat" and request.method == "POST":
                message = str(body.get("message", "")).strip()
                if not 1 <= len(message) <= 2000:
                    raise ValueError("消息须为 1～2000 个字符")
                return web.json_response({"reply": await self.agent(game, message)})
            if action == "state" and request.method == "GET":
                rows = self.db.execute("SELECT role,content FROM demo_chat WHERE game=? ORDER BY id DESC LIMIT 20",
                                       (game["id"],)).fetchall()
                return web.json_response(dict(state=public_snapshot(self, game["id"], 20),
                    chat=[dict(row) for row in reversed(rows)], profile={k: current.get(k, "")
                    for k in ("persona", "url", "model")}, has_key=bool(current.get("key")), demo=self.demo))
            raise web.HTTPNotFound()

        app.router.add_get("/", page)
        app.router.add_route("*", "/api/{action}", api)
        return app


async def main():
    data = Path(os.getenv("KURUMI_DATA", "data"))
    data.mkdir(parents=True, exist_ok=True)
    demo = Demo(data / "ledger.sqlite3")
    runners = []
    tasks = []
    try:
        for admin, port in [(True, 8790), (False, 8791)]:
            runner = web.AppRunner(demo.app(admin), access_log=None)
            runners.append(runner)
            await runner.setup()
            await web.TCPSite(runner, "127.0.0.1", port).start()
        print(f"管理员入口 http://127.0.0.1:8790/#token={demo.admin}", flush=True)
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

