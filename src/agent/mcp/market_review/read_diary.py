from src.core.engine import dump
from src.core.tool_reads import diary_view


def build(app, gid):
    async def read_diary(before: str = "", limit: int = 3, include_text: bool = False) -> str:
        """读取本工作区已完成的日记，可按日期向前翻页。"""
        try:
            return dump(diary_view(app.diary.store, gid, before=before, limit=limit, include_text=include_text))
        except ValueError as exc:
            return dump({"ok": False, "error": str(exc)})

    return read_diary
