import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from aiohttp import web
from aiohttp.test_utils import TestServer
from langchain.agents import create_agent
from langchain_core.messages import AIMessage

from app import Demo
from src.agent.mcp.inbox.read_inbox import build
from src.agent.mcp.load_tool_group import ToolGroups
from src.agent.model_registry import model_client
from src.agent.task_trace_middleware import TraceTools


class TaskTraceTest(unittest.IsolatedAsyncioTestCase):
    async def test_opt_in_trace_captures_tools_and_provider_usage(self):
        requests = []

        async def complete(request):
            body = await request.json()
            requests.append(body)
            calls = sum(message["role"] == "tool" for message in body["messages"])
            message = {"role": "assistant", "content": "done"}
            if calls < 2:
                name = ("load_tool_group", "read_inbox")[calls]
                arguments = {"categories": ["inbox"]} if not calls else {}
                message = {"role": "assistant", "content": None, "tool_calls": [{
                    "id": f"call-{calls}", "type": "function",
                    "function": {"name": name, "arguments": json.dumps(arguments)},
                }]}
            return web.json_response({
                "id": "test", "model": "local-test", "object": "chat.completion", "created": 1,
                "choices": [{"index": 0, "message": message,
                             "finish_reason": "tool_calls" if calls < 2 else "stop"}],
                "usage": {"prompt_tokens": 100, "completion_tokens": 10, "total_tokens": 110},
            })

        fake = web.Application()
        fake.router.add_post("/v1/chat/completions", complete)
        with tempfile.TemporaryDirectory() as root:
            demo = Demo(Path(root) / "test.sqlite3")
            try:
                owner = await demo.accounts.create_web_user("trace-user", "temporary-test-password")
                other = await demo.accounts.create_web_user("trace-other", "temporary-test-password")
                demo.agent_inbox.add(owner, "system", "test", "One event")
                async with TestServer(fake) as endpoint:
                    config = {"key": "test-private-key", "model": "local-test", "url": str(endpoint.make_url("/v1"))}

                    async def run():
                        tool = build(demo.agent_inbox, owner)
                        audit = demo.call_logs.callback(owner, "patrol")
                        agent = create_agent(model_client(config, lambda url: url, audit=audit), tools=[tool],
                                             middleware=[TraceTools(), ToolGroups({"inbox": [tool]})])
                        await agent.ainvoke({"messages": [("user", "check inbox test-private-key")]})
                        return "completed"

                    first, future = demo.timeline.enqueue(owner, "patrol", run)
                    await future
                    self.assertEqual(demo.timeline.trace_events(owner, first), [])
                    profile = {"timeline_debug": True, **config}
                    demo.db.execute("INSERT OR REPLACE INTO demo_profiles VALUES(?,?)", (owner, json.dumps(profile)))
                    demo.agent_inbox.add(owner, "system", "test-2", "Another event")
                    second, future = demo.timeline.enqueue(owner, "patrol", run)
                    await future

                events = demo.timeline.trace_events(owner, second)
                self.assertEqual([event["kind"] for event in events],
                                 ["task", "model", "tool", "model", "tool", "inbox", "model", "result"])
                models = [event for event in events if event["kind"] == "model"]
                self.assertEqual(models[0]["result"]["usage"]["input_tokens"], 100)
                for index, event in enumerate(models):
                    sent = requests[index + 3]["tools"]
                    self.assertEqual(event["request"]["tools"], sent)
                self.assertEqual([tool["function"]["name"] for tool in models[0]["request"]["tools"]], ["load_tool_group"])
                self.assertEqual({tool["function"]["name"] for tool in models[1]["request"]["tools"]},
                                 {"load_tool_group", "read_inbox"})
                self.assertEqual(events[2]["request"]["call_id"], "call-0")
                self.assertEqual(events[5]["request"]["total"], 1)
                self.assertEqual(demo.agent_inbox.logs(owner)[0]["task"], second)
                self.assertNotIn(config["key"], json.dumps(events))
                self.assertEqual(demo.timeline.trace_events(other, second), [])
                self.assertEqual(demo.call_logs.entries(owner), [])
            finally:
                await demo.timeline.close()
                demo.db.close()

    async def test_background_memory_trace_records_committed_items_and_failures(self):
        with tempfile.TemporaryDirectory() as root:
            demo = Demo(Path(root) / "test.sqlite3")
            try:
                owner = await demo.accounts.create_web_user("memory-trace", "temporary-test-password")
                demo.db.execute("INSERT OR REPLACE INTO demo_profiles VALUES(?,?)",
                                (owner, json.dumps({"timeline_debug": True})))
                key = (owner, "private:trace-fixture")
                demo.db.executemany("""INSERT INTO agent_memory_events(owner,stream,event,subject,text,reply)
                    VALUES(?,?,?,'self',?,'')""", ((*key, f"user:{i}", f"Memory evidence {i}") for i in range(1, 6)))
                seq = demo.memory.batch(key)[0]["seq"]
                answer = AIMessage(content=json.dumps([{"event": seq, "text": "A real memory", "tags": ["test"]}]))
                with patch("src.agent.memory.model_client") as client:
                    client.return_value.ainvoke = AsyncMock(return_value=answer)
                    await demo.memory.drain(key, {}, lambda url: url)
                tasks = demo.timeline.history(owner, trace_kind="memory-generation")
                task = next(item for item in tasks if item["kind"] == "memory_generation")
                event = next(item for item in demo.timeline.trace_events(owner, task["id"]) if item["kind"] == "memory_write")
                saved = event["request"]["saved"]
                self.assertEqual([(item["action"], item["text"]) for item in saved], [("created", "A real memory")])
                self.assertEqual(demo.memory.user_entries(owner)[0]["id"], saved[0]["id"])
                self.assertEqual(demo.timeline.context(owner)["recent"], [])

                async def broken():
                    raise ValueError("invalid model JSON")

                with self.assertRaisesRegex(ValueError, "invalid model JSON"):
                    await demo.timeline.trace_background(owner, "memory_generation", {"stream": key[1]}, broken)
                failed = demo.timeline.history(owner, trace_kind="memory-generation")[0]
                self.assertEqual(failed["state"], "failed")
                events = demo.timeline.trace_events(owner, failed["id"])
                self.assertEqual(events[1]["request"]["message"], "invalid model JSON")
            finally:
                await demo.timeline.close()
                demo.db.close()
