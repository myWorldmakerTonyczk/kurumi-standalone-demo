"""Community actions scoped to the signed-in workspace owner."""
from aiohttp import web

from src.core.engine import dump
from src.agent.image_library import image_reference


def build_tools(app, gid):
    owner = app.engine.get(gid)["owner_id"]

    async def forum(action: str, post_id: int = 0, title: str = "", body: str = "",
                    sticker_id: str = "", title_sticker_id: str = "", category_id: str = "",
                    reply_to: int = 0) -> str:
        """论坛：list 列表、read 读帖和评论、stickers 列出可用表情包（可按 category_id 过滤）。
        对现有帖子的回应使用 comment，传原帖 post_id；回复某条评论时再传该评论的 reply_to。
        publish 仅用于独立新帖。sticker_id 附在正文或评论；title_sticker_id 附在新帖标题。
        发帖和评论须由用户明确要求。
        """
        try:
            if action == "list":
                return dump(app.social.forum_list_for_agent(owner))
            if action == "stickers":
                return dump(app.social.forum_stickers(owner, category_id))
            if action == "publish":
                if post_id or reply_to:
                    raise ValueError("回复已有内容请使用 comment")
                return dump(app.community.handle(owner, "POST", "", {
                    "title": title, "body": body, "sticker_id": sticker_id, "title_sticker_id": title_sticker_id,
                }, is_agent=True))
            if post_id <= 0:
                raise ValueError("请指定帖子编号")
            if action == "read":
                post = app.community.thread(str(post_id))
                result = app.community.handle(owner, "GET", str(post_id), {}, {})
                app.remember_community(owner, post["owner"], post["body"], "forum", stream=post_id, event="post",
                                       is_agent=bool(post["is_agent"]))
                for entry in reversed(result["entries"]):
                    app.remember_community(owner, entry["owner"], entry["body"], "forum",
                                           stream=post_id, event="comment:" + str(entry["id"]), is_agent=bool(entry["is_agent"]))
                return dump({
                    "post": app.social.forum_entry_for_agent(owner, post),
                    "entries": [app.social.forum_entry_for_agent(owner, row) for row in result["entries"]],
                })
            if action == "comment":
                return dump(app.community.handle(owner, "POST", str(post_id), {
                    "body": body, "sticker_id": sticker_id, "reply_to": reply_to or None,
                }, is_agent=True))
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
            if action == "send":
                if not body.strip():
                    raise ValueError("请输入消息内容")
                if not app.social.send(owner, room_id, body.strip()):
                    raise ValueError("智能体未加入该群")
                return dump({"ok": True})
            result = app.community.groups(owner, "GET" if action == "read" else "POST",
                                          str(room_id), {"action": action, "body": body})
            if action == "read":
                for entry in result["entries"]:
                    media = entry.pop("attachment", None)
                    ref = image_reference(media, f"group:{room_id}:{entry['id']}")
                    if ref:
                        entry["image"] = ref
                    elif media:
                        entry["attachment"] = {"kind": media["kind"], "label": media.get("label", "")}
            if result.get("message_id"):
                app.social.on_message(str(room_id), result["message_id"])
            return dump(result)
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
