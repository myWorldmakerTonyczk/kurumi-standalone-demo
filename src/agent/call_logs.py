"""Opt-in, account-scoped model call logs."""
import json
import math
import re
import time

from langchain_core.callbacks import AsyncCallbackHandler

from ..diary.store import redact
from .context_tokens import estimate_text_tokens

RETAIN = 150
CONTENT_LIMIT = 40_000
INLINE_MEDIA = re.compile(r"data:(?:image|audio)/[^;, ]+;base64,[A-Za-z0-9+/=]+")
CREDENTIAL = re.compile(r"(?i)(?:Bearer\s+[A-Za-z0-9_.~+/=-]+|sk-[A-Za-z0-9_-]{12,})")


def log_secrets(profile):
    from .model_registry import connections

    return sorted({item["key"] for item in connections(profile) if item.get("key")}, key=len, reverse=True)


def clean_log(value, secrets, *, limit=CONTENT_LIMIT, json_payload=False):
    def clean_text(text):
        for secret in secrets:
            text = text.replace(secret, "[密钥已隐藏]")
        return redact(INLINE_MEDIA.sub("[媒体数据已省略]", CREDENTIAL.sub("[凭据已隐藏]", text)))

    def clean_value(item):
        if isinstance(item, dict):
            return {str(key): "[凭据已隐藏]" if str(key).lower() in {"api_key", "authorization", "password"}
                    else clean_value(value) for key, value in item.items()}
        if isinstance(item, (tuple, list)):
            return [clean_value(value) for value in item]
        if isinstance(item, str):
            return clean_text(item)
        if item is None or isinstance(item, (bool, int, float)):
            return item
        return clean_text(str(item))

    sanitized = clean_value(value)
    text = sanitized if isinstance(sanitized, str) and not json_payload else json.dumps(
        sanitized, ensure_ascii=False, indent=None if json_payload else 2)
    return text if limit is None or len(text) <= limit else text[:limit] + "\n[记录已截断]"


def error_label(error):
    status = getattr(error, "status_code", None)
    return type(error).__name__ + (f" (HTTP {status})" if isinstance(status, int) else "")


def message_usage(messages):
    return next((m["usage_metadata"] for m in messages if isinstance(m, dict) and m.get("usage_metadata")), None)


def estimate_payload(payload):
    text = json.dumps(payload, ensure_ascii=False, separators=(",", ":"), default=str)
    return math.ceil(estimate_text_tokens(INLINE_MEDIA.sub("[media omitted]", text)))


def call_usage(request, response, usage=None):
    if usage and any(type(usage.get(key)) is int for key in ("input_tokens", "output_tokens", "total_tokens")):
        result = dict(usage, source="provider")
        if result.get("total_tokens") is None and all(type(result.get(key)) is int for key in ("input_tokens", "output_tokens")):
            result["total_tokens"] = result["input_tokens"] + result["output_tokens"]
        return result
    input_tokens = estimate_payload(request)
    output_tokens = estimate_payload(response) if response is not None else None
    return {"source": "estimate", "input_tokens": input_tokens, "output_tokens": output_tokens,
            "total_tokens": input_tokens + output_tokens if output_tokens is not None else None}


class CallLogs:
    def __init__(self, db, profile):
        self.db = db
        self.profile = profile
        db.executescript("""
            CREATE TABLE IF NOT EXISTS model_call_log_settings(
              owner TEXT PRIMARY KEY REFERENCES sim_users(id) ON DELETE CASCADE, enabled INTEGER NOT NULL);
            CREATE TABLE IF NOT EXISTS model_call_logs(
              id INTEGER PRIMARY KEY, owner TEXT NOT NULL REFERENCES sim_users(id) ON DELETE CASCADE,
              task TEXT NOT NULL, model TEXT NOT NULL, started REAL NOT NULL, elapsed_ms INTEGER NOT NULL,
              status TEXT NOT NULL, request TEXT NOT NULL, response TEXT NOT NULL, error TEXT NOT NULL);
            CREATE INDEX IF NOT EXISTS model_call_logs_owner ON model_call_logs(owner,id DESC);
        """)
        if not any(row[1] == "usage" for row in db.execute("PRAGMA table_info(model_call_logs)")):
            db.execute("ALTER TABLE model_call_logs ADD COLUMN usage TEXT NOT NULL DEFAULT ''")

    def enabled(self, owner):
        row = self.db.execute("SELECT enabled FROM model_call_log_settings WHERE owner=?", (owner,)).fetchone()
        return bool(row and row[0])

    def set_enabled(self, owner, enabled):
        self.db.execute("INSERT OR REPLACE INTO model_call_log_settings VALUES (?,?)", (owner, enabled))

    def clear(self, owner):
        self.db.execute("DELETE FROM model_call_logs WHERE owner=?", (owner,))

    def entries(self, owner, task=""):
        query = """SELECT id,task,model,started,elapsed_ms,status,error,usage,
                          CASE WHEN usage='' THEN response ELSE '' END AS legacy_response FROM model_call_logs
                   WHERE owner=? AND (?='' OR task=?) ORDER BY id DESC LIMIT ?"""
        return [self.with_usage(dict(row)) for row in self.db.execute(query, (owner, task, task, RETAIN))]

    @staticmethod
    def with_usage(entry):
        legacy = entry.pop("legacy_response", entry.get("response", ""))
        if entry["usage"]:
            entry["usage"] = json.loads(entry["usage"])
        else:
            try:
                messages = json.loads(legacy)
            except (TypeError, ValueError):
                messages = None
            usage = message_usage(messages) if isinstance(messages, list) else None
            entry["usage"] = call_usage(None, None, usage) if usage else None
        return entry

    def detail(self, owner, entry_id):
        row = self.db.execute("""SELECT id,task,model,started,elapsed_ms,status,request,response,error,usage
                              FROM model_call_logs WHERE owner=? AND id=?""", (owner, entry_id)).fetchone()
        return self.with_usage(dict(row)) if row else None

    def callback(self, owner, task):
        from .timeline import task_trace

        trace = task_trace(owner)
        return CallTrace(self, owner, task, trace) if trace or self.enabled(owner) else None

    def insert(self, owner, task, model, started, request, response="", error="", *, usage=None):
        self.db.execute("""INSERT INTO model_call_logs(owner,task,model,started,elapsed_ms,status,request,response,error,usage)
                          VALUES(?,?,?,?,?,?,?,?,?,?)""",
                        (owner, task, model, started, max(0, int((time.time() - started) * 1000)),
                         "failed" if error else "ok", request, response, error, json.dumps(usage) if usage else ""))
        self.db.execute("""DELETE FROM model_call_logs WHERE owner=? AND id IN
                           (SELECT id FROM model_call_logs WHERE owner=? ORDER BY id DESC LIMIT -1 OFFSET ?)""",
                        (owner, owner, RETAIN))


