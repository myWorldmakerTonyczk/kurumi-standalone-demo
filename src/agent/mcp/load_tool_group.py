from langchain.agents.middleware import AgentMiddleware
from langchain_core.messages import ToolMessage

from src.core.engine import dump

CATALOG = {
    "market_review": "查行情、账户、播报、日志和日记，生成复盘图。",
    "trading": "操作模拟账户、交易计划和通知设置。",
    "community": "读论坛和群聊原文，回复、发言或拟草稿。",
    "inbox": "收取未读、查看历史或标回未读。",
    "memory": "查询和保存长期记忆。",
    "media": "找现成图片、表情包和附上链接。",
    "timeline": "预约任务、查任务时间线与执行报告。",
}

INSTRUCTIONS = "需要工具时可调用 load_tool_group。知道类别就直接传类别，不确定时留空查目录。没有工具需求可直接回复。"


def tool_name(tool):
    return getattr(tool, "name", None) or tool.__name__


class ToolGroups(AgentMiddleware):
    """One instance per agent invocation. The executor knows all authorized tools."""
    def __init__(self, groups, initial=()):
        self.groups = {name: tools for name, tools in groups.items() if tools}
        self.enabled = set()
        self.initial = set(initial) | {"load_tool_group"}
        self.tools = [self._loader()]

    def _loader(self):
        def load_tool_group(category: str = "") -> str:
            """启用一组工具。留空查看当前场景可用的工具类别。"""
            if not category:
                return dump({"groups": {name: CATALOG[name] for name in self.groups}})
            if category not in self.groups:
                return dump({"ok": False, "error": "当前场景没有这个工具组", "groups": list(self.groups)})
            self.enabled.add(category)
            return dump({"ok": True, "enabled": category, "tools": [tool_name(tool) for tool in self.groups[category]]})

        load_tool_group.__doc__ += " 可用类别：" + "、".join(self.groups)
        return load_tool_group

    def available(self):
        return self.initial | {tool_name(tool) for name in self.enabled for tool in self.groups[name]}

    async def awrap_model_call(self, request, handler):
        available = self.available()
        return await handler(request.override(tools=[tool for tool in request.tools if tool_name(tool) in available]))

    async def awrap_tool_call(self, request, handler):
        call = request.tool_call
        if call["name"] not in self.available():
            return ToolMessage(content="请先启用对应工具组。", tool_call_id=call["id"], status="error")
        return await handler(request)
