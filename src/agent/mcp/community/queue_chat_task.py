from src.core.engine import dump


def build(app, owner):
    async def queue_chat_task(channel: str, purpose: str, room_id: int = 0,
                        bulletin_id: str = "", message_id: int = 0) -> str:
        """安排下一项聊天任务，当前任务结束后才处理。private 是所属用户的私聊；group 需要已加入的 room_id。purpose 用一句话说明目的，群聊仅传可公开内容。可引用公开播报 bulletin_id 或目标群内的 message_id。"""
        try:
            return dump(app.social.queue_chat(owner, channel, purpose, room_id, bulletin_id, message_id))
        except ValueError as error:
            return dump({"ok": False, "error": str(error)})

    return queue_chat_task
