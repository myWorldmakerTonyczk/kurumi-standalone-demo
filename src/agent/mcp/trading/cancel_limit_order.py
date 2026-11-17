def build(trade):
    async def cancel_limit_order(order_id: str, reason: str) -> str:
        """撤销当前模拟账户一笔未成交的限价单。可从账户快照的 orders 获取挂单 ID。"""
        return await trade("cancel_limit_order", order_id=order_id, reason=reason)

    return cancel_limit_order
