from src.core.engine import dump


def build(app, owner):
    def query_task_timeline(since: float = 0, until: float = 0, kind: str = "", state: str = "",
                            before: int = 0, limit: int = 20) -> str:
        """按 Unix 秒时间段、类型、状态查任务摘要，before 使用上页 next_before。"""
        return dump(app.timeline.query(owner, since, until, kind, state, before, limit))

    return query_task_timeline
