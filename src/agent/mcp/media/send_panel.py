from src.core.engine import dump


def build(attachments):
    async def send_panel() -> str:
        """发送当前工作区链接。"""
        attachments.append({"kind": "panel", "label": "打开交易工作区"})
        return dump({"ok": True, "message": "已附上工作区链接"})

    return send_panel
