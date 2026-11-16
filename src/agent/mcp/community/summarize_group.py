from aiohttp import web
from src.core.engine import dump


def build(app, gid, owner):
    async def summarize_group(room_id: int = 0, limit: int = 80) -> str:
        """只总结指定群的公开消息；不会自动发送摘要。"""
        try:
            game = app.engine.get(gid)
            return dump(await app.summarize_group(game, owner, room_id, limit=limit))
        except (ValueError, web.HTTPException) as exc:
            return dump({"ok": False, "error": getattr(exc, "reason", str(exc))})

    return summarize_group
