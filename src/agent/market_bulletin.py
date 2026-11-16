"""Shared, administrator-owned market bulletin and durable release wakeups."""
import asyncio
import hashlib
import json
import logging
import re
import time
from datetime import datetime, timedelta
from email.utils import parsedate_to_datetime
from html import unescape
from xml.etree import ElementTree
from zoneinfo import ZoneInfo

from aiohttp import ClientSession, ClientTimeout
from langchain_core.messages import HumanMessage, SystemMessage
from yarl import URL

from src.core.availability import WEEKEND, inside_weekly_window
from src.core.engine import PAIRS
from .model_registry import model_client
from .workflow import workflow

ZONE = ZoneInfo("Asia/Shanghai")
EASTERN = ZoneInfo("America/New_York")
FED = "https://www.federalreserve.gov/feeds/press_monetary.xml"
ECB = "https://www.ecb.europa.eu/rss/press.html"
BLS = "https://www.bls.gov/feed/bls_latest.rss"
CALENDAR = "https://www.bls.gov/schedule/news_release/bls.ics"
BEA = "https://apps.bea.gov/rss/rss.xml"
BEA_CALENDAR = "https://www.bea.gov/news/schedule/ics/online-calendar-subscription.ics"
FEEDS = {"美联储": FED, "欧洲央行": ECB, "美国劳工统计局": BLS, "美国经济分析局": BEA}
CALENDARS = {"美国劳工统计局": CALENDAR, "美国经济分析局": BEA_CALENDAR}
KINDS = {"day": "日报", "week": "周报", "month": "月报", "event": "指标跟踪"}
DEFAULT = {"enabled": True, "interval": 60, "time": "08:30", "periods": ["day", "week", "month"],
           "pairs": list(PAIRS)[:4], "url": "", "model": "", "key": ""}
log = logging.getLogger("kurumi")


def model_ready(config):
    return all(config[key] for key in ("url", "model", "key"))


def period_window(kind, now, clock):
    hour, minute = map(int, clock.split(":"))
    cutoff = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if now < cutoff:
        cutoff -= timedelta(days=1)
    end = cutoff.replace(hour=0, minute=0)
    if kind == "day":
        start = end - timedelta(days=1)
    elif kind == "week":
        end -= timedelta(days=end.weekday())
        start = end - timedelta(days=7)
    else:
        end = end.replace(day=1)
        start = (end - timedelta(days=1)).replace(day=1)
    return start, end


def clean(value):
    return " ".join(unescape(re.sub(r"<[^>]*>", " ", value or "")).split())


def parse_rss(payload, source):
    root = ElementTree.fromstring(payload)
    for item in root.findall(".//item"):
        title = clean(item.findtext("title"))[:250]
        link = item.findtext("link", "").strip()
        try:
            stamp = parsedate_to_datetime(item.findtext("pubDate", "")).timestamp()
            url = URL(link)
            if not title or url.scheme != "https" or not url.host or url.user:
                continue
        except (ValueError, TypeError, OverflowError):
            continue
        yield (link, source, title, clean(item.findtext("description"))[:450], stamp)


def parse_calendar(payload, source=""):
    unfolded = re.sub(r"\r?\n[ \t]", "", payload)
    for block in re.findall(r"BEGIN:VEVENT\s*(.*?)END:VEVENT", unfolded, flags=re.S):
        fields = {}
        for line in block.splitlines():
            key, sep, value = line.partition(":")
            if sep:
                fields[key.split(";")[0]] = (key, value)
        title = fields.get("SUMMARY", ("", ""))[1]
        key, value = fields.get("DTSTART", ("", ""))
        if not title or not value or fields.get("STATUS", ("", ""))[1] == "CANCELLED":
            continue
        try:
            if value.endswith("Z"):
                stamp = datetime.strptime(value, "%Y%m%dT%H%M%SZ").replace(tzinfo=ZoneInfo("UTC"))
            else:
                tz = re.search(r"TZID=([^;]+)", key)
                stamp = datetime.strptime(value, "%Y%m%dT%H%M%S").replace(tzinfo=ZoneInfo(tz[1]) if tz else EASTERN)
        except (ValueError, KeyError):
            continue
        identity = fields.get("UID", ("", ""))[1] or title + value
        event_id = hashlib.sha256((source + ":" + identity).encode()).hexdigest()[:20]
        yield (event_id, clean(title.replace(chr(92) + ",", ","))[:240], int(stamp.timestamp()))


