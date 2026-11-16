from src.core.engine import dump


def build(trade):
    async def virtual_loan(action: str, amount: str, reason: str) -> str:
        """借入或归还虚拟资金，action 为 borrow 或 repay。"""
        if action not in {"borrow", "repay"}:
            return dump({"ok": False, "error": "仅支持 borrow 或 repay"})
        return await trade(action, amount=amount, reason=reason)

    return virtual_loan
