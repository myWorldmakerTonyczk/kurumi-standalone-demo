"""Prompts used by platform agent workflows."""
import json
import re
from pathlib import Path

_PROMPTS = json.loads((Path(__file__).resolve().parents[2] / "config" / "workflow.json").read_text(encoding="utf-8"))


def workflow(name):
    return _PROMPTS[name]


def persona_body(text):
    """Remove the built-in name from the start of older saved personas."""
    text = text.strip()
    text = re.sub(r"^你是(?:福贺)?久留美[，,]\s*", "你是", text, count=1)
    return re.sub(r"^你是(?:福贺)?久留美(?:[。.]|\n|$)\s*", "", text, count=1)


def agent_real_name(profile):
    return profile.get("agent_real_name") or profile.get("agent_name") or "久留美"


def character_persona(profile, default):
    name = agent_real_name(profile)
    nickname = profile.get("agent_name") or "久留美"
    body = persona_body(profile.get("persona") or "") or default
    return (f"我是{name}。这是你的真实姓名。你的昵称是「{nickname}」，是你在群聊、论坛等平台界面使用的显示名。"
            "别人用这个昵称称呼或 @ 你时，指的也是你。\n\n" + body)


def social_system_prompt(persona):
    return workflow("social_system_prefix") + "\n\n" + persona
