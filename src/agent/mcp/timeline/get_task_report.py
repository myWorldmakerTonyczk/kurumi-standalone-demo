from src.core.engine import dump


def build(app, owner, public):
    def get_task_report(task_id: int) -> str:
        """通过任务 ID 查看状态、时间、执行报告和引用。公开会话仅返回摘要。"""
        return dump(app.timeline.report(owner, task_id, summary_only=public) or {"error": "任务不存在"})

    return get_task_report
