import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
from langchain_core.messages import AIMessage

from langgraph.errors import GraphRecursionError

from app import AGENT_RECURSION_LIMIT, invoke_agent
from src.agent.social import Social


class _Recursing:
    async def ainvoke(self, payload, config):
        raise GraphRecursionError("Recursion limit reached")


class _Echo:
    def __init__(self):
        self.config = None

    async def ainvoke(self, payload, config):
        self.config = config
        return {"messages": payload["messages"]}


class AgentLimitTest(unittest.IsolatedAsyncioTestCase):
    async def test_recursion_error_becomes_readable_value_error(self):
        with self.assertRaisesRegex(ValueError, "步骤太多"):
            await invoke_agent(_Recursing(), [])

    async def test_default_limit_is_applied(self):
        agent = _Echo()
        result = await invoke_agent(agent, ["hi"])
        self.assertEqual(result, {"messages": ["hi"]})
        self.assertEqual(agent.config, {"recursion_limit": AGENT_RECURSION_LIMIT})


class RepeatingPatrolModel(FakeMessagesListChatModel):
    def bind_tools(self, tools, **kwargs):
        return self


class PatrolLimitTest(unittest.IsolatedAsyncioTestCase):
    async def test_repeated_public_tool_calls_stop_without_reposting(self):
        social = Social.__new__(Social)
        social.slots = asyncio.Semaphore(1)
        publish = Mock(return_value={"id": 1})
        social.app = SimpleNamespace(
            profile=lambda owner: {"model": "test", "key": "test"},
            external=SimpleNamespace(settings=lambda owner: {"mode": "platform"}),
            model_url=lambda url: url, default_persona="test",
            call_logs=SimpleNamespace(callback=lambda *args: None),
            community=SimpleNamespace(handle=publish),
        )
        model = RepeatingPatrolModel(responses=[
            AIMessage(content="", tool_calls=[{"name": "forum", "args": {
                "action": "publish", "title": "News", "body": "Update"}, "id": f"tool-{i}"}])
            for i in range(9)
        ])
        with patch("src.agent.social.model_client", return_value=model), patch("src.agent.social.log") as logger:
            await social.run_patrol("owner")
        publish.assert_called_once()
        self.assertEqual(model.i, 8)
        logger.exception.assert_not_called()
        logger.warning.assert_not_called()


if __name__ == "__main__":
    unittest.main()
