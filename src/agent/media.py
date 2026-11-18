"""Shared image normalization and source resolution for agent sends."""
import base64

from src.portal.community import webp_data_url
from .mcp.market_review.send_review_image import render_review


def normalize_image(value):
    return webp_data_url(value)


def normalize_attachment(attachment):
    if attachment and attachment.get("kind") == "image":
        return {**attachment, "src": normalize_image(attachment["src"])}
    return attachment


async def resolve_image(app, owner, *, image_id=0, reference="", review=None):
    choices = sum(bool(value) for value in (image_id, reference, review))
    if choices > 1:
        raise ValueError("图片参数只填一种")
    if not choices:
        return None
    game = app.owner_game(owner)
    gid = game["id"] if game else None
    if review is not None:
        if not isinstance(review, dict) or set(review) - {"kind", "pair", "days", "end", "kinds", "diary_date", "text"}:
            raise ValueError("复盘图参数无效")
        if not gid:
            raise ValueError("请先创建模拟交易")
        try:
            png = await render_review(app, gid, **review)
        except TypeError as exc:
            raise ValueError("复盘图参数无效") from exc
        return normalize_image("data:image/png;base64," + base64.b64encode(png).decode("ascii"))
    media = app.images.attachment(owner, gid, reference=reference, image_id=image_id)
    return normalize_image(media["src"])
