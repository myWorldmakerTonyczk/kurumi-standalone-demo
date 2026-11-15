import unittest

from langgraph.errors import GraphRecursionError

from app import AGENT_RECURSION_LIMIT, invoke_agent


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


if __name__ == "__main__":
    unittest.main()
