from src.core.engine import dump


def build(app, owner, public=False):
    async def query_task_timeline(since: float = 0, until: float = 0, kind: str = "", state: str = "",
                            before: int = 0, limit: int = 20) -> str:
        """按 Unix 秒时间段、类型、状态查任务摘要，before 使用上页 next_before。私聊同时返回待执行预约。"""
        result = app.timeline.query(owner, since, until, kind, state, before, limit)
        if not public:
            result["pending_appointments"] = app.task_schedule.pending(owner)
        return dump(result)

    return query_task_timeline
