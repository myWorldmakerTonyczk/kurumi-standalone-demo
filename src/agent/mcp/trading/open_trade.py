def build(trade):
    async def open_trade(side: str, units: int, reason: str, pair: str = "EUR/USD",
                         sl: str = "", tp: str = "", leverage: str | None = None) -> str:
        """模拟开仓，方向为 buy 或 sell，必须提供理由。可填止损和止盈价格。"""
        extra = {"leverage": leverage} if leverage is not None else {}
        return await trade("open", pair=pair, side=side, units=units, reason=reason, sl=sl, tp=tp, **extra)

    return open_trade
