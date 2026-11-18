from aiohttp import web
from src.core.engine import dump
from src.agent.image_library import image_reference as describe_reference


def build(app, owner, game=None, current_image=None):
    async def group_chat(action: str, room_id: int = 0, name: str = "", body: str = "",
                         private: bool = False, image_reference: str = "", image_id: int = 0) -> str:
        """群聊：list 列群、create 建群、join 入群、leave 退群、read 读消息。
        send 直接发送已确定的文字或图片，body 是配文，可只发图片。
        handoff 排队交给目标群聊任务，等当前任务结束后读取群聊上下文再决定是否发，
        body 是简短的交接目的，可附图片供该任务发送。群聊只能收到明确允许公开的内容。
        图片使用 image_reference（例如 current、private:302:0）或图库 image_id，两者选一。
        变更须由用户明确要求。handoff 返回已排队，不能当作已经发送。
        """
        try:
            if (image_reference or image_id) and action not in {"send", "handoff"}:
                raise ValueError("图片参数只用于 send 或 handoff")
            if action in {"list", "create"}:
                return dump(app.community.groups(owner, "GET" if action == "list" else "POST", "", {"name": name, "private": private}))
            if action not in {"join", "leave", "read", "send", "handoff"} or room_id <= 0:
                raise ValueError("请指定有效的群操作及群编号")
            if action in {"send", "handoff"}:
                if not app.social.joined(room_id, owner):
                    raise ValueError("智能体未加入该群")
                attachment = app.images.attachment(owner, game, reference=image_reference,
                    image_id=image_id, current=current_image) if image_reference or image_id else None
                if action == "handoff":
                    return dump(app.social.queue_chat(owner, "group", body, room_id, attachment=attachment))
                if not body.strip() and not attachment:
                    raise ValueError("请填写消息内容或指定图片")
                if not app.social.send(owner, room_id, body.strip(), attachment):
                    raise ValueError("当前无法向该群发送消息")
                return dump({"ok": True, "sent": True})
            result = app.community.groups(owner, "GET" if action == "read" else "POST",
                                          str(room_id), {"action": action, "body": body})
            if action == "read":
                for entry in result["entries"]:
                    media = entry.pop("attachment", None)
                    ref = describe_reference(media, f"group:{room_id}:{entry['id']}")
                    if ref:
                        entry["image"] = ref
                    elif media:
                        entry["attachment"] = {"kind": media["kind"], "label": media.get("label", "")}
            if result.get("message_id"):
                app.social.on_message(str(room_id), result["message_id"])
            return dump(result)
        except (ValueError, web.HTTPException) as exc:
            return dump({"ok": False, "error": getattr(exc, "reason", str(exc))})

    return group_chat
