from src.core.engine import dump


def build(app):
    async def market() -> str:
        """查看当前公开外汇报价。"""
        return dump(app.engine.quotes)

    return market
