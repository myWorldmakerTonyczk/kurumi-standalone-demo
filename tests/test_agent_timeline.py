import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from langchain_core.messages import AIMessage
from src.agent.mcp.community.group_chat import build as group_chat_tool
from src.core.engine import dump
from src.users.ownership import issue_id

from aiohttp.test_utils import TestClient, TestServer

from app import Demo


class AgentTimelineTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        root = tempfile.TemporaryDirectory()
        self.addCleanup(root.cleanup)
        self.app = Demo(Path(root.name) / "data.sqlite3")
        self.addCleanup(self.app.db.close)
        self.addAsyncCleanup(self.app.timeline.close)
        self.owner = await self.app.accounts.create_web_user("timeline-test", "temporary-password")

    async def test_priority_and_chat_handoff_wait_for_current_task(self):
        timeline = self.app.timeline
        released, started = asyncio.Event(), asyncio.Event()
        events = []
        self.app.social._chat_handoff = AsyncMock(return_value=True)

        async def watching():
            events.append("watch")
            receipt = self.app.social.queue_chat(self.owner, "private", "跟进公开播报")
            repeated = self.app.social.queue_chat(self.owner, "private", "同目标已有排队任务")
            self.assertEqual(receipt["task_id"], repeated["task_id"])
            self.assertFalse(receipt["sent"])
            started.set()
            await released.wait()
            events.append("watch ended")

        async def private():
            events.append("private")
            self.app.social._chat_handoff.assert_not_awaited()

        _, watch = timeline.enqueue(self.owner, "watch", watching)
        await asyncio.wait_for(started.wait(), 2)
        _, chat = timeline.enqueue(self.owner, "private", private)
        other = await self.app.accounts.create_web_user("timeline-other", "temporary-password")
        async def separate():
            return "parallel"

        self.assertEqual(await asyncio.wait_for(timeline.run(other, "private", separate), 2), "parallel")
        released.set()
        await asyncio.wait_for(asyncio.gather(watch, chat), 2)
        if self.owner in timeline.workers:
            await asyncio.wait_for(asyncio.shield(timeline.workers[self.owner]), 2)
        self.assertEqual(events, ["watch", "watch ended", "private"])
        self.app.social._chat_handoff.assert_awaited_once()
        self.assertEqual(timeline.history(self.owner)[0]["kind"], "private")
        self.assertEqual(timeline.logs(self.owner), [])

    async def test_handoff_uses_and_updates_only_the_target_history(self):
        app, owner = self.app, self.owner
        app.db.execute("INSERT INTO demo_profiles VALUES(?,?)", (owner, dump({"model": "test", "key": "test"})))
        token = issue_id(app.db, owner)
        game = app.engine.create({"user_binding": {"type": "random", "values": [token]}, "name": "timeline"})
        room = app.community.groups(owner, "POST", "", {"name": "timeline room"})["id"]
        app.db.execute("INSERT INTO community_group_agents VALUES(?,?)", (room, owner))
        app.db.execute("INSERT INTO community_group_messages(room,owner,body) VALUES(?,?,?)",
                       (room, owner, "群聊历史"))
        app.db.execute("INSERT INTO demo_chat(game,role,content,created) VALUES(?,'user',?,1)",
                       (game["id"], "私聊历史"))
        invoke = AsyncMock(return_value={"messages": [AIMessage(content="跟进完成")]})
        with patch("src.agent.social.model_client"), patch("langchain.agents.create_agent",
                return_value=SimpleNamespace(ainvoke=invoke)):
            self.assertTrue(await app.social._chat_handoff(owner, "group", "公开跟进", room, "", 0))
            self.assertTrue(await app.social._chat_handoff(owner, "private", "私聊跟进", 0, "", 0))
        group, private = [json.loads(call.args[0]["messages"][0].content) for call in invoke.await_args_list]
        self.assertIn("群聊历史", str(group["history"]))
        self.assertNotIn("私聊历史", str(group))
        self.assertIn("私聊历史", str(private["history"]))
        self.assertNotIn("群聊历史", str(private))
        self.assertEqual(app.db.execute("SELECT body FROM community_group_messages WHERE room=? ORDER BY id DESC LIMIT 1",
                                       (room,)).fetchone()[0], "跟进完成")
        self.assertEqual(app.db.execute("SELECT content FROM demo_chat WHERE game=? ORDER BY id DESC LIMIT 1",
                                       (game["id"],)).fetchone()[0], "跟进完成")

    async def test_group_image_can_send_directly_or_wait_for_contextual_handoff(self):
        app, owner = self.app, self.owner
        app.db.execute("INSERT INTO demo_profiles VALUES(?,?)", (owner, dump({"model": "test", "key": "test"})))
        token = issue_id(app.db, owner)
        game = app.engine.create({"user_binding": {"type": "random", "values": [token]}})
        room = app.community.groups(owner, "POST", "", {"name": "image group"})["id"]
        app.db.execute("INSERT INTO community_group_agents VALUES(?,?)", (room, owner))
        media = {"kind": "image", "src": "data:image/webp;base64,QUJD", "label": "chart"}
        image_message = app.db.execute(
            "INSERT INTO demo_chat(game,role,content,created,attachments) VALUES(?,'user',?,1,?)",
            (game["id"], "private context", dump([media])),
        ).lastrowid
        reference = f"private:{image_message}:0"
        tool = group_chat_tool(app, owner, game["id"])
        unauthorized = group_chat_tool(app, owner, "different-game")
        self.assertFalse(json.loads(await unauthorized("send", room, image_reference=reference))["ok"])
        self.assertTrue(json.loads(await tool("send", room, image_reference=reference))["sent"])

        invoke = AsyncMock(return_value={"messages": [AIMessage(content="[SEND_IMAGE]")]})

        async def private():
            receipt = json.loads(await tool("handoff", room, body="Send if relevant", image_reference=reference))
            self.assertEqual(receipt["state"], "queued")
            self.assertFalse(receipt["sent"])
            invoke.assert_not_awaited()

        with patch("src.agent.social.model_client"), patch("langchain.agents.create_agent",
                return_value=SimpleNamespace(ainvoke=invoke)):
            await app.timeline.run(owner, "private", private)
            worker = app.timeline.workers.get(owner)
            if worker:
                await asyncio.wait_for(asyncio.shield(worker), 2)
        rows = app.db.execute("SELECT body,attachment FROM community_group_messages WHERE room=? ORDER BY id", (room,)).fetchall()
        self.assertEqual([(row["body"], json.loads(row["attachment"])) for row in rows], [("", media), ("", media)])
        context = invoke.await_args.args[0]["messages"][0].content
        self.assertIn("chart", context)
        self.assertNotIn("private context", context)
        self.assertNotIn(media["src"], context)

    async def test_debug_option_and_failed_task_log(self):
        async with TestClient(TestServer(self.app.app(False))) as client:
            login = await client.post("/api/login", json={"username": "timeline-test", "password": "temporary-password"})
            self.assertTrue(login.ok)
            settings = await (await client.get("/api/agent-timeline")).json()
            self.assertEqual(settings, {"enabled": False, "tasks": [], "logs": []})
            self.assertTrue((await client.post("/api/agent-timeline", json={"enabled": True})).ok)

            async def fails():
                raise ValueError("private payload")

            with self.assertRaises(ValueError):
                await self.app.timeline.run(self.owner, "watch", fails)
            settings = await (await client.get("/api/agent-timeline")).json()
            self.assertEqual(settings["tasks"][0]["state"], "failed")
            self.assertEqual(settings["logs"][0]["error_type"], "ValueError")
            self.assertNotIn("private payload", str(settings))
