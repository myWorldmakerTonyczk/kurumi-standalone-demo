from aiohttp import web
from src.core.engine import dump


def build(app, owner):
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

    return forum
