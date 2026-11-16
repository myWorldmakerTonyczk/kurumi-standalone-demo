"""Compatibility builder for the media tools."""
from src.agent.mcp.media import save_image, search_images, send_saved_image


def build_tools(app, owner, game, attachments, current):
    tools = [save_image.build(app, owner, game, current)]
    if attachments is not None:
        tools += [search_images.build(app, owner), send_saved_image.build(app, owner, attachments)]
    return tools
