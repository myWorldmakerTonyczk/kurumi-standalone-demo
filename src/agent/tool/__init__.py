"""Tools for the authenticated workspace."""
from random import random

from . import community, delivery, notifications, queries, stickers, trading


def workspace_tools(app, gid, *, writable, revision, attachments=None):
    tools = queries.build_tools(app, gid) + trading.build_tools(app, gid, writable, revision)
    if attachments is not None:
        tools += delivery.build_tools(app, gid, attachments) + community.build_tools(app, gid)
        owner = app.engine.get(gid)["owner_id"]
        if random() * 100 < app.profile(owner).get("sticker_probability", 100):
            tools += stickers.build_tools(app, gid, attachments)
        if writable:
            tools += notifications.build_tools(app, gid)
    return tools
