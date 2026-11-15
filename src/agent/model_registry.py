"""Account-owned model connections and feature assignments."""
from urllib.parse import urlsplit
from uuid import uuid4

from openai import APIConnectionError, APIStatusError


def connections(profile):
    if "models" in profile:
        return profile["models"]
    if profile.get("model"):
        return [{"id": "legacy", "label": profile["model"],
                 **{field: profile.get(field, "") for field in ("url", "model", "key")}}]
    return []


def supports_vision(model):
    return model.get("kind") == "vision" or (
        model.get("kind", "chat") == "chat"
        and urlsplit(model.get("url") or "").hostname == "api.deepseek.com"
        and model["model"].lower() in {"deepseek-flash", "deepseek-v4-flash", "deepseek-v4-flash-vision-exp"}
    )


def selected(profile, role="chat", *, default=True):
    if role == "vision":
        if not profile.get("model_policy", {}).get("vision_enabled", True):
            return {}
        choices = [item for item in connections(profile) if item.get("key") and supports_vision(item)]
        model_id = profile.get("model_roles", {}).get("vision")
        return next((item for item in choices if item["id"] == model_id), choices[0] if default and choices else {})
    kind = "speech" if role == "speech" else "chat"
    models = [item for item in connections(profile) if item.get("kind", "chat") == kind]
    model_id = profile.get("model_roles", {}).get(role)
    return next((model for model in models if model["id"] == model_id), models[0] if default and models else {})


def public_models(profile):
    return [{field: model[field] for field in ("id", "label", "url", "model")}
            | {"has_key": bool(model["key"]), "kind": model.get("kind", "chat"), "vision": supports_vision(model)} for model in connections(profile)]


def update(profile, body, validate_url):
    models = connections(profile).copy()
    roles = profile.get("model_roles", {}).copy()
    operation = body.get("action")
    model_id = body.get("id")
    existing = next((entry for entry in models if entry["id"] == model_id), None)
    if operation == "save":
        if model_id and not existing:
            raise ValueError("模型不存在")
        label, url, model, key = (str(body.get(field) or "").strip()
                                  for field in ("label", "url", "model", "key"))
        url = validate_url(url)
        if not model or len(model) > 200 or len(label) > 80 or len(key) > 4000:
            raise ValueError("请检查模型名称和密钥长度")
        old_key = existing["key"] if existing else ""
        old_url = existing["url"] or "https://api.openai.com/v1" if existing else ""
        if existing and url != old_url and old_key and not key:
            raise ValueError("更换接口地址时请填写新的 API Key")
        kind = body.get("kind", "chat")
        if kind not in ("chat", "speech", "vision"):
            raise ValueError("模型类型无效")
        if existing and existing.get("kind", "chat") != kind:
            roles = {role: value for role, value in roles.items() if value != model_id}
        entry = dict(kind=kind, id=model_id or uuid4().hex, label=label or model, url=url, model=model, key=key or old_key)
        if any(item["id"] != model_id and item["model"] == model
               and (item["url"] or "https://api.openai.com/v1") == url for item in models):
            raise ValueError("该接口下的模型已存在，请直接选用")
        if existing:
            models[models.index(existing)] = entry
        else:
            models.append(entry)
    elif operation in {"clear", "delete"}:
        if not existing:
            raise ValueError("模型不存在")
        if operation == "clear":
            models[models.index(existing)] = existing | {"key": ""}
        else:
            models.remove(existing)
            roles = {role: value for role, value in roles.items() if value != model_id}
    elif operation == "roles":
        for role in ("chat", "fallback", "summary", "speech", "emotion", "vision"):
            value = body.get(role, roles.get(role, "") if role == "vision" else "")
            kind = "speech" if role == "speech" else "chat"
            matches = [item for item in models if item["id"] == value]
            if value and not any(supports_vision(item) if role == "vision" else item.get("kind", "chat") == kind
                                 for item in matches):
                raise ValueError("请选择已配置的模型")
            roles[role] = value
        try:
            retries = int(body.get("retries", 0))
            rounds = int(body.get("rounds", 10))
            discard = int(body.get("discard", 1))
            keep = float(body.get("keep", 0.15))
            voice_probability = float(body.get("voice_probability", .2))
        except (ValueError, TypeError, OverflowError):
            raise ValueError("请检查重试次数和上下文参数") from None
        strategy = body.get("strategy", "trim")
        if not (0 <= retries <= 5 and (rounds == -1 or 1 <= rounds <= 100)
                and 1 <= discard <= 100 and 0 <= keep <= .3 and 0 <= voice_probability <= 1 and strategy in {"trim", "summary"}):
            raise ValueError("重试次数或上下文参数超出范围")
        profile = profile | {"model_policy": dict(retries=retries, rounds=rounds, discard=discard,
                              keep=keep, strategy=strategy, voice_probability=voice_probability,
                              voice_enabled=body.get("voice_enabled") is True,
                              vision_enabled=body.get("vision_enabled", profile.get("model_policy", {}).get("vision_enabled", True)) is True,
                              voice=str(body.get("voice") or "alloy").strip()[:80], prompt=str(body.get("prompt") or "")[:4000])}
    else:
        raise ValueError("未知模型操作")
    return {field: value for field, value in profile.items() if field not in {"url", "model", "key"}} | {
        "models": models, "model_roles": roles,
    }


def model_client(config, validate_url, *, thinking=False, audit=None):
    from langchain_openai import ChatOpenAI
    from yarl import URL
    from .deepseek_chat import DeepSeekChat

    endpoint = validate_url(config["url"])
    cls = ChatOpenAI
    options = {}
    if thinking and URL(endpoint).host == "api.deepseek.com":
        cls = DeepSeekChat
        options = dict(use_responses_api=False, disable_streaming=True,
                       extra_body={"thinking": {"type": "enabled"}})
    return cls(model=config["model"], api_key=config["key"], base_url=endpoint,
               timeout=45, max_retries=0, callbacks=[audit] if audit else None, **options)


def retryable(error):
    return isinstance(error, APIConnectionError) or (
        isinstance(error, APIStatusError) and (error.status_code in {408, 409, 429} or error.status_code >= 500)
    )
