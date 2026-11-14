"""HTTP adapters for the original Kurumi dashboard."""
import asyncio
import time
from types import SimpleNamespace

from aiohttp import web

from src.core.alerts import notices
from src.core.availability import status
from src.core.engine import PAIRS, dump
from src.core.journal import journal
from src.core.replay import review
from src.core.trading import lock_for
from src.diary.settings import settings as diary_settings
from src.diary.store import DiaryStore
from src.market_data.history import History, PERIODS, identity
from src.market_data.market import probe
from src.market_data.settings import merge_settings, public_settings
from src.portal.server import Portal, settings as portal_settings
from src.users import ownership, policy


class Dashboard:
    def __init__(self, demo):
        self.demo = demo
        self.engine = demo.engine
        self.history = History(self.engine, demo.market)
        self.download_lock = asyncio.Lock()
        self.diary = SimpleNamespace(store=DiaryStore(self.engine))
        self.portal = Portal(demo)
        self.portal.access = demo.accounts.access
        self.portal.accounts = demo.accounts
        self.engine.db.executescript("""
        CREATE TABLE IF NOT EXISTS dashboard_settings(name TEXT PRIMARY KEY, data TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS decision_jobs(
          id INTEGER PRIMARY KEY, game TEXT REFERENCES games(id) ON DELETE CASCADE,
          trigger TEXT, events TEXT, state TEXT, created REAL, updated REAL,
          error TEXT DEFAULT '', result TEXT DEFAULT '');
        """)

    def get(self, name, default):
        import json

        row = self.engine.db.execute("SELECT data FROM dashboard_settings WHERE name=?", (name,)).fetchone()
        return json.loads(row[0]) if row else default

    def put(self, name, data):
        self.engine.db.execute("INSERT OR REPLACE INTO dashboard_settings VALUES(?,?)", (name, dump(data)))

    async def page(self, config, pair, period, before, limit):
        if not self.demo.demo:
            return await self.history.page(config, pair, period, before, limit)
        bars = self.history.cached(identity(config), pair, period, before or int(time.time()) + 1, limit)
        return dict(bars=bars, warning="当前为演示行情", has_more=False)

    async def download_history(self):
        if self.demo.demo:
            raise ValueError("预下载需要启用真实行情")
        if self.download_lock.locked():
            raise ValueError("正在预下载，请稍候")
        config = dict(self.demo.settings(), history_source="market")
        async with self.download_lock:
            async def download(pair, period):
                total = 0
                cursor = 0
                for _ in range(2):
                    page = await self.history.page(config, pair, period, cursor, 200, refresh=not cursor)
                    total += len(page["bars"])
                    cursor = page["next_before"]
                    if page["warning"] or not cursor:
                        break
                return total, page["warning"] or ("暂无历史数据" if not total else "")

            series = [(pair, period) for pair in PAIRS for period in PERIODS]
            results = await asyncio.gather(*(download(*item) for item in series), return_exceptions=True)
            failures = []
            count = 0
            for (pair, period), result in zip(series, results):
                if isinstance(result, Exception):
                    failures.append(f"{pair} {period}：{result}")
                else:
                    count += result[0]
                    if result[1]:
                        failures.append(f"{pair} {period}：{result[1]}")
            return dict(message=f"预下载完成，公共缓存已覆盖 {count} 根 K 线"
                                + (f"。{len(failures)} 组未完整获取：{failures[0]}" if failures else "。"))

    async def records(self, request, action, game):
        query = request.query
        gid = game["id"]
        if action == "review":
            return await review(self.demo, gid, query.get("pair", ""),
                                int(query.get("days", 1)), int(query.get("end", 0)), period=query.get("period", ""))
        if action == "history":
            source = query.get("history_source", self.demo.settings().get("history_source", "market"))
            if source not in {"market", "frankfurter"}:
                raise ValueError("历史来源无效")
            return await self.page(dict(self.demo.settings(), history_source=source),
                                   query.get("pair", ""), query.get("period", "5m"),
                                   int(query.get("before", 0)), min(500, max(1, int(query.get("limit", 200)))))
        if action == "journal":
            return journal(self.engine, gid, page=int(query.get("page", 1)),
                           before=int(query.get("before", 0)), include_text=True,
                           pair=query.get("pair", ""), source=query.get("source", "all"),
                           filter=query.get("filter", "all"), sort=query.get("sort", "newest"))
        if action == "diaries":
            entries = self.diary.store.list(gid, query.get("before", ""), 10, successful_only=True)
            return dict(entries=[dict(row) for row in entries],
                        next_before=entries[-1]["day"] if entries else "")
        if action == "notebook":
            entries = self.diary.store.list(gid, "", 31, successful_only=True)
            return dict(plan=game.get("plan", ""), diaries=[dict(row) for row in entries])
        return notices(self.engine, gid, int(query["after"]) if "after" in query else None)

    def admin_state(self, request):
        demo = self.demo
        users = [dict(user, portal_username=demo.accounts.username(user["id"])) for user in self.engine.users()]
        games = self.engine.games()
        gid = request.query.get("game")
        game = next((item for item in games if item["id"] == gid), None)
        snapshot = self.engine.snapshot(gid) if game else None
        if snapshot:
            snapshot["game"]["portal_trade"] = demo.accounts.trade_allowed(game)
        latest = self.engine.db.execute(
            "SELECT state,error,result FROM decision_jobs WHERE game=? ORDER BY id DESC LIMIT 1", (gid,)
        ).fetchone()
        decision = None
        if latest:
            import json

            decision = dict(state=latest[0], error=latest[1],
                            last_summary=json.loads(latest[2] or "{}").get("summary", ""))
        settings = demo.settings()
        return dict(demo=demo.demo, portal=dict(enabled=True, active=True, bind="127.0.0.1",
                    origin=demo.public_url(), port=8791, error=""),
                    games=games, pairs=PAIRS, missing_instance=bool(gid and not game),
                    workspace=dict(users=users, mode=self.get("workspace_mode",
                                   dict(multiplayer=True, main_user_id=users[0]["id"] if users else "")),
                                   dashboard_admin=True, ownership_enforced=True),
                    current=snapshot,
                    providers=[dict(id="user", name="用户自己的模型")],
                    personas=[dict(id="user", name="用户自己的人格")],
                    llm_settings=dict(mode="platform", provider_id="user"),
                    diary_settings=diary_settings(), connection=public_settings(settings),
                    instance_access=policy.effective(self.engine, game) if game else None,
                    market_identity=identity(settings), decision=decision, **status(None))

    async def admin(self, request, action, body):
        if request.method == "GET":
            if action == "dashboard":
                return self.admin_state(request)
            if action in {"review", "history", "journal", "notebook", "diaries", "alerts"}:
                gid = request.query.get("game")
                if not gid and action in {"alerts", "history"}:
                    if action == "alerts":
                        return {"events": []}
                    return await self.records(request, action, {"id": ""})
                return await self.records(request, action, self.engine.get(gid))
        if request.method == "POST":
            if action == "settings":
                self.put("market", merge_settings(self.demo.settings(), body))
                return {"ok": True}
            if action == "download-history":
                return await self.download_history()
            if action == "connection-test":
                return await probe(merge_settings(self.demo.settings(), body))
            if action == "portal-settings":
                config = portal_settings(body)
                if config["port"] != 8791 or config["bind"] != "127.0.0.1":
                    raise ValueError("独立版用户面板固定监听 127.0.0.1:8791")
                self.put("portal", config)
                return {"ok": True}
            if action == "decision":
                return {"message": await self.demo.run_decision(self.engine.get(body.get("game")))}
            if action == "command":
                return await self.command(body)
        raise web.HTTPNotImplemented(reason="该功能尚未接入独立版")

    async def command(self, body):
        name, gid = body.get("action"), body.get("game")
        params = body.get("params") or {}
        if name == "create":
            options = dict(body.get("options", {}))
            binding = ownership.normalize(options.get("user_binding", {"type": "random", "values": []}))
            if binding["type"] != "random":
                raise ValueError("独立版使用网页用户账号")
            if binding["values"]:
                owner = ownership.owner_for_token(self.engine.db, binding["values"][0])
                if any(game.get("owner_id") == owner for game in self.engine.games()):
                    raise ValueError("该用户已有交易工作区")
            options["user_binding"] = binding
            game = self.engine.create(options)
            user, password = await self.demo.accounts.issue(game["owner_id"])
            self.demo.accounts.allow_trade(game["id"], True)
            return dict(id=game["id"], username=user, password=password,
                        message=f"登录名 {user} · 初始密码 {password}" if password else "交易实例已创建")
        if name == "workspace_mode":
            uid = body.get("main_user_id", "")
            if uid and not any(user["id"] == uid for user in self.engine.users()):
                raise ValueError("用户不存在")
            self.put("workspace_mode", dict(multiplayer=body.get("multiplayer") is True,
                     main_user_id=uid, role_settings=body.get("role_settings")))
        elif name == "update_user":
            policy.update(self.engine, body.get("user_id"), body.get("nickname"),
                          body.get("policy"), body.get("notifications"))
        elif name == "delete_user":
            owner = body.get("user_id")
            games = [game["id"] for game in self.engine.games() if game.get("owner_id") == owner]
            self.engine.delete_virtual_user(owner)
            self.engine.db.execute("DELETE FROM demo_profiles WHERE owner=?", (owner,))
            self.engine.db.executemany("DELETE FROM demo_chat WHERE game=?", [(gid,) for gid in games])
            self.engine.db.executemany("DELETE FROM demo_schedule WHERE game=?", [(gid,) for gid in games])
        elif name == "set_portal_password":
            await self.demo.accounts.set_password(body.get("user_id"), body.get("password"))
        elif name == "delete":
            async with lock_for(self.demo, gid):
                self.engine.delete(gid)
                self.engine.db.execute("DELETE FROM demo_chat WHERE game=?", (gid,))
                self.engine.db.execute("DELETE FROM demo_schedule WHERE game=?", (gid,))
        elif name == "update_pairs":
            self.engine.update_pairs(gid, body.get("pairs"))
        elif name == "bind":
            game = self.engine.get(gid)
            game.update(auto=body.get("auto") is True, interval=max(30, int(body.get("interval", 3600))))
            self.engine.save(game)
            self.demo.accounts.allow_trade(gid, body.get("portal_trade") is True)
        elif name in {"open", "close", "protect", "deposit", "pause", "resume", "borrow", "repay", "start", "plan", "notes"}:
            async with lock_for(self.demo, gid):
                return self.engine.action(gid, name, params, "human", body.get("request_id"))
        else:
            raise web.HTTPNotImplemented(reason="该操作尚未接入独立版")
        return {"ok": True}
