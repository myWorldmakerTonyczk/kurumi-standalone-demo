"""Image library tools for private chat."""
from aiohttp import web

from src.core.engine import dump


def build_tools(app, owner, game, attachments, current):
    async def save_image(reference: str, name: str = "", category: str = "", tags: list[str] | None = None) -> str:
        """收藏图片。reference 使用本轮 current 或读取私聊、群聊、论坛得到的图片引用。首次保存会识图。"""
        try:
            return dump(await app.images.save(owner, game, reference, current, name=name, category=category, tags=tags))
        except (ValueError, web.HTTPException) as error:
            return dump({"ok": False, "error": getattr(error, "reason", str(error))})

    async def search_images(query: str = "", category: str = "", tag: str = "", source: str = "",
                            since: int = 0) -> str:
        """搜索已收藏图片。query 匹配名称、标签和描述。source 可填 private、group、post 或 comment。since 是保存时间的 Unix 秒。"""
        return dump(app.images.search(owner, query, category, tag, source, since, limit=15))

    async def send_saved_image(image_id: int) -> str:
        """把图库中的指定图片发到当前私聊。用户明确想看该图时调用。"""
        try:
            row = app.images.get(owner, image_id)
            if sum(item["kind"] == "image" for item in attachments) >= 2:
                raise ValueError("一轮最多发送两张图片")
            attachments.append({"kind": "image", "src": row["src"], "label": row["name"]})
            return dump({"ok": True, "name": row["name"]})
        except (ValueError, web.HTTPException) as error:
            return dump({"ok": False, "error": getattr(error, "reason", str(error))})

    return [save_image, search_images, send_saved_image] if attachments is not None else [save_image]
