import asyncio
import os
import tempfile
import time
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch

from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from app import Demo
from src.core.replay import review


class DemoFlow(unittest.IsolatedAsyncioTestCase):
    async def test_login_profiles_chat_and_scheduled_fill(self):
        requests = []

        async def model(request):
            body = await request.json()
            requests.append((request.headers["Authorization"], body))
            has_tool_result = any(m["role"] == "tool" for m in body["messages"])
            output = dict(role="assistant", content="已收到")
            if body.get("tools") and not has_tool_result:
                output = dict(role="assistant", content=None, tool_calls=[dict(
                    id="call_1", type="function", function=dict(name="open_trade",
                    arguments='{"side":"buy","units":100,"reason":"测试看盘"}'))])
            return web.json_response(dict(id="test", object="chat.completion", created=1,
                model="local-test", choices=[dict(index=0, message=output,
                finish_reason="tool_calls" if output.get("tool_calls") else "stop")]))

        fake = web.Application()
        fake.router.add_post("/v1/chat/completions", model)
        with tempfile.TemporaryDirectory() as directory, ExitStack() as stack:
            demo = Demo(Path(directory) / "ledger.sqlite3")
            stack.callback(demo.db.close)
            async with TestServer(fake) as endpoint, TestClient(TestServer(demo.app(True))) as admin:
                url = str(endpoint.make_url("/v1"))
                headers = {"Authorization": "Bearer " + demo.admin}
                with patch.dict(os.environ, {"KURUMI_MODEL_URLS": url}):
                    async with TestClient(TestServer(demo.app(False))) as user:
                        self.assertEqual((await user.get("/api/state")).status, 401)
                        for name in ("甲", "乙"):
                            created = await admin.post("/api/create", headers=headers, json={
                                "nickname": name, "name": name, "auto": name == "甲"})
                            self.assertEqual(created.status, 200, await created.text())
                            login = await user.post("/api/login", json=await created.json())
                            self.assertEqual(login.status, 200, await login.text())
                            state = await (await user.get("/api/state")).json()
                            self.assertEqual(state["state"]["game"]["name"], name)
                            self.assertFalse(state["has_key"])
                            saved = await user.post("/api/profile", json=dict(persona=name+"的人格",
                                url=url, model="local-test", key="test-key-"+name.encode().hex()))
                            self.assertEqual(saved.status, 200, await saved.text())
                            for message in ("第一条", "第二条"):
                                reply = await user.post("/api/chat", json=dict(message=message))
                                self.assertEqual(reply.status, 200, await reply.text())
                            self.assertIn("第一条", str(requests[-1][1]["messages"]))
                            state = await (await user.get("/api/state")).json()
                            self.assertNotIn("test-key", str(state))
                            self.assertEqual(len(state["chat"]), 4)
                            if name == "乙":
                                gid = state["state"]["game"]["id"]
                                await demo.refresh()
                                demo.engine.action(gid, "pause", {})
                                trade = await user.post("/api/action", params={"game": gid}, json=dict(
                                    action="open", request_id="0123456789abcdef",
                                    params=dict(pair="EUR/USD", side="buy", units="100", reason="手动交易")))
                                self.assertEqual(trade.status, 200, await trade.text())
                                self.assertEqual(len(demo.engine.get(gid)["positions"]), 1)
                    await demo.refresh()
                    job = asyncio.create_task(demo.jobs())
                    try:
                        for _ in range(100):
                            game = next(g for g in demo.engine.games() if g["name"] == "甲")
                            completed = demo.db.execute(
                                "SELECT 1 FROM decision_jobs WHERE game=? AND state='success'", (game["id"],)
                            ).fetchone()
                            if game["positions"] and completed:
                                break
                            await asyncio.sleep(.05)
                        self.assertEqual(len(game["positions"]), 1)
                        now = time.time()
                        demo.db.execute(
                            "INSERT INTO decision_jobs(game,trigger,events,state,created,updated,result) "
                            "VALUES(?,?,?,'success',?,?,?)",
                            (game["id"], "scheduled", "[]", now, now, '{"summary":"旧记录"}'),
                        )
                        game = demo.engine.get(game["id"])
                        game["pairs"].append("USD/JPY")
                        demo.engine.save(game)
                        replay = await review(demo, game["id"], pair="USD/JPY", include_candles=False)
                        decisions = {event["text"]: event for event in replay["events"] if event["action"] == "review"}
                        self.assertIn("已收到", decisions)
                        self.assertEqual(decisions["已收到"]["outcome"], "已成交")
                        self.assertEqual(decisions["已收到"]["operations"], 1)
                        self.assertEqual(decisions["旧记录"]["outcome"], "已完成，无成交记录")
                        self.assertEqual(decisions["旧记录"]["operations"], 0)
                        self.assertEqual(requests[-1][0], "Bearer test-key-"+"甲".encode().hex())
                    finally:
                        job.cancel()
                        await asyncio.gather(job, return_exceptions=True)
                        await demo.timeline.close()
                    self.assertEqual((await admin.get("/api/state")).status, 401)

