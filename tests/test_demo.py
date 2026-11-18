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
from src.agent.mcp.registry import user_workspace_groups


class DemoFlow(unittest.IsolatedAsyncioTestCase):
    async def test_login_profiles_chat_and_scheduled_fill(self):
        requests = []

        async def model(request):
            body = await request.json()
            requests.append((request.headers["Authorization"], body))
            names = {tool["function"]["name"] for tool in body.get("tools", [])}
            completed_calls = {m.get("tool_call_id") for m in body["messages"] if m["role"] == "tool"}
            output = dict(role="assistant", content="已收到")
            if "open_trade" in names and "call_trade" not in completed_calls:
                output = dict(role="assistant", content=None, tool_calls=[dict(
                    id="call_trade", type="function", function=dict(name="open_trade",
                    arguments='{"side":"buy","units":100,"reason":"测试看盘"}'))])
            elif "price_history" in names and "load_tool_group" in names and "call_load" not in completed_calls:
                output = dict(role="assistant", content=None, tool_calls=[dict(
                    id="call_load", type="function", function=dict(name="load_tool_group",
                    arguments='{"categories":["trading"]}'))])
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
                            persona = name + "的人格" + "个" * 9995 + "。"
                            self.assertEqual(len(persona), 10000)
                            saved = await user.post("/api/profile", json=dict(persona=persona,
                                url=url, model="local-test", key="test-key-"+name.encode().hex()))
                            self.assertEqual(saved.status, 200, await saved.text())
                            too_long = await user.post("/api/profile", json=dict(persona=persona + "超"))
                            self.assertEqual(too_long.status, 400, await too_long.text())
                            if name == "甲":
                                initial_trace = await (await user.get("/api/agent-timeline")).json()
                                self.assertFalse(initial_trace["enabled"])
                                response = await user.post("/api/agent-timeline", json={"enabled": True})
                                self.assertEqual(response.status, 200, await response.text())
                            for message in ("第一条", "第二条"):
                                reply = await user.post("/api/chat", json=dict(message=message))
                                self.assertEqual(reply.status, 200, await reply.text())
                            self.assertIn("第一条", str(requests[-1][1]["messages"]))
                            state = await (await user.get("/api/state")).json()
                            self.assertNotIn("test-key", str(state))
                            self.assertEqual(state["profile"]["persona"], persona)
                            self.assertTrue(any(persona in message["content"] for message in requests[-1][1]["messages"]
                                                if isinstance(message.get("content"), str)))
                            self.assertEqual(len(state["chat"]), 4)
                            if name == "甲":
                                trace_list = await (await user.get("/api/agent-timeline", params={"filter": "memory"})).json()
                                self.assertTrue(trace_list["tasks"])
                                detail = await user.get("/api/agent-timeline", params={"task": trace_list["tasks"][0]["id"]})
                                self.assertEqual(detail.status, 200, await detail.text())
                                events = (await detail.json())["trace"]
                                memory_event = next(event for event in events if event["kind"] == "memory")
                                self.assertEqual(memory_event["request"]["location"], "system_prompt")
                                self.assertTrue(any(event["kind"] == "model" for event in events))
                            if name == "乙":
                                gid = state["state"]["game"]["id"]
                                demo.db.executemany("INSERT INTO demo_chat(game,role,content) VALUES(?,?,?)",
                                                    ((gid, "user", str(i)) for i in range(45)))
                                latest = await (await user.get("/api/state")).json()
                                before = latest["chat"][0]["id"]
                                history = []
                                while before:
                                    page = await user.get("/api/chat-history", params={"game": gid, "before": before})
                                    self.assertEqual(page.status, 200, await page.text())
                                    older = await page.json()
                                    self.assertLessEqual(len(older["entries"]), 20)
                                    history = older["entries"] + history
                                    before = older["entries"][0]["id"] if older["has_more"] else 0
                                combined = history + latest["chat"]
                                self.assertEqual(len(combined), 49)
                                self.assertEqual([item["id"] for item in combined], sorted({item["id"] for item in combined}))
                                await demo.refresh()
                                demo.engine.action(gid, "pause", {})
                                trade = await user.post("/api/action", params={"game": gid}, json=dict(
                                    action="open", request_id="0123456789abcdef",
                                    params=dict(pair="EUR/USD", side="buy", units="100", reason="手动交易")))
                                self.assertEqual(trade.status, 200, await trade.text())
                                self.assertEqual(len(demo.engine.get(gid)["positions"]), 1)
                                placed = await user.post("/api/action", params={"game": gid}, json={
                                    "action": "place_limit_order", "request_id": "1123456789abcdef",
                                    "params": {"pair": "EUR/USD", "side": "buy", "units": "100",
                                               "limit_price": "0.5", "reason": "limit"},
                                })
                                self.assertEqual(placed.status, 200, await placed.text())
                                order_id = (await placed.json())["order_id"]
                                workspace = await user.get("/api/workspace", params={"game": gid})
                                self.assertEqual((await workspace.json())["current"]["game"]["orders"][0]["id"], order_id)
                                cancelled = await user.post("/api/action", params={"game": gid}, json={
                                    "action": "cancel_limit_order", "request_id": "2123456789abcdef",
                                    "params": {"order_id": order_id, "reason": "cancel"},
                                })
                                self.assertEqual(cancelled.status, 200, await cancelled.text())
                                demo.engine.action(gid, "resume", {})
                                game = demo.engine.get(gid)
                                groups = user_workspace_groups(demo, gid, writable=True, revision=game["action_revision"])
                                place = next(tool for tool in groups["trading"] if tool.__name__ == "place_limit_order")
                                self.assertIn('"state": "waiting"', await place("sell", 100, "2", "automated test"))
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
                        failed_job = decisions["已收到"]["job"]
                        demo.db.execute(
                            "UPDATE decision_jobs SET state='failed', result='', error=? WHERE id=?",
                            ("看盘任务未完成", failed_job),
                        )
                        replay = await review(demo, game["id"], pair="USD/JPY", include_candles=False)
                        recorded = next(event for event in replay["events"] if event["id"] == f"job-{failed_job}")
                        self.assertEqual(recorded["outcome"], "已成交")
                        self.assertEqual(recorded["operations"], 1)
                        self.assertEqual(recorded["text"], "")
                        self.assertEqual(decisions["旧记录"]["outcome"], "已完成，无成交记录")
                        self.assertEqual(decisions["旧记录"]["operations"], 0)
                        self.assertEqual(requests[-1][0], "Bearer test-key-"+"甲".encode().hex())
                    finally:
                        job.cancel()
                        await asyncio.gather(job, return_exceptions=True)
                        await demo.timeline.close()
                    self.assertEqual((await admin.get("/api/state")).status, 401)

