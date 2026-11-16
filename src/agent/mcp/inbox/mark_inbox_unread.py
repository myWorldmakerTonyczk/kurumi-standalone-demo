from src.core.engine import dump


def build(inbox):
    def mark_inbox_unread(item_id: int = 0, batch_id: str = "", source: str = "", room: int = 0) -> str:
        """将单条邮箱消息，或一次 read_inbox 的批次标回未读。批次可用 source 或 room 缩小范围；item_id 是邮箱条目 ID。"""
        return dump(inbox.mark_unread(owner, item_id, batch_id, source, room))

    return mark_inbox_unread
