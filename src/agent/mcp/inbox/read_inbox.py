from src.core.engine import dump


def build(inbox):
    def read_inbox(sources: list[str] | None = None, peek: bool = False) -> str:
        """收取智能体邮箱未读。sources 可选 forum/group/market/system。默认标记本次全部已读；peek 只预览。返回批次 ID、群未读数和每群最近五条，正文按需要调用原文工具。"""
        return dump(inbox.read(owner, sources, peek))

    return read_inbox
