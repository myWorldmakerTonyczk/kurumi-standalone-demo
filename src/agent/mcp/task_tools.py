from .load_tool_group import ToolGroups, tool_name
from .registry import flatten


INBOX_TOOLS = ("read_inbox", "read_inbox_history", "mark_inbox_unread")
PUBLIC_TIMELINE_TOOLS = ("schedule_task", "get_task_report", "query_task_timeline")
PRIVATE_TIMELINE_TOOLS = PUBLIC_TIMELINE_TOOLS + ("cancel_scheduled_task",)
CHAT_MEDIA_TOOLS = ("search_images", "send_saved_image", "send_review_image", "sticker")
CHAT_INITIAL = (INBOX_TOOLS + PRIVATE_TIMELINE_TOOLS + CHAT_MEDIA_TOOLS
                + ("watch_market", "read_journal", "mention_group_member", "read_market_bulletins"))

# One place for what each task category starts with and whether it can load more.
SCENES = {
    "chat": {"initial": CHAT_INITIAL, "on_demand": True},
    "watch": {"initial": ("watch_market", "price_history", "read_journal"), "on_demand": True},
    "patrol": {"initial": ("read_inbox",), "on_demand": True},
    "diary": {"initial": ("read_journal",), "on_demand": False},
    "trade_review": {"initial": (), "on_demand": False},
    "group_summary": {"initial": (), "on_demand": False},
}
INITIAL_TOOLS = {name: scene["initial"] for name, scene in SCENES.items()}
ON_DEMAND_TASKS = {name for name, scene in SCENES.items() if scene["on_demand"]}

APPOINTMENT_WORDS = ("提醒", "唤醒", "预约", "叫醒我", "叫我")
CANCELLATION_WORDS = ("取消", "撤销")


def task_tools(task, groups, message=""):
    initial = INITIAL_TOOLS[task]
    if task == "chat" and any(word in message for word in APPOINTMENT_WORDS):
        initial += ("schedule_task",)
        if any(word in message for word in CANCELLATION_WORDS):
            initial += ("cancel_scheduled_task",)

    tools = flatten(groups)
    if task in ON_DEMAND_TASKS:
        return tools, ToolGroups(groups, initial=initial)
    return [tool for tool in tools if tool_name(tool) in initial], None
