def build(trade):
    async def record_observation(reason: str) -> str:
        """在交易日志记一条观望及其理由。"""
        return await trade("observe", reason=reason)

    return record_observation
