from aiohttp import web
from src.core.engine import dump


def build(app, owner, game, current):
    async def save_image(reference: str, name: str = "", category: str = "", tags: list[str] | None = None) -> str:
        """收藏图片。reference 使用本轮 current 或读取私聊、群聊、论坛得到的图片引用。首次保存会识图。"""
        try:
            return dump(await app.images.save(owner, game, reference, current, name=name, category=category, tags=tags))
        except (ValueError, web.HTTPException) as error:
            return dump({"ok": False, "error": getattr(error, "reason", str(error))})

    return save_image
