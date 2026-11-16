from aiohttp import web
from src.core.engine import dump


def build(app, owner, attachments):
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

    return send_saved_image
