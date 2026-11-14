"""External Agent credentials and explicitly supported tools."""
import hashlib
import secrets
import time
from datetime import date, datetime
from zoneinfo import ZoneInfo

from aiohttp import web

from src.core.engine import PAIRS
from src.diary.settings import bounds
from src.diary.store import redact
from src.market_data.history import PERIODS


class ExternalAgent:
    def __init__(self, app):
        self.app = app
        self.db = app.db
        self.db.execute("""CREATE TABLE IF NOT EXISTS external_tokens(
            owner TEXT PRIMARY KEY REFERENCES sim_users(id) ON DELETE CASCADE,
            digest TEXT NOT NULL UNIQUE, expires REAL NOT NULL)""")

    def settings(self, owner):
        profile = self.app.profile(owner)
        return {"mode": profile.get("agent_mode", "platform"),
                "auto": profile.get("external_auto", False),
                "diary": profile.get("external_diary", False)}

    def revoke(self, owner):
        self.db.execute("DELETE FROM external_tokens WHERE owner=?", (owner,))

    def issue(self, owner):
        token = "kext_" + secrets.token_urlsafe(32)
        expires = time.time() + 604800
        self.db.execute("INSERT OR REPLACE INTO external_tokens VALUES (?,?,?)",
                        (owner, hashlib.sha256(token.encode()).hexdigest(), expires))
        return {"token": token, "expires": expires}

    async def call(self, request, body):
        scheme, _, token = request.headers.get("Authorization", "").partition(" ")
        if scheme.lower() != "bearer" or len(token) != 48 or not token.startswith("kext_"):
            raise web.HTTPUnauthorized()
        row = self.db.execute("SELECT owner FROM external_tokens WHERE digest=? AND expires>?",
                              (hashlib.sha256(token.encode()).hexdigest(), time.time())).fetchone()
        if not row or self.settings(row[0])["mode"] != "external":
            raise web.HTTPUnauthorized()
        owner = row[0]
        action = body.get("action", "review")
        names = {"quotes": {}, "history": {"pair": "EUR/USD", "period": "1h", "before": 0},
                 "chart": {"pair": "EUR/USD", "period": "1h"},
                 "write_diary": {"day": "YYYY-MM-DD", "content": "日记正文"}}
        if self.settings(owner)["diary"]:
            names.pop("write_diary")
        if action == "review":
            fields = {
                "pair": {"type": "string", "enum": list(PAIRS), "default": "EUR/USD"},
                "period": {"type": "string", "enum": list(PERIODS), "default": "1h"},
                "before": {"type": "integer", "minimum": 0, "default": 0},
                "day": {"type": "string", "format": "date"},
                "content": {"type": "string", "minLength": 1, "maxLength": 6000},
            }
            return {"method": "POST", "headers": {"Authorization": "Bearer <token>",
                    "Content-Type": "application/json"}, "tools": [
                        {"name": name, "parameters": {"type": "object",
                         "properties": {key: fields[key] for key in args},
                         "required": list(args) if name == "write_diary" else []},
                         "example": {"action": "call", "tool": name, "arguments": args}}
                        for name, args in names.items()]}
        name, args = body.get("tool"), body.get("arguments", {})
        if action != "call" or not isinstance(name, str) or name not in names:
            raise web.HTTPForbidden(reason="工具不可用")
        if not isinstance(args, dict) or set(args) - names[name].keys():
            raise ValueError("工具参数无效")
        if name == "quotes":
            return {"quotes": {pair: self.app.engine.quotes[pair] for pair in PAIRS if pair in self.app.engine.quotes}}
        game = next((g for g in self.app.engine.games() if g.get("owner_id") == owner), None)
        if not game:
            raise web.HTTPNotFound(reason="交易工作区不存在")
        gid = game["id"]
        if name == "write_diary":
            day, content = args.get("day"), args.get("content")
            zone = self.app.preferences(owner, game)["diary"]["timezone"]
            if not isinstance(day, str) or date.fromisoformat(day).isoformat() != day or day > datetime.now(ZoneInfo(zone)).date().isoformat():
                raise ValueError("请输入有效的日记日期")
            if not isinstance(content, str) or not 1 <= len(content.strip()) <= 6000:
                raise ValueError("正文须为 1～6000 字")
            start, end = bounds(day, zone)
            inserted = self.db.execute("""INSERT OR IGNORE INTO diaries
                (game,day,zone,start,end,status,content,updated) VALUES(?,?,?,?,?,'success',?,?)""",
                (gid, day, zone, start, end, redact(content.strip()), time.time())).rowcount
            return {"day": day, "created": bool(inserted)}
        pair, period = args.get("pair", "EUR/USD"), args.get("period", "1h")
        if pair not in PAIRS or not isinstance(period, str) or period not in PERIODS:
            raise ValueError("货币对或周期无效")
        data = await self.app.history.page(dict(self.app.settings(), history_source="market"),
                                           pair, period, args.get("before", 0), 100)
        if name == "history":
            return data
        from asyncio import to_thread
        from base64 import b64encode
        from src.core.replay_image import render_png

        bars = data["bars"]
        if not bars:
            raise ValueError("暂无可绘制的历史行情")
        chart = dict(pair=pair, period=period, candles=bars, events=[], marks=[],
                     start=bars[0]["time"], end=bars[-1]["time"], days=7)
        png = await to_thread(render_png, chart, "candles")
        return {"mime_type": "image/png", "base64": b64encode(png).decode("ascii")}
