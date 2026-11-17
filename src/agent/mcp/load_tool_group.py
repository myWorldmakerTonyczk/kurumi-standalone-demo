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

INSTRUCTIONS = (
    '需要工具时可调用 load_tool_group。传 {} 查看当前可用目录。'
    '如果已知需要哪些组，请一次传入所需类别，例如 {"categories": ["inbox", "community"]}。'
    '加载结果列出各组工具名，从下一轮开始可以按工具定义直接调用。没有工具需求可直接回复。'
)



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
        def load_tool_group(category: str = "", categories: list[str] | None = None) -> str:
            """按需加载当前场景的工具组。

            无参数时返回可用类别。用 categories 一次加载多组，
            例如 {"categories": ["inbox", "community"]}。成功后下一轮即可调用这些工具。

            Args:
                category: 单个类别，兼容旧调用。
                categories: 要一起加载的类别列表。重复类别会去重。
            """
            requested = dict.fromkeys(([category] if category else []) + (categories or []))
            if not requested:
                return dump({"groups": {name: CATALOG[name] for name in self.groups}})
            unavailable = [name for name in requested if name not in self.groups]
            if unavailable:
                return dump({"ok": False, "error": "当前场景没有这些工具组",
                             "unavailable": unavailable, "groups": list(self.groups)})
            self.enabled.update(requested)
            if categories:
                return dump({"ok": True, "enabled": list(requested), "tools": {
                    name: [tool_name(tool) for tool in self.groups[name]] for name in requested
                }})
            return dump({"ok": True, "enabled": category, "tools": [tool_name(tool) for tool in self.groups[category]]})

        load_tool_group.__doc__ = load_tool_group.__doc__.replace(
            "            Args:", "            可用类别为 " + "、".join(self.groups) + "。\n\n            Args:"
        )
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
