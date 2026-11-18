"""Shared chat agent construction for every chat trigger."""
from .task_trace_middleware import TraceTools


def chat_agent(model, tools, tool_groups, system_prompt, *, extra_middleware=()):
    from langchain.agents import create_agent

    middleware = [TraceTools()]
    if tool_groups is not None:
        middleware.append(tool_groups)
    middleware.extend(extra_middleware)
    return create_agent(model, tools=tools, middleware=middleware, system_prompt=system_prompt)
