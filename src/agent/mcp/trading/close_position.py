def build(trade):
    async def close_position(position_id: str, reason: str) -> str:
        """按持仓编号模拟平仓。先查询持仓，必须提供理由。"""
        return await trade("close", position=position_id, reason=reason)

    return close_position
