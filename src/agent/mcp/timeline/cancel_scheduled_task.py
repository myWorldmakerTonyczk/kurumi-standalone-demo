from src.core.engine import dump


def build(app, owner):
    async def cancel_scheduled_task(appointment_id: int) -> str:
        """取消本账号的待执行预约。appointment_id 来自时间线中的 pending_appointments。"""
        try:
            return dump(app.task_schedule.cancel(owner, appointment_id))
        except ValueError as exc:
            return dump({"ok": False, "error": str(exc)})

    return cancel_scheduled_task
