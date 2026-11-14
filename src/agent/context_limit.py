from langchain.agents import AgentState
from langchain.agents.middleware import before_model
from langchain_core.messages import trim_messages, RemoveMessage
from langgraph.graph.message import REMOVE_ALL_MESSAGES
from langgraph.runtime import Runtime

from .context_tokens import count_tokens_cjk

# 只是把上下文卡在 N 以内、不做摘要，所以这个值是「硬顶」。
# ⚠️ 必须配 count_tokens_cjk：默认的 "approximate" 是「字符数 ÷ 4」，对中文低估 1.8~3.7 倍，
# 写 100000 实际要 18~37 万才触发。
MAX_CONTEXT_TOKEN = 16_000


@before_model
def cap_context(state: AgentState, runtime: Runtime):
    msgs = state["messages"]  # 不要写成 `state["messages"],` —— 尾逗号会包成 1 元组
    trim = trim_messages(
        messages=msgs,  # 参数名是 messages，写成 msgs 不报错但会返回一个 Runnable，下一行才炸
        max_tokens=MAX_CONTEXT_TOKEN,
        token_counter=count_tokens_cjk,
        strategy="last",
        allow_partial=False,
        start_on="human",  # 本项目系统提示词在 create_agent(system_prompt=) 里，这里是空转
    )
    if len(trim) == len(msgs):
        return None  # 没变就别返回更新，否则每轮都白写一份 checkpoint 全量快照
    return {"messages": [RemoveMessage(id=REMOVE_ALL_MESSAGES), *trim]}

