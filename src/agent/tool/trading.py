import json
from src.core.engine import dump
from src.core.trading import execute


def build_tools(app, gid, writable, revision):
    async def trade(action, **params):
        try:
            if app.engine.get(gid).get("action_revision", 0) != revision:
                return dump({"ok": False, "error": "账户已变化，请重新查看后再操作"})
            result = await execute(app, gid, action, params, already_locked=True)
            return dump({"ok": True, **result})
        except ValueError as exc:
            return dump({"ok": False, "error": str(exc)})

    async def open_trade(side: str, units: int, reason: str, pair: str = "EUR/USD",
                         sl: str = "", tp: str = "") -> str:
        """模拟开仓，方向为 buy 或 sell，必须提供理由。可填止损和止盈价格。"""
        return await trade("open", pair=pair, side=side, units=units, reason=reason, sl=sl, tp=tp)

    async def close_position(position_id: str, reason: str) -> str:
        """按持仓编号模拟平仓。先查询持仓，必须提供理由。"""
        return await trade("close", position=position_id, reason=reason)

    async def set_protection(position_id: str, reason: str, sl: str | None = None,
                             tp: str | None = None) -> str:
        """设置持仓止损或止盈。省略对应参数即保留原值，空字符串清除。"""
        params = {key: value for key, value in {"sl": sl, "tp": tp}.items() if value is not None}
        return await trade("protect", position=position_id, reason=reason, **params)

    async def write_plan(text: str, reason: str) -> str:
        """保存本工作区的交易计划和更新理由。"""
        return await trade("plan", text=text, reason=reason)

    async def virtual_loan(action: str, amount: str, reason: str) -> str:
        """借入或归还虚拟资金，action 为 borrow 或 repay。"""
        if action not in {"borrow", "repay"}:
            return dump({"ok": False, "error": "仅支持 borrow 或 repay"})
        return await trade(action, amount=amount, reason=reason)

    async def write_memory(kind: str, text: str, reason: str) -> str:
        """保存交易计划，kind 填 plan。"""
        if kind != "plan":
            return dump({"ok": False, "error": "仅支持保存交易计划"})
        return await write_plan(text, reason)

    async def forex(action: str, parameters: str = "{}") -> str:
        """通用交易入口。支持 state、logs、open、close、observe 和 plan。"""
        try:
            values = json.loads(parameters)
            if not isinstance(values, dict):
                raise ValueError("参数须为 JSON 对象")
            if action in {"state", "logs"}:
                return dump(await execute(app, gid, action, values, already_locked=True))
            if not writable or action not in {"open", "close", "observe", "plan"}:
                raise ValueError("当前操作不可用")
            return await trade(action, **values)
        except (TypeError, ValueError) as exc:
            return dump({"ok": False, "error": str(exc)})

    return [forex, open_trade, close_position, set_protection, write_plan, virtual_loan, write_memory] if writable else [forex]
