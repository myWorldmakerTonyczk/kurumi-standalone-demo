"""Optional speech synthesis using an account-owned model connection."""
import base64
import logging
import random
import re

from langchain_core.messages import HumanMessage, SystemMessage
from openai import AsyncOpenAI

from .workflow import workflow
from .model_registry import model_client, selected

log = logging.getLogger(__name__)


async def voice_reply(profile, history, question, reply, validate_url, *, audit_factory=None):
    policy = profile.get("model_policy", {})
    config = selected(profile, "speech", default=False)
    if not policy.get("voice_enabled") or not config.get("key") or random.random() >= policy.get("voice_probability", .2):
        return None
    text = re.sub(r"```[\s\S]*?```|https?://\S+|(?m:^\s*\|.*$)", "", reply)
    text = re.sub(r"[*#_`]", "", text).strip()[:800]
    if not text:
        return None
    instruction = ""
    emotion = selected(profile, "emotion", default=False)
    if emotion.get("key"):
        try:
            context = "\n".join(str(message.content)[:400] for message in history[-5:])
            audit = audit_factory("voice_instruction") if audit_factory else None
            result = await model_client(emotion, validate_url, audit=audit).ainvoke([
                SystemMessage(content=workflow("voice")),
                HumanMessage(content=f"人格：{profile.get('persona', '')[:1200]}\n近期对话：{context}\n问题：{question[:500]}\n回复：{text}"),
            ])
            instruction = " ".join(result.text.split())[:120]
        except Exception:
            log.warning("Voice instruction generation failed")
    try:
        async with AsyncOpenAI(api_key=config["key"], base_url=validate_url(config["url"]), timeout=30,
                               max_retries=0) as client:
            params = dict(model=config["model"], voice=policy.get("voice") or "alloy", input=text,
                          response_format="mp3")
            audit = audit_factory("speech") if audit_factory else None
            submit = client.audio.speech.create

            async def synthesize(arguments):
                return await audit.record_speech(submit, arguments) if audit else await submit(**arguments)

            if instruction:
                try:
                    response = await synthesize(params | {"instructions": instruction})
                except (TypeError, ValueError):
                    response = await synthesize(params)
            else:
                response = await synthesize(params)
        if len(response.content) <= 2_000_000:
            return {"kind": "audio", "src": "data:audio/mpeg;base64," + base64.b64encode(response.content).decode("ascii")}
    except Exception:
        log.warning("Speech synthesis failed")
    return None
