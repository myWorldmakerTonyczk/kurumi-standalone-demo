from aiohttp import web
from src.core.engine import dump


def build(app, gid):
    async def generate_trade_review(days: int = 1, pair: str = "") -> str:
        """生成模拟交易复盘草稿；发布前仍需用户确认。"""
        try:
            return dump(await app.generate_trade_review(app.engine.get(gid), days=days, pair=pair))
        except (ValueError, web.HTTPException) as exc:
            return dump({"ok": False, "error": getattr(exc, "reason", str(exc))})

    return generate_trade_review
