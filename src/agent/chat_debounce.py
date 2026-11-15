"""Per-conversation batching for incoming chat messages."""
import asyncio
import math
from dataclasses import dataclass, field

DEFAULTS = {"enabled": True, "private_quiet": 1.2, "private_max": 4,
            "group_quiet": 2, "group_max": 5}


def debounce_config(values):
    config = {key: values.get(key, default) for key, default in DEFAULTS.items()}
    if type(config["enabled"]) is not bool:
        raise ValueError("请选择是否开启消息防抖")
    for key in ("private_quiet", "private_max", "group_quiet", "group_max"):
        value = config[key]
        if type(value) not in (int, float) or not math.isfinite(value) or not 0.1 <= value <= 15:
            raise ValueError("防抖时间须在 0.1～15 秒之间")
    for channel in ("private", "group"):
        if config[channel + "_max"] < config[channel + "_quiet"]:
            raise ValueError("最长等待须大于或等于静默时间")
    return config


@dataclass
class Conversation:
    pending: list = field(default_factory=list)
    changed: asyncio.Event = field(default_factory=asyncio.Event)
    first: float = 0
    last: float = 0


class ChatDebounce:
    def __init__(self):
        self.conversations = {}
        self.tasks = set()

    def enqueue(self, key, item, quiet, maximum, process):
        loop = asyncio.get_running_loop()
        conversation = self.conversations.get(key)
        if conversation is None:
            conversation = self.conversations[key] = Conversation()
            task = loop.create_task(self.drain(key, conversation, process))
            self.tasks.add(task)
            task.add_done_callback(self.tasks.discard)
        now = loop.time()
        if not conversation.pending:
            conversation.first = now
        conversation.last = now
        conversation.pending.append((item, quiet, maximum))
        conversation.changed.set()

    async def drain(self, key, conversation, process):
        loop = asyncio.get_running_loop()
        try:
            while conversation.pending:
                _, quiet, maximum = conversation.pending[0]
                remaining = min(conversation.last + quiet, conversation.first + maximum) - loop.time()
                if remaining > 0:
                    conversation.changed.clear()
                    try:
                        await asyncio.wait_for(conversation.changed.wait(), remaining)
                    except TimeoutError:
                        pass
                    continue
                count = len(conversation.pending) if quiet else 1
                items = [item for item, _, _ in conversation.pending[:count]]
                del conversation.pending[:count]
                await process(key, items)
        finally:
            self.conversations.pop(key, None)

    async def close(self):
        tasks = list(self.tasks)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
