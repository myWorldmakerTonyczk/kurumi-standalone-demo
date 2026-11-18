import json
import tempfile
import unittest
from pathlib import Path
from typing import ClassVar
from unittest.mock import patch

from langchain.agents import create_agent
from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
from langchain_core.messages import AIMessage

from app import Demo
from src.agent.mcp.load_tool_group import ToolGroups, tool_name
from src.agent.mcp.registry import flatten, user_group_groups, user_patrol_groups, user_workspace_groups


class TrackingModel(FakeMessagesListChatModel):
    bindings: ClassVar[list] = []

    def bind_tools(self, tools, **kwargs):
        self.bindings.append([tool_name(tool) for tool in tools])
        return self


class ToolGroupTest(unittest.IsolatedAsyncioTestCase):
    async def test_watch_records_observation_only_without_an_existing_outcome(self):
        import time
        from src.users.ownership import issue_id

        with tempfile.TemporaryDirectory() as root:
            app = Demo(Path(root) / "test.sqlite3")
            try:
                owner = await app.accounts.create_web_user("watch-test", "temporary-test-password")
                token = issue_id(app.db, owner)
                game = app.engine.create({"user_binding": {"type": "random", "values": [token]}})
                gid = game["id"]
                app.db.execute("INSERT OR REPLACE INTO demo_profiles VALUES(?,?)",
                               (owner, json.dumps({"url": "https://example.com/v1", "model": "test", "key": "test-key"})))
                for pair, bid, ask in [("EUR/USD", "1.1000", "1.1002"), ("USD/JPY", "150", "150.02")]:
                    app.engine.quotes[pair] = dict(bid=bid, ask=ask, timestamp=time.time(), tradeable=True)

                observe = ("record_observation", {"reason": "tool observation"})
                opening = ("open_trade", {"side": "buy", "units": 100, "reason": "simulated trade"})
                plan = ("write_plan", {"text": "Next step", "reason": "updated after opening"})
                for calls, expected in [([], ["observe"]), ([observe, observe], ["observe"]),
                                        ([opening, observe], ["open"]), ([opening, plan], ["plan", "open"])]:
                    with self.subTest(calls=calls):
                        count = len(app.engine.logs(gid))
                        responses = [AIMessage(content="", tool_calls=[dict(name=name, args=args, id=f"call-{index}")])
                                     for index, (name, args) in enumerate(calls)]
                        if calls:
                            responses.insert(0, AIMessage(content="", tool_calls=[
                                dict(name="load_tool_group", args={"categories": ["trading"]}, id="load-trading"),
                            ]))
                        TrackingModel.bindings.clear()
                        responses.append(AIMessage(content="Final watch summary"))
                        with patch("app.model_client", return_value=TrackingModel(responses=responses)):
                            reply = await app.run_decision(app.engine.get(gid))
                        self.assertEqual(reply, "Final watch summary")
                        self.assertEqual(set(TrackingModel.bindings[0]), {
                            "load_tool_group", "watch_market", "price_history", "read_journal",
                        })
                        if calls:
                            self.assertIn(calls[0][0], TrackingModel.bindings[1])
                        added = app.engine.logs(gid)[:len(app.engine.logs(gid)) - count]
                        self.assertEqual([entry["action"] for entry in added], expected)
                        if calls == [observe, observe]:
                            self.assertEqual(added[0]["reason"], "tool observation")
                for message, extra_tools in [
                    ("Which trade closed last?", set()), ("六点提醒我", {"schedule_task"}),
                    ("取消六点的预约", {"schedule_task", "cancel_scheduled_task"}),
                ]:
                    TrackingModel.bindings.clear()
                    with patch("app.model_client", return_value=TrackingModel(responses=[AIMessage(content="[SKIP]")])):
                        await app.agent(app.engine.get(gid), message, attachments=[])
                    expected = {
                        "load_tool_group", "watch_market", "read_journal",
                        "read_inbox", "read_inbox_history", "mark_inbox_unread",
                        "schedule_task", "get_task_report", "query_task_timeline", "cancel_scheduled_task",
                        "search_images", "send_saved_image", "send_review_image", "sticker",
                        "read_market_bulletins",
                    }
                    self.assertEqual(set(TrackingModel.bindings[0]), expected | extra_tools)
                for task, expected in [("diary", {"read_journal"}), ("trade_review", set()), ("group_summary", set())]:
                    with self.subTest(task=task):
                        TrackingModel.bindings.clear()
                        with patch("app.model_client", return_value=TrackingModel(responses=[AIMessage(content="Report")])):
                            self.assertEqual(await app.agent(app.engine.get(gid), diary_prompt="Task input", audit_task=task), "Report")
                        self.assertEqual(set(TrackingModel.bindings[0] if TrackingModel.bindings else ()), expected)
            finally:
                await app.timeline.close()
                app.db.close()

    async def test_group_media_and_handoff_tasks_can_send_saved_images(self):
        import time
        from src.agent.mcp.task_tools import ON_DEMAND_TASKS, task_tools

        with tempfile.TemporaryDirectory() as root:
            app = Demo(Path(root) / "group-media.sqlite3")
            try:
                owner = await app.accounts.create_web_user("group-media", "temporary-test-password")
                from src.users.ownership import issue_id
                token = issue_id(app.db, owner)
                app.engine.create({"user_binding": {"type": "random", "values": [token]}})
                app.db.execute("""INSERT INTO agent_images
                    (owner,digest,src,name,category,description,tags,sources,created)
                    VALUES(?,?,?,?,?,?,?,?,?)""",
                    (owner, "digest", "data:image/webp;base64,QUJD", "图库图", "分类", "描述", "[]", "[]", time.time()))
                attachments = []
                groups = user_group_groups(app, owner, {}, f"agent:{owner}", attachments,
                                           has_images=False, with_stickers=True)
                names = {tool_name(tool) for tool in groups["media"]}
                self.assertEqual(names, {"search_images", "send_saved_image", "save_image",
                                         "send_review_image", "sticker"})
                send = next(tool for tool in groups["media"] if tool_name(tool) == "send_saved_image")
                self.assertTrue(json.loads(await send(1))["ok"])
                self.assertEqual(len(attachments), 1)
                blocked = json.loads(await send(1))
                self.assertFalse(blocked["ok"])
                self.assertIn("一个附件", blocked["error"])
                self.assertEqual(len(attachments), 1)
                for task in ("chat",):
                    self.assertIn(task, ON_DEMAND_TASKS)
                    tools, middleware = task_tools(task, groups)
                    self.assertIsNotNone(middleware)
                    self.assertTrue(tools)
                    self.assertLessEqual({"search_images", "send_saved_image",
                                          "send_review_image", "sticker"}, middleware.initial)
            finally:
                app.db.close()

    async def test_forum_and_patrol_group_accept_direct_library_images(self):
        import base64
        import time
        from io import BytesIO
        from PIL import Image as PILImage
        from src.agent.mcp.community.forum import build as forum_tool
        from src.agent.mcp.community.patrol_group import build as patrol_group_tool

        with tempfile.TemporaryDirectory() as root:
            app = Demo(Path(root) / "direct-images.sqlite3")
            try:
                owner = await app.accounts.create_web_user("direct-images", "temporary-test-password")
                from src.users.ownership import issue_id
                token = issue_id(app.db, owner)
                app.engine.create({"user_binding": {"type": "random", "values": [token]}})
                output = BytesIO()
                PILImage.new("RGB", (32, 32), (20, 120, 200)).save(output, "PNG")
                src = "data:image/png;base64," + base64.b64encode(output.getvalue()).decode("ascii")
                image_id = app.db.execute("""INSERT INTO agent_images
                    (owner,digest,src,name,category,description,tags,sources,created)
                    VALUES(?,?,?,?,?,?,?,?,?)""",
                    (owner, "digest", src, "图库图", "分类", "描述", "[]", "[]", time.time())).lastrowid
                room = app.community.groups(owner, "POST", "", {"name": "贴图群"})["id"]
                app.db.execute("INSERT INTO community_group_agents VALUES(?,?)", (room, owner))

                forum = forum_tool(app, owner)
                result = json.loads(await forum("publish", title="带图帖子", body="正文",
                                                title_image_id=image_id, image_id=image_id))
                post = app.db.execute("SELECT title_media,media FROM community_posts WHERE id=?",
                                      (result["id"],)).fetchone()
                for value in (post["title_media"], post["media"]):
                    media = json.loads(value)
                    self.assertEqual(media["kind"], "image")
                    self.assertTrue(media["src"].startswith("data:image/webp;base64,"))

                group = patrol_group_tool(app.social, owner, [False])
                self.assertIn("已发送", await group("send", str(room), "群图", image_id=image_id))
                message = app.db.execute("""SELECT attachment FROM community_group_messages
                    WHERE room=? ORDER BY id DESC LIMIT 1""", (room,)).fetchone()
                self.assertTrue(json.loads(message["attachment"])["src"].startswith("data:image/webp;base64,"))
            finally:
                app.db.close()

    async def test_same_turn_load_and_no_carry_to_next_turn(self):
        def read_example(peek: bool = False) -> str:
            """Inspect an example."""
            return str(peek)

        def private_tool() -> str:
            """Return a different group."""
            return "other"

        groups = {"inbox": [read_example], "trading": [private_tool]}
        responses = [
            AIMessage(content="", tool_calls=[dict(name="load_tool_group", args={"category": "inbox"}, id="group")]),
            AIMessage(content="", tool_calls=[dict(name="read_example", args={"peek": True}, id="read")]),
            AIMessage(content="done"),
        ]
        TrackingModel.bindings.clear()
        graph = create_agent(TrackingModel(responses=responses), tools=flatten(groups), middleware=[ToolGroups(groups)])
        result = await graph.ainvoke({"messages": [("user", "check")]})
        self.assertEqual(TrackingModel.bindings[0], ["load_tool_group"])
        self.assertEqual(set(TrackingModel.bindings[1]), {"load_tool_group", "read_example"})
        self.assertTrue(any(message.content == "True" for message in result["messages"]))
        graph = create_agent(TrackingModel(responses=[AIMessage(content="hi")]), tools=flatten(groups),
                             middleware=[ToolGroups(groups)])
        await graph.ainvoke({"messages": [("user", "hello")]})
        self.assertEqual(TrackingModel.bindings[-1], ["load_tool_group"])

    async def test_load_multiple_groups_in_one_call(self):
        def read_example() -> str:
            """Read the example inbox."""
            return "inbox"

        def inspect_trade() -> str:
            """Read the example trade."""
            return "trade"

        groups = {"inbox": [read_example], "trading": [inspect_trade]}
        responses = [
            AIMessage(content="", tool_calls=[dict(name="load_tool_group", args={}, id="directory")]),
            AIMessage(content="", tool_calls=[dict(
                name="load_tool_group", args={"categories": ["inbox", "media"]}, id="invalid"
            )]),
            AIMessage(content="", tool_calls=[dict(
                name="load_tool_group", args={"categories": ["inbox", "trading", "inbox"]}, id="groups"
            )]),
            AIMessage(content="", tool_calls=[
                dict(name="read_example", args={}, id="read"),
                dict(name="inspect_trade", args={}, id="trade"),
            ]),
            AIMessage(content="done"),
        ]
        TrackingModel.bindings.clear()
        graph = create_agent(TrackingModel(responses=responses), tools=flatten(groups), middleware=[ToolGroups(groups)])
        result = await graph.ainvoke({"messages": [("user", "check both groups")]})
        by_call = {message.tool_call_id: message.content for message in result["messages"] if message.type == "tool"}

        self.assertEqual(set(json.loads(by_call["directory"])["groups"]), set(groups))
        self.assertEqual(json.loads(by_call["invalid"])["unavailable"], ["media"])
        self.assertEqual(TrackingModel.bindings[:3], [["load_tool_group"]] * 3)
        self.assertEqual(json.loads(by_call["groups"]), {
            "ok": True,
            "enabled": ["inbox", "trading"],
            "tools": {"inbox": ["read_example"], "trading": ["inspect_trade"]},
        })
        self.assertEqual(set(TrackingModel.bindings[3]), {"load_tool_group", "read_example", "inspect_trade"})
        self.assertEqual(by_call["read"], "inbox")
        self.assertEqual(by_call["trade"], "trade")

    async def test_timeline_tools_run_on_the_database_thread(self):
        from src.agent.mcp.registry import timeline_group

        with tempfile.TemporaryDirectory() as root:
            app = Demo(Path(root) / "test.sqlite3")
            try:
                owner = await app.accounts.create_web_user("timeline-tools", "temporary-test-password")

                async def complete():
                    return "done"

                task_id, finished = app.timeline.enqueue(owner, "private_followup", complete)
                await finished
                model = TrackingModel(responses=[
                    AIMessage(content="", tool_calls=[
                        dict(name="query_task_timeline", args={"limit": 10}, id="query"),
                        dict(name="get_task_report", args={"task_id": task_id}, id="report"),
                    ]),
                    AIMessage(content="done"),
                ])
                agent = create_agent(model, tools=timeline_group(app, owner))
                result = await agent.ainvoke({"messages": [("user", "check the reminder")]})
                by_call = {message.tool_call_id: json.loads(message.content)
                           for message in result["messages"] if message.type == "tool"}
                self.assertEqual(by_call["query"]["items"][0]["id"], task_id)
                self.assertEqual(by_call["report"]["state"], "completed")
            finally:
                await app.timeline.close()
                app.db.close()

    async def test_patrol_inbox_tools_use_the_current_account(self):
        with tempfile.TemporaryDirectory() as root:
            app = Demo(Path(root) / "inbox.sqlite3")
            try:
                owner = await app.accounts.create_web_user("inbox-owner", "temporary-test-password")
                other = await app.accounts.create_web_user("inbox-other", "temporary-test-password")
                app.agent_inbox.add(owner, "system", "test", "own event")
                app.agent_inbox.add(other, "system", "test", "other event")
                groups = user_patrol_groups(app.social, owner)
                responses = [
                    AIMessage(content="", tool_calls=[dict(name=name, args=args, id=f"call-{i}")])
                    for i, (name, args) in enumerate([
                        ("load_tool_group", {"category": "inbox"}),
                        ("read_inbox", {}),
                        ("read_inbox_history", {}),
                        ("mark_inbox_unread", {"item_id": 1}),
                        ("read_inbox", {"peek": True}),
                    ])
                ]
                graph = create_agent(TrackingModel(responses=responses + [AIMessage(content="done")]),
                                     tools=flatten(groups), middleware=[ToolGroups(groups)])
                result = await graph.ainvoke({"messages": [("user", "check inbox")]})
                by_call = {message.tool_call_id: message.content for message in result["messages"]
                           if message.type == "tool"}
                inbox = json.loads(by_call["call-1"])
                self.assertEqual([item["title"] for item in inbox["items"]], ["own event"])
                self.assertTrue(inbox["batch_id"])
                history = json.loads(by_call["call-2"])
                self.assertEqual([item["title"] for item in history["items"]], ["own event"])
                self.assertEqual(json.loads(by_call["call-3"])["marked_unread"], 1)
                self.assertEqual(json.loads(by_call["call-4"])["counts"]["system"], 1)
                self.assertEqual(app.agent_inbox.read(other, peek=True)["counts"]["system"], 1)
            finally:
                app.db.close()

    async def test_memory_write_merges_and_records_sources(self):
        with tempfile.TemporaryDirectory() as root:
            app = Demo(Path(root) / "test.sqlite3")
            try:
                owner = await app.accounts.create_web_user("memory-test", "temporary-test-password")
                # Use an owned game created through the same binding as onboarding.
                from src.users.ownership import issue_id
                token = issue_id(app.db, owner)
                game = app.engine.create({"user_binding": {"type": "random", "values": [token]}, "name": "memory"})
                gid = game["id"]
                for message_id, tags, strength in [(1, ["昵称"], 0.6), (2, ["偏好"], 0.8)]:
                    groups = user_workspace_groups(app, gid, writable=False, revision=0,
                                                  attachments=[], message_ids=[message_id])
                    write = next(tool for tool in groups["memory"] if tool_name(tool) == "write_memory")
                    result = json.loads(await write("请叫我小鱼", "self", tags, strength))
                    self.assertTrue(result["ok"])
                    self.assertNotIn("trading", groups)
                rows = app.memory.user_entries(owner)
                self.assertEqual(len(rows), 1)
                self.assertEqual(rows[0]["initial"], 0.8)
                self.assertEqual(set(rows[0]["tags"]), {"昵称", "偏好"})
                self.assertEqual(app.db.execute("SELECT count(*) FROM agent_memory_sources").fetchone()[0], 2)
            finally:
                app.db.close()
