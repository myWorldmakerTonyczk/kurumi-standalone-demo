from src.core.engine import dump
from src.core.trading import public_snapshot


def build(app, gid):
    async def watch_market(log_limit: int = 10, include_details: bool = False) -> str:
        """查询本工作区的余额、持仓、报价和近期记录。"""
        return dump(public_snapshot(app, gid, log_limit, include_details))

    return watch_market