class MarketBulletin:
    def __init__(self, app):
        self.app, self.db = app, app.db
        self.lock = asyncio.Lock()
        self.generation = 0
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS platform_bulletin_config(id INTEGER PRIMARY KEY CHECK(id=1), data TEXT);
            CREATE TABLE IF NOT EXISTS platform_bulletin_news(
                url TEXT PRIMARY KEY, source TEXT, title TEXT, excerpt TEXT, published REAL);
            CREATE INDEX IF NOT EXISTS platform_bulletin_news_date ON platform_bulletin_news(published);
            CREATE TABLE IF NOT EXISTS platform_bulletin_events(
                id TEXT PRIMARY KEY, title TEXT, due INTEGER, active INTEGER NOT NULL DEFAULT 1,
                source TEXT NOT NULL DEFAULT '美国劳工统计局');
            CREATE TABLE IF NOT EXISTS platform_bulletin_wakeups(
                event TEXT PRIMARY KEY REFERENCES platform_bulletin_events(id), task TEXT NOT NULL,
                state TEXT NOT NULL DEFAULT 'pending', attempts INTEGER NOT NULL DEFAULT 0,
                retry_at INTEGER NOT NULL DEFAULT 0, finished INTEGER);
            CREATE TABLE IF NOT EXISTS platform_bulletins(
                id TEXT PRIMARY KEY, kind TEXT, label TEXT, body TEXT, generated INTEGER, details TEXT);
            CREATE TABLE IF NOT EXISTS platform_bulletin_runs(
                kind TEXT PRIMARY KEY, attempted INTEGER, success INTEGER, error TEXT);
        """)
        if "source" not in {row[1] for row in self.db.execute("PRAGMA table_info(platform_bulletin_events)")}:
            self.db.execute("ALTER TABLE platform_bulletin_events ADD COLUMN source TEXT NOT NULL DEFAULT '美国劳工统计局'")

    def config(self):
        row = self.db.execute("SELECT data FROM platform_bulletin_config WHERE id=1").fetchone()
        return DEFAULT | (json.loads(row[0]) if row else {})

    def save(self, body):
        old = self.config()
        config = {key: body.get(key, old[key]) for key in DEFAULT}
        if type(config["enabled"]) is not bool:
            raise ValueError("请设置启用状态")
        if type(config["interval"]) is not int or not 15 <= config["interval"] <= 1440:
            raise ValueError("采集间隔须为 15～1440 分钟")
        if not isinstance(config["time"], str) or not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", config["time"]):
            raise ValueError("请选择有效的发布时间")
        for key, allowed in (("periods", ("day", "week", "month")), ("pairs", PAIRS)):
            if not isinstance(config[key], list) or any(not isinstance(x, str) or x not in allowed for x in config[key]):
                raise ValueError("播报配置选项无效")
            config[key] = list(dict.fromkeys(config[key]))
        for key in ("url", "model", "key"):
            if not isinstance(config[key], str) or len(config[key]) > 1500:
                raise ValueError("模型设置无效")
            config[key] = config[key].strip()
        if body.get("clear_key") is True:
            config["key"] = ""
        elif config["url"] != old["url"] and not body.get("key", "").strip():
            config["key"] = ""
        elif not config["key"]:
            config["key"] = old["key"]
        if config["url"]:
            self.app.model_url(config["url"])
        self.db.execute("INSERT OR REPLACE INTO platform_bulletin_config VALUES(1,?)", (json.dumps(config),))
        if config["enabled"] and model_ready(config) and (not old["enabled"] or not model_ready(old)):
            self.db.execute("UPDATE platform_bulletin_runs SET attempted=0 WHERE kind='scan'")
        self.generation += 1

    def quotes(self, config):
        return [{"pair": pair, **{key: q.get(key) for key in ("bid", "ask", "timestamp", "tradeable")}}
                for pair in config["pairs"] if (q := self.app.engine.quotes.get(pair))]

    def reports(self, limit, *, kind=None, report_id=None):
        conditions, params = [], []
        if kind is not None:
            conditions.append("kind=?")
            params.append(kind)
        if report_id is not None:
            conditions.append("id=?")
            params.append(report_id)
        where = " WHERE " + " AND ".join(conditions) if conditions else ""
        rows = self.db.execute(
            "SELECT id,kind,label,body,generated,details FROM platform_bulletins"
            + where + " ORDER BY generated DESC,rowid DESC LIMIT ?", (*params, limit),
        )
        return [dict(row) | {"details": json.loads(row["details"])} for row in rows]

    def public(self):
        config = self.config()
        if not config["enabled"]:
            return {"enabled": False}
        now = datetime.now(ZONE)
        end = now.replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(days=1)
        agenda = self.db.execute("""SELECT id,title,due,source FROM platform_bulletin_events
            WHERE active=1 AND due BETWEEN ? AND ? ORDER BY due LIMIT 20""", (time.time(), end.timestamp() - 1))
        quotes = self.quotes(config)
        state = "closed" if inside_weekly_window(now.timestamp(), WEEKEND) else (
            "live" if any(q["tradeable"] and now.timestamp() - (q["timestamp"] or 0) <= 300 for q in quotes) else "stale")
        return {"enabled": True, "model_ready": model_ready(config), "reports": self.reports(50), "quotes": quotes, "agenda": [dict(r) for r in agenda],
                "market": state, "demo": self.app.demo}

    def admin(self):
        config = self.config()
        ready = model_ready(config)
        key = config.pop("key")
        tasks = self.db.execute("""SELECT w.*,e.title,e.due,e.active FROM platform_bulletin_wakeups w
            JOIN platform_bulletin_events e ON e.id=w.event
            ORDER BY (w.state='pending') DESC,CASE WHEN w.state='pending' THEN e.due ELSE -e.due END LIMIT 60""")
        next_events = self.db.execute("SELECT title,due,source FROM platform_bulletin_events WHERE active=1 AND due>=? ORDER BY due LIMIT 8", (time.time(),))
        return {"config": config | {"has_key": bool(key), "model_ready": ready}, "available_pairs": list(PAIRS),
                "tasks": [dict(row) for row in tasks], "agenda": [dict(row) for row in next_events],
                "runs": [dict(row) for row in self.db.execute("SELECT * FROM platform_bulletin_runs")],
                "reports": self.reports(8)}

    def manage_task(self, action, event):
        if not isinstance(event, str):
            raise ValueError("请选择指标任务")
        state, target = ("pending", "cancelled") if action == "cancel" else ("failed", "pending")
        row = self.db.execute("""UPDATE platform_bulletin_wakeups SET state=?,
            attempts=CASE WHEN ?='pending' THEN 0 ELSE attempts END,retry_at=0,
            finished=CASE WHEN ?='pending' THEN NULL ELSE unixepoch() END
            WHERE event=? AND state=? AND EXISTS(SELECT 1 FROM platform_bulletin_events
                WHERE id=event AND active=1)""", (target, target, target, event, state))
        if not row.rowcount:
            raise ValueError("任务状态已变化，请刷新")
        self.generation += 1

    async def fetch(self, session, address):
        async with session.get(address, allow_redirects=False) as response:
            response.raise_for_status()
            if response.status != 200:
                raise ValueError("来源未直接返回内容")
            data = bytearray()
            async for chunk in response.content.iter_chunked(16384):
                data.extend(chunk)
                if len(data) > 1_000_000:
                    raise ValueError("来源返回内容过大")
            return data.decode("utf-8-sig")

    async def collect(self):
        async def read(session, label, source, address, calendar=False):
            try:
                payload = await self.fetch(session, address)
                if calendar:
                    events = [(*item, source) for item in parse_calendar(payload, source)]
                    if not events:
                        raise ValueError("日历为空或格式无法识别")
                    self.db.execute("SAVEPOINT refresh_bulletin_calendar")
                    try:
                        self.db.execute("UPDATE platform_bulletin_events SET active=0 WHERE source=? AND due>?",
                                        (source, time.time()))
                        self.db.executemany("""INSERT INTO platform_bulletin_events(id,title,due,source) VALUES(?,?,?,?)
                            ON CONFLICT(id) DO UPDATE SET title=excluded.title,due=excluded.due,active=1""", events)
                    except BaseException:
                        self.db.execute("ROLLBACK TO refresh_bulletin_calendar")
                        raise
                    finally:
                        self.db.execute("RELEASE refresh_bulletin_calendar")
                else:
                    items = list(parse_rss(payload, source))
                    if not items:
                        raise ValueError("RSS 暂无可解析条目")
                    self.db.executemany("INSERT OR REPLACE INTO platform_bulletin_news VALUES(?,?,?,?,?)", items)
                return True
            except Exception as error:
                log.info("Bulletin source unavailable: %s (%s)", label, type(error).__name__)
                return False

        async with ClientSession(timeout=ClientTimeout(total=20), headers={"User-Agent": "KurumiMarketBulletin/1.0"}) as session:
            sources = [(name, name, url, False) for name, url in FEEDS.items()]
            sources += [(name + "日历", name, url, True) for name, url in CALENDARS.items()]
            results = await asyncio.gather(*(read(session, *source) for source in sources))
        failed = [row[0] for row, ok in zip(sources, results) if not ok]
        if len(failed) == len(sources):
            raise RuntimeError("所有公共消息源暂时不可用")
        return "暂未同步 " + "、".join(failed) if failed else ""

    async def ask(self, config, instruction, payload):
        """Use the administrator model with public sources and no model-callable tools."""
        result = await model_client(config, self.app.model_url).ainvoke([
            SystemMessage(content=workflow("market_bulletin") + "\n" + instruction),
            HumanMessage(content=json.dumps(payload, ensure_ascii=False)),
        ])
        text = result.text.strip()
        if not text:
            raise ValueError("模型返回空内容")
        return text

    async def plan(self, config, generation):
        rows = self.db.execute("""SELECT e.id,e.title,e.due,e.source FROM platform_bulletin_events e
            LEFT JOIN platform_bulletin_wakeups w ON w.event=e.id WHERE e.active=1 AND e.due BETWEEN ? AND ?
            AND w.event IS NULL ORDER BY e.due LIMIT 40""", (time.time(), time.time() + 14 * 86400))
        candidates = {r["id"]: dict(r) for r in rows}
        if not candidates:
            return
        raw = await self.ask(config, workflow("market_bulletin_plan"), list(candidates.values()))
        if raw.startswith(chr(96) * 3):
            raw = raw.partition("\n")[2].rsplit(chr(96) * 3, 1)[0]
        plan = json.loads(raw)
        if not isinstance(plan, list):
            raise ValueError("无效的唤起任务")
        if generation != self.generation:
            return
        for item in plan[:10]:
            if not isinstance(item, dict):
                continue
            event, task = item.get("event"), item.get("task")
            if isinstance(event, str) and event in candidates and isinstance(task, str) and task.strip():
                self.db.execute("""INSERT OR IGNORE INTO platform_bulletin_wakeups(event, task)
                    VALUES(?,?)""", (event, task.strip()[:240]))

    async def publish(self, kind, config, generation, *, event=None):
        now = datetime.now(ZONE)
        if event:
            label = event["title"]
            ident = "event:" + event["event"]
            start = datetime.fromtimestamp(event["due"] - 7200, ZONE)
            end = now
        else:
            start, end = period_window(kind, now, config["time"])
            label = f"{start.date()} 至 {(end - timedelta(days=1)).date()}" if kind != "day" else str(start.date())
            ident = kind + ":" + str(start.date())
        news = [dict(row) for row in self.db.execute("""SELECT * FROM platform_bulletin_news
            WHERE published>=? AND published<? AND (? IS NULL OR source=?) ORDER BY published,url""",
            (start.timestamp(), end.timestamp(), event["source"] if event else None, event["source"] if event else None))]
        ledger = []
        if len(news) > 25:
            for index in range(0, len(news), 25):
                ledger.append((await self.ask(config, "将这一批来源压缩成简短事实清单，保留关键日期与机构。",
                                              news[index:index + 25]))[:1500])
        payload = {"period": label, "type": KINDS[kind], "news": news if not ledger else ledger,
                   "items": len(news)}
        if kind == "day":
            day_end = now.replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(days=1)
            payload["today_agenda"] = [dict(r) for r in self.db.execute(
                "SELECT title,due FROM platform_bulletin_events WHERE active=1 AND due BETWEEN ? AND ? ORDER BY due LIMIT 20",
                (now.timestamp(), day_end.timestamp() - 1))]
        if event:
            payload["event"] = {"title": label, "due": event["due"], "task": event["task"], "source": event["source"]}
        body = await self.ask(config, "以短段落输出中文简报。写清市场要点和需关注的事项。资料为空时明确说明。"
            "指标结果未在材料中证实时，写明尚未核实，禁止编造数值。", payload)
        if generation != self.generation:
            return False
        details = {"news_count": len(news), "links": [(item["title"], item["url"]) for item in news[-12:]]}
        self.db.execute("INSERT OR REPLACE INTO platform_bulletins VALUES(?,?,?,?,?,?)",
                        (ident, kind, label, body[:7000], int(time.time()), json.dumps(details)))
        inbox = getattr(self.app, "agent_inbox", None)
        if inbox:
            inbox.market(ident, kind, f"{KINDS[kind]} · {label}")
        return True

    async def run(self, action):
        async with self.lock:
            config, generation = self.config(), self.generation
            if not config["enabled"]:
                return
            if action == "scan":
                await self.track("scan", self.scan(config, generation))
            elif action in ("day", "week", "month"):
                if not model_ready(config):
                    raise ValueError("请先配置管理员播报模型")
                await self.track(action, self.publish(action, config, generation))
            elif action == "tick":
                await self.tick(config, generation)

    async def scan(self, config, generation):
        warning = await self.collect()
        if generation == self.generation and model_ready(config):
            try:
                await self.plan(config, generation)
            except (ValueError, TypeError):
                warning = "；".join(filter(None, [warning, "采集成功，模型未返回有效的唤起任务"]))
        return warning

    async def track(self, name, job):
        now = int(time.time())
        self.db.execute("""INSERT INTO platform_bulletin_runs(kind,attempted) VALUES(?,?)
            ON CONFLICT(kind) DO UPDATE SET attempted=excluded.attempted""", (name, now))
        try:
            note = await job
        except Exception as error:
            log.warning("Market bulletin %s failed (%s)", name, type(error).__name__)
            self.db.execute("UPDATE platform_bulletin_runs SET error=? WHERE kind=?", (
                "采集或模型调用失败，请检查数据源与模型配置", name))
            return False
        if note is False:
            return False
        self.db.execute("UPDATE platform_bulletin_runs SET success=?,error=? WHERE kind=?",
                        (int(time.time()), note if isinstance(note, str) else "", name))
        return True

    async def tick(self, config, generation):
        now = datetime.now(ZONE)
        stamp = int(now.timestamp())
        if not model_ready(config):
            await self.collect_if_due(config, generation, stamp)
            return
        for event in self.db.execute("""SELECT w.*,e.title,e.due,e.source FROM platform_bulletin_wakeups w
            JOIN platform_bulletin_events e ON e.id=w.event WHERE w.state='pending' AND e.active=1
            AND e.due<=? AND w.retry_at<=? ORDER BY e.due LIMIT 4""", (stamp, stamp)).fetchall():
            if generation != self.generation:
                return
            rescheduled = False

            async def wake():
                nonlocal rescheduled
                await self.collect()
                current = self.db.execute("SELECT title,due,active FROM platform_bulletin_events WHERE id=?",
                                          (event["event"],)).fetchone()
                rescheduled = not current or not current["active"] or current["due"] != event["due"]
                if rescheduled or generation != self.generation:
                    return False
                fresh = self.db.execute("""SELECT 1 FROM platform_bulletin_news WHERE
                    source=? AND published>=? AND published<=? LIMIT 1""",
                    (event["source"], event["due"] - 60, time.time())).fetchone()
                if not fresh and event["attempts"] < 2:
                    return "尚未收到同机构的新公告，5 分钟后再次核查"
                return await self.publish("event", config, generation, event=event)
            ok = await self.track("event", wake())
            if generation != self.generation:
                return
            if rescheduled:
                continue
            attempts = event["attempts"] + 1
            published = self.db.execute("SELECT 1 FROM platform_bulletins WHERE id=?",
                                        ("event:" + event["event"],)).fetchone()
            state = "done" if ok and published else "failed" if attempts >= 3 else "pending"
            self.db.execute("UPDATE platform_bulletin_wakeups SET state=?,attempts=?,retry_at=?,finished=? WHERE event=?",
                            (state, attempts, stamp + 300, None if state == "pending" else stamp, event["event"]))
        runs = {row["kind"]: row["attempted"] for row in self.db.execute("SELECT * FROM platform_bulletin_runs")}
        if generation != self.generation:
            return
        await self.collect_if_due(config, generation, stamp)
        for kind in config["periods"]:
            if generation != self.generation:
                return
            start, _ = period_window(kind, now, config["time"])
            if (stamp - runs.get(kind, 0) >= 900 and not self.db.execute(
                    "SELECT 1 FROM platform_bulletins WHERE id=?", (kind + ":" + str(start.date()),)).fetchone()):
                await self.track(kind, self.publish(kind, config, generation))

    async def collect_if_due(self, config, generation, stamp):
        last = self.db.execute("SELECT attempted FROM platform_bulletin_runs WHERE kind='scan'").fetchone()
        if not last or stamp - last[0] >= config["interval"] * 60:
            await self.track("scan", self.scan(config, generation))

    async def loop(self):
        while True:
            try:
                await self.run("tick")
            except Exception:
                log.exception("Market bulletin scheduler failed")
            await asyncio.sleep(20)
