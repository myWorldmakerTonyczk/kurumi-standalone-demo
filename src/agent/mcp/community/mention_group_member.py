from src.core.engine import dump


def build(roster, self_id):
    async def mention_group_member(member_id: str) -> str:
        """按 members 里的成员 ID 获取本群的正确 @ 写法。把返回的 mention 放入最终回复才会发送。"""
        member = roster.get(member_id)
        if not member or member_id == self_id:
            return dump({"error": "请选择当前群里的其他成员"})
        return dump({"member_id": member_id, "mention": member["mention"]})

    return mention_group_member
