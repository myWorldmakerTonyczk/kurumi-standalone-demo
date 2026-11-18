from src.core.engine import dump


def build(app, owner, public):
    async def schedule_task(kind: str, purpose: str, trigger: dict, room_id: int = 0,
                            bulletin_id: str = "", message_id: int = 0) -> str:
        """预约一次任务。kind 可选 watch、patrol、private_followup、group_followup。
        trigger: {type: immediate}、{type: delay, seconds}、{type: at, time: ISO8601 带时区}；
        quote 用 pair、side(bid/ask/mid)、condition(cross_up/cross_down/enter_range)、price 或 low/high；
        account 用 event(liquidation/stop_loss/take_profit/margin_call/deposit)；
        bulletin 用 event(live/day/week/month/event，可省略)和可选 title_contains；mention 可用 room_id 限定群。群聊目的仅写可公开内容。
        先检查首轮给出的待执行预约，避免重复登记。到期只表示入队。
        """
        try:
            if public and kind == "private_followup":
                raise ValueError("公开任务不能直接预约私聊")
            return dump(app.task_schedule.schedule(owner, kind, purpose, trigger,
                room_id=room_id, bulletin_id=bulletin_id, message_id=message_id, created_by="agent_tool"))
        except (ValueError, TypeError, OverflowError) as error:
            return dump({"ok": False, "error": str(error)})

    return schedule_task