class CallTrace(AsyncCallbackHandler):
    run_inline = True
    error_label = staticmethod(error_label)

    def __init__(self, logs, owner, task, trace=None):
        self.logs, self.owner, self.task, self.trace = logs, owner, task, trace
        self.pending = {}
        self.secrets = trace.secrets if trace else log_secrets(logs.profile(owner))

    def clean(self, value):
        return clean_log(value, self.secrets)

    async def on_chat_model_start(self, serialized, messages, *, run_id, **kwargs):
        if not self.trace and not self.logs.enabled(self.owner):
            return
        params = kwargs.get("invocation_params") or {}
        options = kwargs.get("options") or {}
        tools = options.get("tools") or params.get("tools") or []
        batches = [[self.message(message) for message in batch] for batch in messages]
        payload = {"messages": batches[0] if len(batches) == 1 else batches}
        if tools:
            payload["tools"] = tools
        started = time.time()
        model = str(params.get("model") or params.get("model_name") or "")
        event = self.trace.start("model", self.task, {"model": model, "messages": payload["messages"], "tools": tools}) if self.trace else None
        self.pending[run_id] = (started, model, payload, event)

    @staticmethod
    def message(message):
        result = {"role": message.type, "content": message.content}
        for field in ("tool_calls", "tool_call_id", "usage_metadata"):
            value = getattr(message, field, None)
            if value:
                result[field] = value
        for field in ("reasoning_content", "reasoning", "reasoning_summary"):
            if value := message.additional_kwargs.get(field):
                result[field] = value
        return result

    async def record_speech(self, submit, params):
        started = time.time()
        try:
            response = await submit(**params)
        except Exception as error:
            if self.logs.enabled(self.owner):
                self.logs.insert(self.owner, self.task, self.clean(params["model"]), started,
                                 self.clean(params), error=self.error_label(error))
            raise
        if self.logs.enabled(self.owner):
            self.logs.insert(self.owner, self.task, self.clean(params["model"]), started,
                             self.clean(params), self.clean({"audio_bytes": len(response.content)}))
        return response

    async def on_llm_end(self, response, *, run_id, **kwargs):
        messages = [self.message(generation.message) if hasattr(generation, "message") else generation.text
                    for batch in response.generations for generation in batch]
        usage = message_usage(messages)
        if usage is None:
            raw = (response.llm_output or {}).get("token_usage") or {}
            if raw:
                usage = {"input_tokens": raw.get("prompt_tokens"), "output_tokens": raw.get("completion_tokens"),
                         "total_tokens": raw.get("total_tokens"), "provider_usage": raw}
        self.finish(run_id, messages, usage=usage)

    async def on_llm_error(self, error, *, run_id, **kwargs):
        self.finish(run_id, error=self.error_label(error), message=str(error))

    def finish(self, run_id, response=None, *, usage=None, error="", message=""):
        start = self.pending.pop(run_id, None)
        if start is None:
            return
        started, model, request, event = start
        usage = call_usage(request, response, usage)
        if self.trace:
            self.trace.finish(event, {"messages": response, "usage": usage, "error": error, "message": message},
                              "failed" if error else "ok")
        if self.logs.enabled(self.owner):
            self.logs.insert(self.owner, self.task, self.clean(model), started, self.clean(request),
                             self.clean(response) if response is not None else "", error, usage=usage)
