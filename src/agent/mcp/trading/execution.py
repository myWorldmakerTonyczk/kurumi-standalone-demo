from src.core.engine import dump
from src.core.trading import execute


WATCH_OUTCOME_ACTIONS = {"observe", "open", "close", "protect", "place_limit_order", "cancel_limit_order", "borrow", "repay"}


def bind_trade(app, gid, revision, *, watch_actions=None):
    async def trade(action, **params):
        nonlocal revision
        try:
            if action == "observe" and watch_actions is not None and not WATCH_OUTCOME_ACTIONS.isdisjoint(watch_actions):
                return dump({"ok": True, "recorded": False, "message": "本轮已有操作记录，无需再记观望"})
            if app.engine.get(gid).get("action_revision", 0) != revision:
                return dump({"ok": False, "error": "账户在本轮执行期间被其他操作更新。请停止本轮写入并报告，下一轮重新评估。"})
            result = await execute(app, gid, action, params, already_locked=True)
            revision = app.engine.get(gid).get("action_revision", 0)
            if watch_actions is not None:
                watch_actions.add(action)
            return dump({"ok": True, **result})
        except ValueError as exc:
            return dump({"ok": False, "error": str(exc)})


    return trade
