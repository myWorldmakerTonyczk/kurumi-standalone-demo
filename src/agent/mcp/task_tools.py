from .load_tool_group import ToolGroups, tool_name
from .registry import flatten


INBOX_TOOLS = ("read_inbox", "read_inbox_history", "mark_inbox_unread")
PUBLIC_TIMELINE_TOOLS = ("schedule_task", "get_task_report", "query_task_timeline")
PRIVATE_TIMELINE_TOOLS = PUBLIC_TIMELINE_TOOLS + ("cancel_scheduled_task",)

# Fixed tool lists for the first model call. On-demand tasks also get the loader.
INITIAL_TOOLS = {
    "private": ("watch_market", "read_journal"),
    "group": ("mention_group_member",),
    "group_followup": ("mention_group_member",),
    "watch": ("watch_market", "price_history", "read_journal"),
    "patrol": ("read_inbox",),
    "diary": ("read_journal",),
    "trade_review": (),
    "group_summary": (),
    "proactive": INBOX_TOOLS + PRIVATE_TIMELINE_TOOLS,
    "private_handoff": INBOX_TOOLS + PRIVATE_TIMELINE_TOOLS,
    "group_handoff": ("read_market_bulletins", "mention_group_member") + INBOX_TOOLS + PUBLIC_TIMELINE_TOOLS,
}

ON_DEMAND_TASKS = {"private", "group", "group_followup", "watch", "patrol"}

APPOINTMENT_WORDS = ("提醒", "唤醒", "预约", "叫醒我", "叫我")
CANCELLATION_WORDS = ("取消", "撤销")


def task_tools(task, groups, message=""):
    initial = INITIAL_TOOLS[task]
    if task == "private" and any(word in message for word in APPOINTMENT_WORDS):
        initial += ("schedule_task",)
        if any(word in message for word in CANCELLATION_WORDS):
            initial += ("cancel_scheduled_task",)

    tools = flatten(groups)
    if task in ON_DEMAND_TASKS:
        return tools, ToolGroups(groups, initial=initial)
    return [tool for tool in tools if tool_name(tool) in initial], None
