from aiohttp import web
from src.core.engine import dump
from src.agent.image_library import image_reference


def build(app, owner):
    async def group_chat(action: str, room_id: int = 0, name: str = "", body: str = "",
                         private: bool = False) -> str:
        """群聊：list 列群、create 建群、join 加入公开群或申请加入私有群、leave 退出、read 读消息、send 发消息。变更须由用户明确要求。"""
        try:
            if action in {"list", "create"}:
                return dump(app.community.groups(owner, "GET" if action == "list" else "POST", "", {"name": name, "private": private}))
            if action not in {"join", "leave", "read", "send"} or room_id <= 0:
                raise ValueError("请指定有效的群操作及群编号")
            if action == "send":
                if not body.strip():
                    raise ValueError("请输入消息内容")
                if not app.social.send(owner, room_id, body.strip()):
                    raise ValueError("智能体未加入该群")
                return dump({"ok": True})
            result = app.community.groups(owner, "GET" if action == "read" else "POST",
                                          str(room_id), {"action": action, "body": body})
            if action == "read":
                for entry in result["entries"]:
                    media = entry.pop("attachment", None)
                    ref = image_reference(media, f"group:{room_id}:{entry['id']}")
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
