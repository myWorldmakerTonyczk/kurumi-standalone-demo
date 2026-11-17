"""Evidence-backed portraits, scoped to the conversation that supplied them."""
import json

from .timeline import task_trace
from datetime import datetime
from zoneinfo import ZoneInfo

from langchain_core.messages import HumanMessage, SystemMessage

from .model_registry import model_client
from .workflow import workflow

ZONE = ZoneInfo("Asia/Shanghai")
DIMENSIONS = {"preference", "habit", "requirement", "relationship", "project", "attribute"}
EVIDENCE_LIMIT = 400

# Only events already eligible for this agent's ordinary memory can supply evidence.
SOURCES = {
    "private": """SELECT u.content AS text,u.created AS occurred FROM demo_chat a
        JOIN demo_chat u ON u.id=(SELECT max(id) FROM demo_chat WHERE game=a.game AND id<a.id)
        JOIN games g ON g.id=a.game
        WHERE a.id=? AND a.game=? AND json_extract(g.data,'$.owner_id')=?
        AND a.role='assistant' AND u.role='user'""",
    "group": """SELECT m.body AS text,m.created AS occurred FROM community_group_messages m
        JOIN community_group_members member ON member.room=m.room AND member.owner=?
        WHERE m.room=? AND m.id=? AND m.status='active'""",
    "forum_post": """SELECT body AS text,created AS occurred FROM community_posts
        WHERE id=? AND status='active'""",
    "forum": """SELECT m.body AS text,m.created AS occurred FROM community_messages m
        JOIN community_posts p ON p.id=m.post
        WHERE m.id=? AND m.post=? AND m.status='active' AND p.status='active'""",
}


