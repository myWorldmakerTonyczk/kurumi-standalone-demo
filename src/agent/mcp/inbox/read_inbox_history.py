from src.core.engine import dump


def build(inbox):
    def read_inbox_history(source: str = "", room: int = 0, before: int = 0,
                           query: str = "", since: float = 0, until: float = 0, limit: int = 30) -> str:
        """查已收和未收的历史邮件，不修改已读。before 使用上页 next_before，since/until 是 Unix 秒。"""
        result = inbox.history(owner, source, room, before, query[:80], since, until, limit)
        inbox.audit(owner, "history", len(result["items"]))
        return dump(result)

    return read_inbox_history
