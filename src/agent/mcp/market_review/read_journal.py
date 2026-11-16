from src.core.engine import dump
from src.core.journal import journal


def build(app, gid):
    async def read_journal(page: int = 1, pair: str = "", filter: str = "all",
                           source: str = "all", sort: str = "newest", include_text: bool = False,
                           before: int = 0, page_size: int = 15, pages: int = 1) -> str:
        """读取本工作区交易日志，可按品种和类别筛选。"""
        try:
            return dump(journal(app.engine, gid, page=page, pair=pair, filter=filter, source=source, sort=sort,
                include_text=include_text, before=before, page_size=page_size, pages=pages))
        except ValueError as exc:
            return dump({"ok": False, "error": str(exc)})

    return read_journal
