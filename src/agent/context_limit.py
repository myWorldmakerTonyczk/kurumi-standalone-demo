from langchain.agents import AgentState
from langchain.agents.middleware import before_model
from langchain_core.messages import trim_messages, RemoveMessage
from langgraph.graph.message import REMOVE_ALL_MESSAGES
from langgraph.runtime import Runtime

from .context_tokens import count_tokens_cjk
from .workflow import workflow
from .timeline import task_trace

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
    if trace := task_trace():
        trace.event("context", "message_trim", {
            "messages_before": len(msgs), "messages_after": len(trim), "estimated_token_limit": MAX_CONTEXT_TOKEN,
        })
    return {"messages": [RemoveMessage(id=REMOVE_ALL_MESSAGES), *trim]}




async def manage_history(history, policy, summary, validate_url, *, audit=None):
    from langchain_core.messages import HumanMessage, SystemMessage
    from .model_registry import model_client

    rounds = policy.get("rounds", 10)
    limit = len(history) if rounds == -1 else rounds * 2
    over_tokens = count_tokens_cjk(history) > MAX_CONTEXT_TOKEN
    if len(history) <= limit and not over_tokens:
        return history
    discard = max(2 * policy.get("discard", 1), len(history) - limit)
    if policy.get("strategy") == "summary" and summary.get("key") and len(history) > 2:
        ratio = policy.get("keep", .15)
        keep = min(limit, len(history) - 2, max(2, int(len(history) * ratio) // 2 * 2)) if ratio else 0
        cutoff = len(history) - keep
        source = trim_messages(history[:cutoff], max_tokens=12000, token_counter=count_tokens_cjk,
                               strategy="last", start_on="human")
        try:
            result = await model_client(summary, validate_url, audit=audit).ainvoke([
                SystemMessage(content=policy.get("prompt") or workflow("summary")),
                HumanMessage(content="\n".join(f"{item.type}: {item.content}" for item in source)),
            ])
            if result.text.strip():
                if trace := task_trace():
                    trace.event("context", "history_summary", {"summarized_messages": len(source), "kept_messages": keep})
                return [HumanMessage(content="历史对话摘要（供参考）：\n" + result.text), *history[cutoff:]]
        except Exception:
            import logging
            logging.getLogger(__name__).warning("Context summary failed; using truncation")
    if trace := task_trace():
        trace.event("context", "history_trim", {"messages_before": len(history), "messages_after": len(history[discard:])})
    return history[discard:]
