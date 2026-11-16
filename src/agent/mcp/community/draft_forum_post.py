from aiohttp import web
from src.core.engine import dump


def build(app, gid, owner):
    async def draft_forum_post(topic: str = "", source: str = "", trade_review: bool = False) -> str:
        """生成论坛草稿，不会自动发帖。"""
        try:
            return dump(await app.draft_forum_post(app.engine.get(gid), owner,
                                                   {"topic": topic, "source": source, "trade_review": trade_review}))
        except (ValueError, web.HTTPException) as exc:
            return dump({"ok": False, "error": getattr(exc, "reason", str(exc))})

    return draft_forum_post
