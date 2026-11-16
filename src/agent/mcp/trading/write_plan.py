def build(trade):
    async def write_plan(text: str, reason: str) -> str:
        """保存本工作区的交易计划和更新理由。"""
        return await trade("plan", text=text, reason=reason)

    return write_plan
