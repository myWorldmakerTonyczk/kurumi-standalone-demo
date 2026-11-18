from src.core.engine import dump
from ..media.sources import resolve_image


def build(social, owner, published):
    async def group(action: str, room: str = "", body: str = "", image_id: int = 0,
                    image_reference: str = "", review: dict | None = None) -> str:
        """列出已加入的群聊、读群或发言。action 是 list、read 或 send。
        send 可附 image_id、image_reference 或 review（生成复盘图）。
        """
        try:
            if (image_id or image_reference or review is not None) and action != "send":
                raise ValueError("图片参数只用于 send")
            if action == "send" and published[0]:
                return "本次巡视已完成一次公开发言，请结束。"
            if action == "list":
                return dump([dict(row) for row in social.db.execute("""SELECT g.id,g.name FROM community_groups g
                    JOIN community_group_agents a ON a.room=g.id WHERE a.owner=?""", (owner,))])
            if not social.joined(room, owner):
                return "智能体未加入该群"
            if action == "read":
                data = social.app.community.groups(owner, "GET", room, {})
                return dump([dict(name=social.identity(row["owner"]) if row["is_agent"] else row["nickname"],
                                  message=row["body"]) for row in reversed(data["entries"][:12])])
            if action == "send":
                image = await resolve_image(social.app, owner, image_id=image_id,
                                            reference=image_reference, review=review)
                if not body.strip() and not image:
                    return "请输入消息内容或图片"
                attachment = {"kind": "image", "src": image, "label": "图片"} if image else None
                published[0] = social.send(owner, room, body.strip(), attachment)
                return "已发送，请结束本次巡视" if published[0] else "智能体已离开"
            return "操作无效"
        except ValueError as exc:
            return str(exc)

    return group
