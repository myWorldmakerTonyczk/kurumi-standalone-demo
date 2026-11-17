"""Record tools at the execution boundary of an agent task."""
import asyncio
import json

from langchain.agents.middleware import AgentMiddleware
from langchain_core.messages import ToolMessage

from .call_logs import error_label
from .timeline import task_trace


def tool_failed(message):
    if not isinstance(message, ToolMessage):
        return False
    if message.status == "error":
        return True
    if isinstance(message.content, str) and message.content.startswith("{"):
        try:
            return json.loads(message.content).get("ok") is False
        except ValueError:
            pass
    return False


class TraceTools(AgentMiddleware):
    async def awrap_tool_call(self, request, handler):
        trace = task_trace()
        if trace is None:
            return await handler(request)

        call = request.tool_call
        entry = trace.start("tool", call["name"], {"call_id": call["id"], "arguments": call["args"]})
        try:
            result = await handler(request)
        except asyncio.CancelledError:
            trace.finish(entry, {}, "interrupted")
            raise
        except Exception as exc:
            trace.finish(entry, {"error": error_label(exc), "message": str(exc)}, "failed")
            raise
        messages = [result] if isinstance(result, ToolMessage) else (
            (getattr(result, "update", None) or {}).get("messages", [])
            if isinstance(getattr(result, "update", None), dict) else []
        )
        failed = any(tool_failed(message) for message in messages)
        payload = [message.model_dump() if isinstance(message, ToolMessage) else message for message in messages]
        trace.finish(entry, {"output": payload or result}, "failed" if failed else "ok")
        return result
