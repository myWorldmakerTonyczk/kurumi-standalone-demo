import json
import tempfile
import unittest
from pathlib import Path
from typing import ClassVar

from langchain.agents import create_agent
from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
from langchain_core.messages import AIMessage

from app import Demo
from src.agent.mcp.load_tool_group import ToolGroups, tool_name
from src.agent.mcp.registry import flatten, user_workspace_groups


class TrackingModel(FakeMessagesListChatModel):
    bindings: ClassVar[list] = []

    def bind_tools(self, tools, **kwargs):
        self.bindings.append([tool_name(tool) for tool in tools])
        return self


class ToolGroupTest(unittest.IsolatedAsyncioTestCase):
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
