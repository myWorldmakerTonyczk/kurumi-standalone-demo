"""Community actions scoped to the signed-in workspace owner."""
from aiohttp import web

from src.core.engine import dump


def build_tools(app, gid):
    owner = app.engine.get(gid)["owner_id"]

    async def forum(action: str, post_id: int = 0, title: str = "", body: str = "") -> str:
        """论坛：list 列表、read 读帖和评论、publish 发帖、comment 评论。发帖和评论须由用户明确要求。"""
        try:
            if action == "list":
                return dump(app.community.handle(owner, "GET", "", {}, {}))
            if action == "publish":
                return dump(app.community.handle(owner, "POST", "", {"title": title, "body": body}))
            if post_id <= 0:
                raise ValueError("请指定帖子编号")
            if action == "read":
                return dump({"post": app.community.thread(str(post_id)),
                             **app.community.handle(owner, "GET", str(post_id), {}, {})})
            if action == "comment":
                return dump(app.community.handle(owner, "POST", str(post_id), {"body": body}))
            raise ValueError("论坛操作无效")
        except (ValueError, web.HTTPException) as exc:
            return dump({"ok": False, "error": getattr(exc, "reason", str(exc))})

    async def group_chat(action: str, room_id: int = 0, name: str = "", body: str = "",
                         private: bool = False) -> str:
        """群聊：list 列群、create 建群、join 加入公开群或申请加入私有群、leave 退出、read 读消息、send 发消息。变更须由用户明确要求。"""
        try:
            if action in {"list", "create"}:
                return dump(app.community.groups(owner, "GET" if action == "list" else "POST", "", {"name": name, "private": private}))
            if action not in {"join", "leave", "read", "send"} or room_id <= 0:
                raise ValueError("请指定有效的群操作及群编号")
            return dump(app.community.groups(owner, "GET" if action == "read" else "POST",
                                            str(room_id), {"action": action, "body": body}))
        except (ValueError, web.HTTPException) as exc:
            return dump({"ok": False, "error": getattr(exc, "reason", str(exc))})

    async def summarize_group(room_id: int = 0, limit: int = 80) -> str:
        """只总结指定群的公开消息；不会自动发送摘要。"""
        try:
            game = app.engine.get(gid)
            return dump(await app.summarize_group(game, owner, room_id, limit=limit))
        except (ValueError, web.HTTPException) as exc:
            return dump({"ok": False, "error": getattr(exc, "reason", str(exc))})

    async def generate_trade_review(days: int = 1, pair: str = "") -> str:
        """生成模拟交易复盘草稿；发布前仍需用户确认。"""
        try:
            return dump(await app.generate_trade_review(app.engine.get(gid), days=days, pair=pair))
        except (ValueError, web.HTTPException) as exc:
            return dump({"ok": False, "error": getattr(exc, "reason", str(exc))})

    async def draft_forum_post(topic: str = "", source: str = "", trade_review: bool = False) -> str:
        """生成论坛草稿，不会自动发帖。"""
        try:
            return dump(await app.draft_forum_post(app.engine.get(gid), owner,
                                                   {"topic": topic, "source": source, "trade_review": trade_review}))
        except (ValueError, web.HTTPException) as exc:
            return dump({"ok": False, "error": getattr(exc, "reason", str(exc))})

    return [forum, group_chat, summarize_group, generate_trade_review, draft_forum_post]
