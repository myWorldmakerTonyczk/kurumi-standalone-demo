import json
from src.core.engine import dump
from src.notifications.settings import validate


def build_tools(app, gid):
    async def set_notifications(settings: str) -> str:
        """修改当前用户的汇报配置，settings 为 JSON 对象。"""
        game = app.engine.get(gid)
        owner = game["owner_id"]
        try:
            prefs = app.preferences(owner, game)
            prefs["notices"] = validate(prefs["notices"] | json.loads(settings))
            app.db.execute("INSERT OR REPLACE INTO demo_profiles VALUES(?,?)",
                           (owner, dump(app.profile(owner) | {"preferences": prefs})))
            return dump({"ok": True, "notices": prefs["notices"]})
        except (TypeError, ValueError) as exc:
            return dump({"ok": False, "error": str(exc)})

    return [set_notifications]
