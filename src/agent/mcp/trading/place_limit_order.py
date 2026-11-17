def build(trade):
    async def place_limit_order(side: str, units: int, limit_price: str, reason: str,
                                pair: str = "EUR/USD", sl: str = "", tp: str = "",
                                leverage: str | None = None) -> str:
        """提交模拟限价开仓单。buy 按 Ask 不高于限价成交，sell 按 Bid 不低于限价成交。
        返回挂单 ID 和 waiting 状态，成交由行情更新驱动。可填止损、止盈和杠杆。"""
        extra = {"leverage": leverage} if leverage is not None else {}
        return await trade("place_limit_order", pair=pair, side=side, units=units, limit_price=limit_price,
                           reason=reason, sl=sl, tp=tp, **extra)

    return place_limit_order
