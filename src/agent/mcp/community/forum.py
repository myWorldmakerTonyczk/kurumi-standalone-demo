from aiohttp import web
from src.core.engine import dump
from ..media.sources import resolve_image


def build(app, owner, published=None):
    async def forum(action: str, post_id: int = 0, title: str = "", body: str = "",
                    sticker_id: str = "", title_sticker_id: str = "", category_id: str = "",
                    reply_to: int = 0, image_id: int = 0, image_reference: str = "",
                    title_image_id: int = 0, title_image_reference: str = "", review: dict | None = None) -> str:
        """论坛：list 列表、read 读帖和评论、stickers 列出可用表情包（可按 category_id 过滤）。
        对现有帖子的回应使用 comment，传原帖 post_id；回复某条评论时再传该评论的 reply_to。
        publish 仅用于独立新帖。sticker_id 附在正文或评论；title_sticker_id 附在新帖标题。
        图片可用 image_id/image_reference（标题用 title_image_id/title_image_reference），
        或传 review 对象生成复盘图；每处只能附一张图片或表情包。
        发帖和评论须由用户明确要求。
        """
        try:
            if published is not None and action in ("comment", "publish") and published[0]:
                return "本次巡视已完成一次公开发言，请结束。"
            if action == "list":
                return dump(app.social.forum_list_for_agent(owner))
            if action == "stickers":
                return dump(app.social.forum_stickers(owner, category_id))
            if action == "publish":
                if post_id or reply_to:
                    raise ValueError("回复已有内容请使用 comment")
                image = await resolve_image(app, owner, image_id=image_id,
                                            reference=image_reference, review=review)
                title_image = await resolve_image(app, owner, image_id=title_image_id,
                                                  reference=title_image_reference)
                result = app.community.handle(owner, "POST", "", {
                    "title": title, "body": body, "sticker_id": sticker_id,
                    "title_sticker_id": title_sticker_id,
                    **({"image": image} if image else {}),
                    **({"title_image": title_image} if title_image else {}),
                }, is_agent=True)
                if published is not None and result.get("id"):
                    published[0] = True
                return dump(result)
            if post_id <= 0:
                raise ValueError("请指定帖子编号")
            if action == "read":
                post = app.community.thread(str(post_id))
                result = app.community.handle(owner, "GET", str(post_id), {}, {})
                if hasattr(app, "remember_community"):
                    app.remember_community(owner, post["owner"], post["body"], "forum", stream=post_id,
                                           event="post", is_agent=bool(post["is_agent"]))
                    for entry in reversed(result["entries"]):
                        app.remember_community(owner, entry["owner"], entry["body"], "forum",
                                               stream=post_id, event="comment:" + str(entry["id"]),
                                               is_agent=bool(entry["is_agent"]))
                return dump({
                    "post": app.social.forum_entry_for_agent(owner, post),
                    "entries": [app.social.forum_entry_for_agent(owner, row) for row in result["entries"]],
                })
            if action == "comment":
                image = await resolve_image(app, owner, image_id=image_id,
                                            reference=image_reference, review=review)
                result = app.community.handle(owner, "POST", str(post_id), {
                    "body": body, "sticker_id": sticker_id, "reply_to": reply_to or None,
                    **({"image": image} if image else {}),
                }, is_agent=True)
                if published is not None and result.get("id"):
                    published[0] = True
                return dump(result)
            raise ValueError("论坛操作无效")
        except (ValueError, web.HTTPException) as exc:
            return dump({"ok": False, "error": getattr(exc, "reason", str(exc))})

    return forum
