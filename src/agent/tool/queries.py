from src.core.engine import dump
from src.core.trading import public_snapshot
from src.core.journal import journal
from src.core.tool_reads import diary_view, history_view


def build_tools(app, gid):
    async def watch_market(log_limit: int = 10, include_details: bool = False) -> str:
        """查询本工作区的余额、持仓、报价和近期记录。"""
        return dump(public_snapshot(app, gid, log_limit, include_details))

    async def price_history(pair: str, period: str = "1h", limit: int = 20, before: int = 0) -> str:
        """查询真实历史 K 线和区间摘要，before 使用上一页的 next_before。"""
        try:
            return dump(await history_view(app, pair=pair, period=period, limit=limit, before=before))
        except ValueError as exc:
            return dump({"ok": False, "error": str(exc)})

    async def read_journal(page: int = 1, pair: str = "", filter: str = "all") -> str:
        """读取本工作区交易日志，可按品种和类别筛选。"""
        try:
            return dump(journal(app.engine, gid, page=page, pair=pair, filter=filter, page_size=15))
        except ValueError as exc:
            return dump({"ok": False, "error": str(exc)})

    async def read_diary(before: str = "", limit: int = 3, include_text: bool = False) -> str:
        """读取本工作区已完成的日记，可按日期向前翻页。"""
        try:
            return dump(diary_view(app.diary.store, gid, before=before, limit=limit, include_text=include_text))
        except ValueError as exc:
            return dump({"ok": False, "error": str(exc)})

    async def my_instances() -> str:
        """列出当前用户的交易工作区。"""
        game = app.engine.get(gid)
        return dump({"instances": [{key: game[key] for key in ("id", "name", "status")}]})

    async def select_instance(instance_id: str) -> str:
        """确认当前聊天所使用的工作区编号。"""
        return await my_instances() if instance_id == gid else dump({"ok": False, "error": "此工作区不可访问"})

    return [watch_market, price_history, read_journal, read_diary, my_instances, select_instance]
