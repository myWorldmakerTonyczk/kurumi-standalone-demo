"""Describe a new chat image using the account's configured vision model."""
import json
import logging

from langchain_core.messages import HumanMessage, SystemMessage

from .model_registry import model_client, selected
from .workflow import workflow

log = logging.getLogger(__name__)


async def describe_image(profile, image, question, validate_url, *, audit=None, catalog=False):
    config = selected(profile, "vision")
    if not config:
        if catalog:
            raise ValueError("请先在「模型与 API」启用并配置识图模型")
        return "本轮有一张图片，但识图已关闭或尚未配置可用的识图模型。"
    try:
        result = await model_client(config, validate_url, audit=audit).ainvoke([
            SystemMessage(content=workflow("vision") + (workflow("image_catalog") if catalog else "")),
            HumanMessage(content=[
                {"type": "text", "text": question or "请描述用户发送的图片。"},
                {"type": "image_url", "image_url": {"url": image}},
            ]),
        ])
        description = result.text.strip()
        if catalog:
            if description.startswith("```") and description.endswith("```"):
                description = description.split("\n", 1)[-1][:-3].strip()
            data = json.loads(description)
            if (not isinstance(data, dict)
                    or any(not isinstance(data.get(key), str) or not data[key].strip()
                           for key in ("name", "category", "description"))
                    or not isinstance(data.get("tags"), list)
                    or not data["tags"] or any(not isinstance(tag, str) or not tag.strip() for tag in data["tags"])):
                raise ValueError("识图结果缺少图库元数据")
            return {"name": data["name"].strip()[:80], "category": data["category"].strip()[:40],
                    "description": data["description"].strip()[:1800], "tags": data["tags"][:20]}
        if description:
            return description[:1800]
    except Exception as error:
        log.warning("Image recognition failed (%s)", type(error).__name__)
    if catalog:
        raise ValueError("图库识图失败，请检查识图模型后重试")
    return "本轮有一张图片，但识图失败，无法确认画面内容。"
