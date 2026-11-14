"""Tools for the authenticated workspace."""
from . import community, delivery, notifications, queries, trading


def workspace_tools(app, gid, *, writable, revision, attachments=None):
    tools = queries.build_tools(app, gid) + trading.build_tools(app, gid, writable, revision)
    if attachments is not None:
        tools += delivery.build_tools(app, gid, attachments) + community.build_tools(app, gid)
        if writable:
            tools += notifications.build_tools(app, gid)
    return tools