class MemoryProfiles:
    def __init__(self, db, call_logs=None):
        self.db = db
        self.call_logs = call_logs
        db.execute("""CREATE TABLE IF NOT EXISTS agent_memory_profiles(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            owner TEXT NOT NULL REFERENCES sim_users(id) ON DELETE CASCADE,
            stream TEXT NOT NULL, subject TEXT NOT NULL, dimension TEXT NOT NULL,
            text TEXT NOT NULL, basis TEXT NOT NULL, evidence TEXT NOT NULL,
            updated REAL NOT NULL DEFAULT (unixepoch()),
            UNIQUE(owner,stream,subject,dimension,text))""")
        db.execute("""CREATE TABLE IF NOT EXISTS agent_memory_profile_progress(
            owner TEXT NOT NULL REFERENCES sim_users(id) ON DELETE CASCADE,
            stream TEXT NOT NULL, last_seq INTEGER NOT NULL, PRIMARY KEY(owner,stream))""")

    def pending(self, owner, stream):
        return self.db.execute("""SELECT coalesce(max(seq),0) FROM agent_memory_events
            WHERE owner=? AND stream=? AND done=1 AND seq>coalesce((
            SELECT last_seq FROM agent_memory_profile_progress WHERE owner=? AND stream=?),0)""",
            (owner, stream, owner, stream)).fetchone()[0]

    @staticmethod
    def unpack(row):
        return dict(row) | {"evidence": json.loads(row["evidence"])}

    def list(self, owner, stream=None):
        rows = self.db.execute("""SELECT id,stream,subject,dimension,text,basis,evidence,updated
            FROM agent_memory_profiles WHERE owner=? AND (? IS NULL OR stream=?)
            ORDER BY updated DESC,id DESC LIMIT 200""", (owner, stream, stream))
        return [self.unpack(row) for row in rows]

    def context(self, owner, stream, subjects):
        result = []
        for subject in dict.fromkeys(subjects):
            rows = self.db.execute("""SELECT dimension,text,basis FROM agent_memory_profiles
                WHERE owner=? AND stream=? AND subject=?
                ORDER BY (basis='declared') DESC,updated DESC,id DESC LIMIT 3""", (owner, stream, subject))
            result.extend(dict(row) | {"subject": subject} for row in rows)
            if len(result) >= 6:
                break
        return result[:6]

    def sources(self, owner, stream, through):
        kind, _, target = stream.partition(":")
        if kind not in SOURCES:
            return {}
        result = {}
        events = self.db.execute("""SELECT seq,event,subject FROM agent_memory_events
            WHERE owner=? AND stream=? AND done=1 AND seq<=? ORDER BY seq DESC LIMIT ?""",
            (owner, stream, through, EVIDENCE_LIMIT)).fetchall()
        for event in reversed(events):
            if event["subject"].startswith("agent:"):
                continue
            ref = event["event"]
            source_kind = kind
            if kind == "forum":
                if ref == "post":
                    source_kind, ref = "forum_post", target
                elif ref.startswith("comment:"):
                    ref = ref.removeprefix("comment:")
                else:
                    continue
            if not ref.isascii() or not ref.isdecimal():
                continue
            args = {"private": (ref, target, owner), "group": (owner, target, ref),
                    "forum": (ref, target), "forum_post": (ref,)}[source_kind]
            row = self.db.execute(SOURCES[source_kind], args).fetchone()
            if not row or not row["occurred"] or not row["text"].strip():
                continue
            day = datetime.fromtimestamp(row["occurred"], ZONE).date().isoformat()
            result[event["seq"]] = {"event": event["seq"], "reference": event["event"],
                "subject": event["subject"], "date": day, "text": row["text"][:450]}
        # Keep the latest exchange and give older dates a share of the input budget.
        recent = list(result)[-24:]
        selected = set(recent)
        per_day = {}
        for key, item in reversed(list(result.items())):
            if len(selected) >= 72:
                break
            bucket = (item["subject"], item["date"])
            if key not in selected and per_day.get(bucket, 0) < 4:
                per_day[bucket] = per_day.get(bucket, 0) + 1
                selected.add(key)
        return {key: item for key, item in result.items() if key in selected}

    async def consolidate(self, owner, stream, model, validate_url):
        through = self.pending(owner, stream)
        if not through:
            return
        sources = self.sources(owner, stream, through)
        existing = {item["id"]: item for item in self.list(owner, stream)}
        actions = []
        if sources:
            audit = self.call_logs.callback(owner, "memory_profile") if self.call_logs else None
            response = await model_client(model, validate_url, audit=audit).ainvoke([
                SystemMessage(content=workflow("memory_profile")),
                HumanMessage(content=json.dumps({"source": stream.split(":", 1)[0],
                    "events": list(sources.values()), "profiles": list(existing.values())}, ensure_ascii=False)),
            ])
            raw = response.text.strip()
            if raw.startswith("```") and raw.endswith("```"):
                raw = raw.partition("\n")[2][:-3]
            actions = json.loads(raw)
            if not isinstance(actions, list):
                raise ValueError("Expected memory profile array")
        trace = task_trace(owner)
        saved = []
        self.db.execute("SAVEPOINT consolidate_profiles")
        try:
            for action in actions[:16]:
                applied = self.apply(owner, stream, action, sources, existing)
                if trace and applied:
                    saved.append(applied)
            self.db.execute("""INSERT INTO agent_memory_profile_progress VALUES(?,?,?)
                ON CONFLICT(owner,stream) DO UPDATE SET last_seq=max(last_seq,excluded.last_seq)""",
                (owner, stream, through))
        except BaseException:
            self.db.execute("ROLLBACK TO consolidate_profiles")
            raise
        finally:
            self.db.execute("RELEASE consolidate_profiles")
        if trace:
            trace.event("memory_write", "portrait", {"candidates": len(actions), "processed_events": len(sources), "saved": saved})

    def apply(self, owner, stream, action, sources, existing):
        if not isinstance(action, dict):
            return
        dimension, text, basis = (action.get(k) for k in ("dimension", "text", "basis"))
        refs, targets = action.get("events"), action.get("replace", [])
        if (not isinstance(dimension, str) or dimension not in DIMENSIONS
                or not isinstance(text, str) or not 1 <= len(text.strip()) <= 240
                or basis not in ("declared", "observed") or not isinstance(refs, list)
                or not 1 <= len(refs) <= 8 or not isinstance(targets, list) or len(targets) > 8
                or any(type(ref) is not int or ref not in sources for ref in refs)
                or any(type(target) is not int or target not in existing for target in targets)):
            return
        proof = [sources[ref] for ref in sorted(set(refs))]
        subject = proof[0]["subject"]
        if any(item["subject"] != subject for item in proof):
            return
        if basis == "observed" and len({item["date"] for item in proof}) < 3:
            return
        if any(existing[i]["subject"] != subject or existing[i]["dimension"] != dimension for i in targets):
            return
        # Re-reading the same exact evidence does not refresh a portrait.
        evidence = json.dumps([{k: item[k] for k in ("event", "reference", "date")} for item in proof])
        cursor = self.db.execute("""INSERT INTO agent_memory_profiles(owner,stream,subject,dimension,text,basis,evidence)
            VALUES(?,?,?,?,?,?,?) ON CONFLICT(owner,stream,subject,dimension,text) DO UPDATE SET
            evidence=excluded.evidence,basis=excluded.basis,updated=unixepoch()
            WHERE agent_memory_profiles.evidence<>excluded.evidence OR agent_memory_profiles.basis<>excluded.basis
            RETURNING id""", (owner, stream, subject, dimension, text.strip(), basis, evidence))
        saved = cursor.fetchone()
        changed = saved is not None
        if not saved:
            saved = self.db.execute("""SELECT id FROM agent_memory_profiles WHERE
                owner=? AND stream=? AND subject=? AND dimension=? AND text=?""",
                (owner, stream, subject, dimension, text.strip())).fetchone()
        replaced = set(targets) - {saved[0]}
        self.db.executemany("DELETE FROM agent_memory_profiles WHERE owner=? AND stream=? AND id=?",
                            [(owner, stream, target) for target in replaced])
        for target in replaced:
            existing.pop(target)
        if changed or replaced:
            return {"id": saved[0], "subject": subject, "dimension": dimension, "text": text.strip(), "basis": basis,
                    "evidence": json.loads(evidence), "replaced": sorted(replaced)}
