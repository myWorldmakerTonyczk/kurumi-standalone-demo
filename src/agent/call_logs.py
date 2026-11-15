"""Opt-in, account-scoped model call logs."""
import json
import re
import time

from langchain_core.callbacks import AsyncCallbackHandler

RETAIN = 150
CONTENT_LIMIT = 40_000
INLINE_MEDIA = re.compile(r"data:(?:image|audio)/[^;, ]+;base64,[A-Za-z0-9+/=]+")
CREDENTIAL = re.compile(r"(?i)(?:Bearer\s+[A-Za-z0-9_.~+/=-]+|sk-[A-Za-z0-9_-]{12,})")


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

    def enabled(self, owner):
        row = self.db.execute("SELECT enabled FROM model_call_log_settings WHERE owner=?", (owner,)).fetchone()
        return bool(row and row[0])

    def set_enabled(self, owner, enabled):
        self.db.execute("INSERT OR REPLACE INTO model_call_log_settings VALUES (?,?)", (owner, enabled))

    def clear(self, owner):
        self.db.execute("DELETE FROM model_call_logs WHERE owner=?", (owner,))

    def entries(self, owner, task=""):
        query = """SELECT id,task,model,started,elapsed_ms,status,error FROM model_call_logs
                   WHERE owner=? AND (?='' OR task=?) ORDER BY id DESC LIMIT ?"""
        return [dict(row) for row in self.db.execute(query, (owner, task, task, RETAIN))]

    def detail(self, owner, entry_id):
        row = self.db.execute("""SELECT id,task,model,started,elapsed_ms,status,request,response,error
                              FROM model_call_logs WHERE owner=? AND id=?""", (owner, entry_id)).fetchone()
        return dict(row) if row else None

    def callback(self, owner, task):
        return CallTrace(self, owner, task) if self.enabled(owner) else None

    def insert(self, owner, task, model, started, request, response="", error=""):
        self.db.execute("""INSERT INTO model_call_logs(owner,task,model,started,elapsed_ms,status,request,response,error)
                          VALUES(?,?,?,?,?,?,?,?,?)""",
                        (owner, task, model, started, max(0, int((time.time() - started) * 1000)),
                         "failed" if error else "ok", request, response, error))
        self.db.execute("""DELETE FROM model_call_logs WHERE owner=? AND id IN
                           (SELECT id FROM model_call_logs WHERE owner=? ORDER BY id DESC LIMIT -1 OFFSET ?)""",
                        (owner, owner, RETAIN))


class CallTrace(AsyncCallbackHandler):
    def __init__(self, logs, owner, task):
        from .model_registry import connections

        self.logs, self.owner, self.task = logs, owner, task
        self.pending = {}
        self.secrets = sorted({item["key"] for item in connections(logs.profile(owner)) if item.get("key")},
                              key=len, reverse=True)

    def clean(self, value):
        text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, indent=2, default=str)
        for secret in self.secrets:
            text = text.replace(secret, "[密钥已隐藏]")
        text = INLINE_MEDIA.sub("[媒体数据已省略]", CREDENTIAL.sub("[凭据已隐藏]", text))
        return text if len(text) <= CONTENT_LIMIT else text[:CONTENT_LIMIT] + "\n[记录已截断]"

    async def on_chat_model_start(self, serialized, messages, *, run_id, **kwargs):
        if not self.logs.enabled(self.owner):
            return
        params = kwargs.get("invocation_params") or {}
        options = kwargs.get("options") or {}
        tools = options.get("tools") or params.get("tools")
        batches = [[self.message(message) for message in batch] for batch in messages]
        payload = {"messages": batches[0] if len(batches) == 1 else batches}
        if tools:
            payload["tools"] = tools
        self.pending[run_id] = (time.time(), str(params.get("model") or params.get("model_name") or ""),
                                self.clean(payload))

    @staticmethod
    def message(message):
        result = {"role": message.type, "content": message.content}
        for field in ("tool_calls", "tool_call_id", "usage_metadata"):
            value = getattr(message, field, None)
            if value:
                result[field] = value
        reasoning = message.additional_kwargs.get("reasoning_content")
        if reasoning:
            result["reasoning_content"] = reasoning
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
        self.finish(run_id, self.clean(messages))

    async def on_llm_error(self, error, *, run_id, **kwargs):
        self.finish(run_id, error=self.error_label(error))

    @staticmethod
    def error_label(error):
        status = getattr(error, "status_code", None)
        return type(error).__name__ + (f" (HTTP {status})" if isinstance(status, int) else "")

    def finish(self, run_id, response="", error=""):
        start = self.pending.pop(run_id, None)
        if start is not None and self.logs.enabled(self.owner):
            started, model, request = start
            self.logs.insert(self.owner, self.task, self.clean(model), started, request, response, error)
