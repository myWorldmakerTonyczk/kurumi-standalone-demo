from src.core.engine import dump


def build(app, gid):
    async def get_current_workspace() -> str:
        """查看当前绑定的交易工作区。"""
        game = app.engine.get(gid)
        return dump({"workspace": {key: game[key] for key in ("id", "name", "status")}})

    return get_current_workspace
