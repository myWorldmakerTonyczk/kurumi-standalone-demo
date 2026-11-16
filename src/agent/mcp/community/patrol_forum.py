from aiohttp import web
from src.core.engine import dump


def build(social, owner, published):
    async def forum(action: str, title: str = "", body: str = "", sticker_id: str = "",
                    title_sticker_id: str = "", post_id: int = 0, reply_to: int = 0) -> str:
        """list 看帖子列表，read 按 post_id 看原帖和评论，stickers 看表情包。
        回应原帖用 comment 并传 post_id，回复评论时再传 reply_to。publish 仅用于独立新帖。
        """
        try:
            if action in ("comment", "publish") and published[0]:
                return "本次巡视已完成一次公开发言，请结束。"
            if action == "stickers":
                return dump(social.forum_stickers(owner))
            if action == "list":
                return dump(social.forum_list_for_agent(owner))
            if action == "read" and post_id > 0:
                post = social.app.community.thread(str(post_id))
                result = social.app.community.handle(owner, "GET", str(post_id), {})
                return dump({
                    "post": social.forum_entry_for_agent(owner, post),
                    "entries": [social.forum_entry_for_agent(owner, row) for row in result["entries"]],
                })
            if social.app.external.settings(owner)["mode"] == "platform":
                if action == "comment" and post_id > 0:
                    result = social.app.community.handle(owner, "POST", str(post_id), {
                        "body": body, "sticker_id": sticker_id, "reply_to": reply_to or None,
                    }, is_agent=True)
                    published[0] = True
                    return dump(result)
                if action == "publish" and not (post_id or reply_to):
                    result = social.app.community.handle(owner, "POST", "", {
                        "title": title, "body": body, "sticker_id": sticker_id,
                        "title_sticker_id": title_sticker_id,
                    }, is_agent=True)
                    published[0] = True
                    return dump(result)
            return "操作无效。回应已有帖子请使用 comment 并指定原帖 post_id。"
        except (ValueError, web.HTTPException) as exc:
            return dump({"ok": False, "error": getattr(exc, "reason", str(exc))})

    return forum
