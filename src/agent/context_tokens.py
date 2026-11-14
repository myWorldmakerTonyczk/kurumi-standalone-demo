"""Context sizing adapted from ForumAgent."""
import math
from typing import Iterable
from langchain_core.messages import AnyMessage

from langgraph.graph.message import REMOVE_ALL_MESSAGES

_CJK_PER_TOKEN = 1.0  # 实测：640 个汉字 → 真实 BPE 600 token
_ASCII_CHARS_PER_TOKEN = 4.0  # 实测：英文 648 字符 → 145 token
_EXTRA_TOKENS_PER_MESSAGE = 3.0  # 每条消息的角色标记等固定开销，和官方取同一个值


def _is_cjk(ch: str) -> bool:
    """汉字、中文标点、全角字符 —— 这些在 BPE 里基本各自占一个 token。"""
    o = ord(ch)
    return (
        0x4E00 <= o <= 0x9FFF
        or 0x3400 <= o <= 0x4DBF
        or 0xF900 <= o <= 0xFAFF
        or 0x3000 <= o <= 0x303F
        or 0xFF00 <= o <= 0xFFEF
    )


def count_tokens_cjk(messages: Iterable[AnyMessage]) -> int:
    """汉字按 ~1 token、ASCII 按 ~4 字符 1 token 分开算。

    笼统的「总字符数 ÷ 4」对本项目（正文、话题名、工具返回全是中文）会低估一个量级。
    工具调用的参数也算进去 —— `AIMessage` 的正文常常是空的，光看 `content`
    会把带工具调用的那条算成 0。

    仍是估算。要绝对准就得拿服务端回的 `usage_metadata.total_tokens` 对账。
    """
    total = 0.0
    for m in messages:
        content = m.content if isinstance(m.content, str) else str(m.content)
        parts = [content]
        parts += [str(c.get("args", "")) for c in getattr(m, "tool_calls", None) or []]
        for text in parts:
            cjk = sum(1 for ch in text if _is_cjk(ch))
            total += cjk / _CJK_PER_TOKEN + (len(text) - cjk) / _ASCII_CHARS_PER_TOKEN
        total += _EXTRA_TOKENS_PER_MESSAGE
    return math.ceil(total)
