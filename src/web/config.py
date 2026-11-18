"""Shared web-layer configuration values."""
import os
from pathlib import Path

PERSONA_MAX_LENGTH = 10_000


def listen_port(admin):
    name, default = ("KURUMI_ADMIN_PORT", 8790) if admin else ("KURUMI_USER_PORT", 8791)
    port = int(os.getenv(name, str(default)))
    if not 1 <= port <= 65535:
        raise ValueError(f"{name} must be between 1 and 65535")
    return port


ROOT = Path(__file__).resolve().parents[2]
DEFAULT_PERSONA = (ROOT / "config" / "default_persona.md").read_text(encoding="utf-8").strip()
MODEL_PRESETS = [
    dict(name="DeepSeek", url="https://api.deepseek.com", model="deepseek-flash"),
    dict(name="OpenAI", url="https://api.openai.com/v1", model="gpt-4o-mini"),
    dict(name="Gemini", url="https://generativelanguage.googleapis.com/v1beta/openai/", model="gemini-3.8-flash"),
]
BULLETIN_MODEL_PRESETS = [
    MODEL_PRESETS[0] | {"name": "DeepSeek Flash"},
    MODEL_PRESETS[0] | {"name": "DeepSeek V4 Pro", "model": "deepseek-v4-pro"},
    MODEL_PRESETS[1] | {"name": "OpenAI GPT-5 mini", "model": "gpt-5-mini"},
    MODEL_PRESETS[2] | {"name": "Gemini 3.8 Flash"},
]
