"""Prompts used by platform agent workflows."""
import json
from pathlib import Path

_PROMPTS = json.loads((Path(__file__).resolve().parents[2] / "config" / "workflow.json").read_text(encoding="utf-8"))


def workflow(name):
    return _PROMPTS[name]
