from src.core.engine import dump
from src.core.tool_reads import history_view


def build(app):
    async def price_history(pair: str, period: str = "1h", limit: int = 20, before: int = 0) -> str:
        """查询真实历史 K 线和区间摘要。limit 为 10–200 根，默认 20。before 使用上一页的 next_before。"""
        try:
            return dump(await history_view(app, pair=pair, period=period, limit=limit, before=before))
        except ValueError as exc:
            return dump({"ok": False, "error": str(exc)})

    return price_history
