from langchain.agents.middleware import AgentMiddleware
from langchain_core.messages import ToolMessage

from src.core.engine import dump

CATALOG = {
    "market_review": "查现在的报价、历史 K 线、模拟账户与持仓、交易日志、日记和市场播报。在支持的场景可生成交易复盘图。适合回答「现在什么价」「上次为什么平仓」「今天有什么市场消息」。",
    "trading": "操作模拟账户，包括按当前报价开仓、挂限价单和撤单、平仓、修改止损和止盈、借还虚拟资金、更新交易计划和记录观望。在支持的场景可配置已有交易事件的通知。到价唤醒或发提醒请用 timeline。",
    "community": "读论坛帖子、评论和群聊原文，按当前场景的权限回复、发帖、群聊发言或拟草稿。从邮箱看到摘要后，需要核对原文或参与讨论时用这组。支持向智能体已加入的群直接发送文字或图片。",
    "inbox": "收取论坛回复和点赞点踩、群聊新消息、市场播报及系统提醒的未读摘要。默认收件后标为已读，也支持只预览、查历史和标回未读。需要看完整内容时再用对应来源的工具。",
    "memory": "查找长期记忆，或保存值得以后记住的事实、偏好和约定。写入时填写记忆对象、标签和强度。任务执行记录和报告请用 timeline。",
    "media": "查找已有图片和表情包，收藏或发送图片，或附上面板链接。要根据交易数据生成复盘图，请用 market_review。",
    "timeline": "设置价位提醒和任务唤醒，或查询任务时间线、查看待执行预约与完整报告，并可在非公开任务取消待执行预约。可预约立即、延时或指定时间执行，也能在报价向上突破、向下跌破或进入区间时触发。还支持账户事件、新市场播报和群聊提及。触发后可安排看盘、巡视、私聊提醒或群聊跟进，以当前场景权限为准。要到价按限价约束自动成交，请用 trading 的限价单。",
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
