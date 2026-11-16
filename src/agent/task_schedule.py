import json
import math
import time
from datetime import datetime
from decimal import Decimal, InvalidOperation

from src.core.engine import PAIRS, dump

ACCOUNT_EVENTS = ("liquidation", "stop_loss", "take_profit", "margin_call", "deposit")
EVENT_LABELS = dict(zip(ACCOUNT_EVENTS, ("模拟强平", "止损成交", "止盈成交", "保证金不足", "追加虚拟资金")))


class TaskSchedule:
    def __init__(self, app):
        self.app, self.db = app, app.db
        self.db.executescript("""
        CREATE TABLE IF NOT EXISTS agent_task_appointments(
          id INTEGER PRIMARY KEY AUTOINCREMENT, owner TEXT REFERENCES sim_users(id) ON DELETE CASCADE,
          kind TEXT NOT NULL, purpose TEXT NOT NULL, trigger TEXT NOT NULL, target TEXT NOT NULL,
          created REAL NOT NULL, expires REAL NOT NULL, state TEXT NOT NULL DEFAULT 'waiting',
          previous TEXT, task INTEGER);
        CREATE TABLE IF NOT EXISTS agent_task_events(
          id INTEGER PRIMARY KEY AUTOINCREMENT, kind TEXT NOT NULL, event_key TEXT NOT NULL,
          owner TEXT, payload TEXT NOT NULL, created REAL NOT NULL, handled INTEGER NOT NULL DEFAULT 0,
          UNIQUE(kind,event_key));
        """)

    def account_event(self, game, entry):
        if not game.get("owner_id") or entry["status"] != "success":
            return
        kind = entry["action"]
        if kind == "close" and entry["source"] == "system":
            if entry["detail"].get("kind") == "liquidation":
                kind = "liquidation"
            else:
                kind = {"触发止损": "stop_loss", "触发止盈": "take_profit"}.get(entry["reason"])
        if kind not in ACCOUNT_EVENTS or (kind == "margin_call" and entry["source"] != "system"):
            return
        position = entry["detail"].get("position") or {}
        self.event(kind, str(entry["id"]), game["owner_id"], {
            "game_id": game["id"], "log_id": entry["id"], "pair": position.get("pair"),
            "position_id": position.get("id"), "reason": entry["reason"],
            "amount": entry["detail"].get("amount"), "detail": entry["detail"],
        })

    def event(self, kind, key, owner, payload):
        self.db.execute("""INSERT OR IGNORE INTO agent_task_events(kind,event_key,owner,payload,created)
                           VALUES(?,?,?,?,?)""", (kind, key, owner, dump(payload), time.time()))

    @staticmethod
    def number(value):
        try:
            price = Decimal(str(value))
            if not price.is_finite() or price <= 0:
                raise ValueError("价格须为正数")
            return price
        except InvalidOperation:
            raise ValueError("价格须为正数") from None

    def schedule(self, owner, kind, purpose, trigger, *, room_id=0, bulletin_id="", message_id=0):
        if kind not in ("watch", "patrol", "private_followup", "group_followup") or not 1 <= len(purpose.strip()) <= 240:
            raise ValueError("任务类型无效，或任务目的超过 240 字")
        if kind == "group_followup" and (not room_id or not self.app.social.joined(room_id, owner)):
            raise ValueError("智能体尚未加入该群")
        if kind != "group_followup" and (room_id or message_id):
            raise ValueError("群聊和消息引用仅可用于群聊跟进")
        if message_id and not self.db.execute("SELECT 1 FROM community_group_messages WHERE id=? AND room=? AND status='active'",
                                              (message_id, room_id)).fetchone():
            raise ValueError("引用的群消息不存在")
        if bulletin_id and not self.db.execute("SELECT 1 FROM platform_bulletins WHERE id=?", (bulletin_id,)).fetchone():
            raise ValueError("引用的播报不存在")
        if self.app.external.settings(owner)["mode"] != "platform":
            raise ValueError("平台智能体未启用")
        now = time.time()
        mode = trigger.get("type", "immediate")
        rule = {"type": mode}
        if mode == "immediate":
            rule["due"] = now
        elif mode == "delay":
            delay = trigger.get("seconds")
            if type(delay) not in (int, float) or not math.isfinite(delay) or not 0 < delay <= 30 * 86400:
                raise ValueError("延迟须在 30 天以内")
            rule["due"] = now + delay
        elif mode == "at":
            stamp = datetime.fromisoformat(str(trigger.get("time", "")))
            if stamp.tzinfo is None or not now <= stamp.timestamp() <= now + 30 * 86400:
                raise ValueError("请填写 30 天以内、带时区的未来时间")
            rule["due"] = stamp.timestamp()
        elif mode == "quote":
            pair, side, condition = (trigger.get(name) for name in ("pair", "side", "condition"))
            if pair not in PAIRS or side not in ("bid", "ask", "mid") or condition not in ("cross_up", "cross_down", "enter_range"):
                raise ValueError("报价条件无效")
            rule.update(pair=pair, side=side, condition=condition)
            if condition == "enter_range":
                low, high = self.number(trigger.get("low")), self.number(trigger.get("high"))
                if low > high:
                    raise ValueError("区间下限不能超过上限")
                rule.update(low=str(low), high=str(high))
            else:
                rule["price"] = str(self.number(trigger.get("price")))
        elif mode in ("account", "bulletin", "mention"):
            event = trigger.get("event")
            if mode == "account" and event not in ACCOUNT_EVENTS:
                raise ValueError("账户事件无效")
            if mode == "bulletin" and event not in (None, "", "live", "day", "week", "month", "event"):
                raise ValueError("市场播报类型无效")
            if mode == "mention" and event not in (None, "", "group"):
                raise ValueError("提及事件仅支持群聊")
            room = trigger.get("room_id", 0)
            if room and (mode != "mention" or type(room) is not int or not self.app.social.joined(room, owner)):
                raise ValueError("提及条件的群聊无效")
            title_contains = trigger.get("title_contains", "")
            if not isinstance(title_contains, str) or len(title_contains) > 80 or (title_contains and mode != "bulletin"):
                raise ValueError("标题筛选仅用于市场播报，最多 80 字")
            rule.update(event=event, room_id=room, title_contains=title_contains.strip())
        else:
            raise ValueError("未知触发类型")
        target = dump({"room_id": room_id, "bulletin_id": bulletin_id, "message_id": message_id})
        pending = self.db.execute("""SELECT id FROM agent_task_appointments WHERE owner=? AND kind=? AND purpose=?
            AND trigger=? AND target=? AND state='waiting'""", (owner, kind, purpose, dump(rule), target)).fetchone()
        if pending:
            return {"appointment_id": pending["id"], "state": "waiting"}
        if self.db.execute("SELECT count(*) FROM agent_task_appointments WHERE owner=? AND state='waiting'", (owner,)).fetchone()[0] >= 30:
            raise ValueError("每个智能体最多等待 30 个预约")
        ident = self.db.execute("""INSERT INTO agent_task_appointments(owner,kind,purpose,trigger,target,created,expires)
            VALUES(?,?,?,?,?,?,?)""", (owner, kind, purpose.strip(), dump(rule), target, now, now + 30 * 86400 + 60)).lastrowid
        return {"appointment_id": ident, "state": "waiting", "due": rule.get("due")}

    def quote_match(self, row, rule, now):
        quote = self.app.engine.quotes.get(rule["pair"], {})
        stamp = quote.get("timestamp", quote.get("updated", quote.get("time")))
        try:
            if not quote.get("tradeable") or not 0 <= now - float(stamp) <= 120:
                return None
            value = ((self.number(quote["bid"]) + self.number(quote["ask"])) / 2
                     if rule["side"] == "mid" else self.number(quote[rule["side"]]))
        except (ValueError, TypeError, KeyError):
            return None
        previous = Decimal(row["previous"]) if row["previous"] else None
        self.db.execute("UPDATE agent_task_appointments SET previous=? WHERE id=?", (str(value), row["id"]))
        if previous is None:
            return None
        condition = rule["condition"]
        if condition == "enter_range":
            low, high = Decimal(rule["low"]), Decimal(rule["high"])
            hit = low <= value <= high and not low <= previous <= high
        else:
            threshold = Decimal(rule["price"])
            hit = previous < threshold <= value if condition == "cross_up" else previous > threshold >= value
        return {"price": str(value), "previous": str(previous), "quote_at": stamp} if hit else None

    def dispatch(self, owner, kind, purpose, target, evidence, *, key=None, readonly=False):
        if evidence.get("event_id"):
            suffix = "" if kind == "watch" else ":".join(str(row["id"]) for row in evidence.get("appointments", []))
            dispatch_key = f"event:{evidence['event_id']}:{kind}:{suffix}"
        else:
            dispatch_key = f"appointment:{evidence['appointment_id']}"
        existing = self.db.execute("""SELECT t.id FROM agent_tasks t JOIN agent_task_reports r ON r.task=t.id
            WHERE t.owner=? AND json_extract(r.data,'$.dispatch_key')=?
                AND NOT (t.state='interrupted' AND t.started IS NULL)
            ORDER BY t.id DESC LIMIT 1""", (owner, dispatch_key)).fetchone()
        if existing:
            return existing["id"]

        async def execute():
            if self.app.external.settings(owner)["mode"] != "platform":
                self.app.timeline.note(owner, {"skip_reason": "平台智能体已停用"})
                return False
            if kind.endswith("_followup"):
                return await self.app.social._chat_handoff(owner, kind.split("_")[0], purpose,
                    target.get("room_id", 0), target.get("bulletin_id", ""), target.get("message_id", 0))
            if kind == "patrol":
                return await self.app.social._run_patrol(owner, handoff=purpose)
            game = next((g for g in self.app.engine.games() if g["owner_id"] == owner), None)
            if not game:
                self.app.timeline.note(owner, {"skip_reason": "没有可用交易工作区"})
                return False
            prompt = "处理预约看盘任务。目的：" + purpose + "\n触发证据：" + dump(evidence)
            return await self.app._agent(game, task_instruction=prompt, readonly=readonly)

        ident, _ = self.app.timeline.enqueue(owner, "account_check" if readonly else kind, execute, key=key,
            outcome=lambda result: ("已完成账户检查" if readonly else "已完成预约任务") if result else "已跳过",
            report={"purpose": purpose, "trigger": evidence, "dispatch_key": dispatch_key,
                    "appointment_dispatch": {"kind": kind, "target": target, "readonly": readonly}})
        return ident

    def recover(self):
        rows = self.db.execute("""SELECT t.id,t.owner,r.data FROM agent_tasks t
            JOIN agent_task_reports r ON r.task=t.id
            WHERE t.state='interrupted' AND t.started IS NULL
                AND json_type(r.data,'$.appointment_dispatch')='object'
                AND json_extract(r.data,'$.replaced_by') IS NULL""").fetchall()
        for row in rows:
            data = json.loads(row["data"])
            options = data["appointment_dispatch"]
            ident = self.dispatch(row["owner"], options["kind"], data["purpose"], options["target"],
                                  data["trigger"], readonly=options["readonly"])
            self.db.execute("UPDATE agent_task_reports SET data=json_set(data,'$.replaced_by',?) WHERE task=?",
                            (ident, row["id"]))
            self.db.execute("UPDATE agent_task_appointments SET task=? WHERE task=?", (ident, row["id"]))

    def tick(self):
        now = time.time()
        self.db.execute("UPDATE agent_task_appointments SET state='expired' WHERE state='waiting' AND expires<?", (now,))
        waiting = self.db.execute("SELECT * FROM agent_task_appointments WHERE state='waiting' ORDER BY id").fetchall()
        pending = [(row, json.loads(row["trigger"])) for row in waiting]
        fired = set()
        for event in self.db.execute("SELECT * FROM agent_task_events WHERE handled=0 ORDER BY id LIMIT 100").fetchall():
            kind, owner, payload = event["kind"], event["owner"], json.loads(event["payload"])
            batches = {}
            for row, rule in pending:
                if row["id"] in fired or row["created"] > event["created"] or (owner and row["owner"] != owner):
                    continue
                if not ((rule["type"] == "account" and rule["event"] == kind)
                    or (rule["type"] == kind and kind in ("bulletin", "mention")
                        and (not rule["event"] or rule["event"] == payload.get("kind"))
                        and (not rule.get("room_id") or rule["room_id"] == payload.get("room_id"))
                        and (not rule.get("title_contains") or rule["title_contains"].casefold()
                             in payload.get("title", "").casefold()))):
                    continue
                key = (row["owner"], row["kind"], 0 if row["kind"] == "watch" else row["id"])
                batches.setdefault(key, []).append((row, rule))
                fired.add(row["id"])
            automatic = kind in ACCOUNT_EVENTS and owner and self.app.profile(owner).get("account_wakeups", {}).get(kind, True)
            if automatic:
                batches.setdefault((owner, "watch", 0), [])
            for (recipient, task_kind, _), matches in batches.items():
                target = json.loads(matches[0][0]["target"]) if matches else {}
                if kind == "bulletin" and task_kind.endswith("_followup") and not target.get("bulletin_id"):
                    target["bulletin_id"] = payload.get("id", "")
                purposes = list(dict.fromkeys(row["purpose"] for row, _ in matches))
                system = bool(automatic and recipient == owner and task_kind == "watch")
                if system:
                    purposes.insert(0, "检查" + EVENT_LABELS[kind])
                evidence = {"event": kind, "event_id": event["id"], "event_at": event["created"], "match": payload,
                            "system": system, "appointments": [{"id": row["id"], "condition": rule} for row, rule in matches]}
                if (kind == "mention" and payload.get("normal_reply") and task_kind == "group_followup"
                        and target.get("room_id") == payload.get("room_id")):
                    ident = None  # The existing directed group task handles this mention.
                else:
                    ident = self.dispatch(recipient, task_kind, "；".join(purposes), target, evidence,
                                          readonly=system, key=("event", event["id"], task_kind) if task_kind == "watch" else None)
                for row, _ in matches:
                    self.db.execute("UPDATE agent_task_appointments SET state='triggered',task=? WHERE id=?", (ident, row["id"]))
            self.db.execute("UPDATE agent_task_events SET handled=1 WHERE id=?", (event["id"],))
        for row, rule in pending:
            if row["id"] in fired:
                continue
            match = self.quote_match(row, rule, now) if rule["type"] == "quote" else (
                {"due": rule["due"]} if rule.get("due", float("inf")) <= now else None)
            if match is None:
                continue
            ident = self.dispatch(row["owner"], row["kind"], row["purpose"], json.loads(row["target"]),
                                  {"appointment_id": row["id"], "condition": rule, "match": match})
            self.db.execute("UPDATE agent_task_appointments SET state='triggered',task=? WHERE id=?", (ident, row["id"]))
