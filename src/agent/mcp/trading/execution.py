from src.core.engine import dump
from src.core.trading import execute


def bind_trade(app, gid, revision):
    async def trade(action, **params):
        try:
            if app.engine.get(gid).get("action_revision", 0) != revision:
                return dump({"ok": False, "error": "账户已变化，请重新查看后再操作"})
            result = await execute(app, gid, action, params, already_locked=True)
            return dump({"ok": True, **result})
        except ValueError as exc:
            return dump({"ok": False, "error": str(exc)})


    return trade
