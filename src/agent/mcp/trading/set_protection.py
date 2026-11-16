def build(trade):
    async def set_protection(position_id: str, reason: str, sl: str | None = None,
                             tp: str | None = None) -> str:
        """设置持仓止损或止盈。省略对应参数即保留原值，空字符串清除。"""
        params = {key: value for key, value in {"sl": sl, "tp": tp}.items() if value is not None}
        return await trade("protect", position=position_id, reason=reason, **params)

    return set_protection
