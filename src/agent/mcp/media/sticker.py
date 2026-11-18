from langchain_core.tools import tool
from src.core.engine import dump


def build(app, owner, attachments, *, single=False):
    @tool
    async def sticker(action: str, sticker_id: str = "", category_id: str = "") -> str:
        """表情包：list 查看分类和使用说明，send 按 category_id 随机发送一张。
        如需指定图片，先用 items 查看图片 ID，再用 send 传 sticker_id。
        只有分类适合当前语气时才发送。
        """
        if action == "list":
            return dump([category for category in app.stickers.categories(owner) if category["count"]])
        if action == "items":
            return dump([{key: entry[key] for key in ("id", "label", "category")}
                         for entry in app.stickers.list(owner) if not category_id or entry["category"] == category_id])
        if action != "send":
            return "请选择 list、items 或 send"
        if not sticker_id and not category_id:
            return "请先用 list 查看分类，再用 send 传 category_id，或传指定的 sticker_id。"
        entry = (app.stickers.find(owner, sticker_id) if sticker_id else
                 app.stickers.pick(owner, category_id))
        if not entry:
            return "找不到该表情包"
        if single and attachments:
            return "本轮已有一个附件，不能再发表情包"
        if any(item["kind"] == "sticker" for item in attachments):
            return "本轮已发送一张表情包"
        attachments.append(dict(kind="sticker", label=entry["label"], src=entry["src"]))
        return "表情包已附在回复中"

    return sticker
