"""Shared, administrator-owned market bulletin and durable release wakeups."""
import asyncio
import hashlib
import ipaddress
import json
import logging
import os
import re
import time
from contextlib import AsyncExitStack, asynccontextmanager
from datetime import datetime, timedelta
from email.utils import parsedate_to_datetime
from html import unescape
from html.parser import HTMLParser
from xml.etree import ElementTree
from zoneinfo import ZoneInfo

from aiohttp import ClientConnectorCertificateError, ClientConnectorDNSError, ClientError, ClientSession, ClientTimeout, TCPConnector
from aiohttp.resolver import ThreadedResolver
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
NEWS_SEARCH = "https://api.search.brave.com/res/v1/news/search"
MEDIA_PREFIX = "非官方 · "
BEA_CALENDAR = "https://www.bea.gov/news/schedule/ics/online-calendar-subscription.ics"
SOURCES = {
    "fed": ("美联储", FED, False, "federalreserve.gov"),
    "ecb": ("欧洲央行", ECB, False, "ecb.europa.eu"),
    "bls": ("美国劳工统计局", BLS, False, "bls.gov"),
    "bea": ("美国经济分析局", BEA, False, "bea.gov"),
    "bls_calendar": ("美国劳工统计局", CALENDAR, True, "bls.gov"),
    "bea_calendar": ("美国经济分析局", BEA_CALENDAR, True, "bea.gov"),
    "media": ("补充媒体", "", False, ""),
}
DOL_BLS_RELEASES = "https://www.dol.gov/index.php/newsroom/economicdata"
DOL_BLS_SOURCE = "美国劳工部 · BLS 报告转载"
CNBC_ECONOMY = "https://www.cnbc.com/id/20910258/device/rss/rss.html"
FOREX_FACTORY_CALENDAR = "https://nfs.faireconomy.media/ff_calendar_thisweek.ics"
BEA_CALENDAR_JSON = "https://apps.bea.gov/API/signup/release_dates.json"
SOURCE_ALTERNATIVES = {
    "fed": ("美联储全部新闻稿", "https://www.federalreserve.gov/feeds/press_all.xml"),
    "ecb": ("欧洲央行统计新闻稿", "https://www.ecb.europa.eu/rss/statpress.html"),
    "bls": ("美国劳工部转载的 BLS 正式报告", DOL_BLS_RELEASES),
    "bls_calendar": ("Forex Factory 美国周历（非官方）", FOREX_FACTORY_CALENDAR),
    "bea": ("CNBC 经济新闻（非官方）", CNBC_ECONOMY),
    "bea_calendar": ("BEA 官方 JSON 日历", BEA_CALENDAR_JSON),
    "media": ("CNBC 经济新闻（非官方）", CNBC_ECONOMY),
}
BUILTIN_SOURCE_URLS = frozenset(value[1] for value in SOURCES.values() if value[1]) | frozenset(
    address for _, address in SOURCE_ALTERNATIVES.values()
)
KINDS = {"live": "实时报告", "day": "日报", "week": "周报", "month": "月报", "event": "指标跟踪"}
DEFAULT = {"enabled": True, "interval": 60, "time": "08:30", "periods": ["day", "week", "month"],
           "pairs": list(PAIRS)[:4], "url": "", "model": "", "key": ""}
log = logging.getLogger("kurumi")


class PublicSourceResolver(ThreadedResolver):
    async def resolve(self, host, port=0, family=0):
        addresses = await super().resolve(host, port, family)
        public = [record for record in addresses
                  if ipaddress.ip_address(record["host"].split("%")[0]).is_global]
        if not public:
            raise OSError("来源域名未解析到公网 IP")
        return public


def source_url(value):
    if not isinstance(value, str) or len(value) > 1500:
        raise ValueError("来源地址无效")
    url = URL(value.strip())
    if url.scheme != "https" or not url.host or url.user is not None or url.port != 443 or url.fragment:
        raise ValueError("请输入公网 HTTPS 订阅地址")
    try:
        address = ipaddress.ip_address(url.host)
    except ValueError:
        if ("." not in url.host or re.fullmatch(r"[\d.]+", url.host)
                or url.host.lower().rstrip(".").endswith((".localhost", ".local", ".internal"))):
            raise ValueError("请输入公网 HTTPS 订阅地址") from None
    else:
        if not address.is_global:
            raise ValueError("请输入公网 HTTPS 订阅地址")
    return str(url)


def source_details(source_id, address):
    name, _, calendar, domain = SOURCES[source_id]
    host = URL(address).host.lower().rstrip(".")
    official = bool(domain) and (host == domain or host.endswith("." + domain))
    source = name if official else MEDIA_PREFIX + host.removeprefix("www.")
    if address == FOREX_FACTORY_CALENDAR:
        source = MEDIA_PREFIX + "Forex Factory"
    elif address == DOL_BLS_RELEASES:
        source = DOL_BLS_SOURCE
    return name + ("日历" if calendar else ""), source, calendar


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


def parse_dol_bls_reports(payload):
    class ReportLinks(HTMLParser):
        def __init__(self):
            super().__init__()
            self.link = None
            self.title = []
            self.items = {}

        def handle_starttag(self, tag, attrs):
            if tag == "a":
                href = dict(attrs).get("href") or ""
                self.link = re.fullmatch(r"(/newsroom/economicdata/[a-z0-9]+_(\d{8})\.pdf)", href, re.I)
                self.title = []

        def handle_data(self, data):
            if self.link:
                self.title.append(data)

        def handle_endtag(self, tag):
            if tag != "a" or not self.link:
                return
            path, date = self.link.groups()
            self.link = None
            title = clean(" ".join(self.title))[:250]
            try:
                published = datetime.strptime(date, "%m%d%Y").replace(tzinfo=EASTERN).timestamp()
            except ValueError:
                return
            if title:
                url = "https://www.dol.gov" + path
                self.items[url] = (url, DOL_BLS_SOURCE, title, "美国劳工部转载的 BLS 正式报告。仅有美国东部发布日期和 PDF 原文，未提供具体发布时间。", published)

    parser = ReportLinks()
    parser.feed(payload)
    parser.close()
    return list(parser.items.values())


def parse_source(payload, source, calendar, address):
    if not calendar and address == DOL_BLS_RELEASES:
        return parse_dol_bls_reports(payload)
    if not calendar:
        return list(parse_rss(payload, source))
    if address == FOREX_FACTORY_CALENDAR:
        return [(ident, title.split("US ", 1)[1], due) for ident, title, due in parse_calendar(payload, source)
                if re.match(r"^(?:[⁎⁑⁂] )?US ", title)]
    if address != BEA_CALENDAR_JSON:
        return list(parse_calendar(payload, source))
    events = {}
    schedule = json.loads(payload)
    if not isinstance(schedule, dict):
        raise ValueError("日历 JSON 格式无法识别")
    for title, entry in schedule.items():
        dates = entry.get("release_dates") if isinstance(entry, dict) else None
        if not isinstance(dates, list):
            continue
        for value in dates:
            try:
                stamp = datetime.fromisoformat(value)
            except (ValueError, TypeError):
                continue
            if stamp.tzinfo is None:
                continue
            seconds = int(stamp.timestamp())
            identity = hashlib.sha256(f"{source}:{title}:{seconds}".encode()).hexdigest()[:20]
            events[identity] = (identity, clean(title)[:240], seconds)
    return list(events.values())


class MarketBulletin:
    def __init__(self, app):
        self.app, self.db = app, app.db
        self.lock = asyncio.Lock()
        self.generation = 0
        self.weekly_calendar_check = (0.0, "")
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
        config = DEFAULT | (json.loads(row[0]) if row else {})
        config["sources"] = {key: url for key, (_, url, _, _) in SOURCES.items()} | config.get("sources", {})
        return config

    def save(self, body):
        old = self.config()
        config = {key: body.get(key, old[key]) for key in DEFAULT}
        overrides = body.get("sources", {})
        if not isinstance(overrides, dict) or set(overrides) - SOURCES.keys():
            raise ValueError("订阅来源设置无效")
        config["sources"] = {
            key: source_url(address) if address else ""
            for key in SOURCES
            for address in [overrides.get(key, old["sources"][key]) or SOURCES[key][1]]
        }
        if type(config["enabled"]) is not bool:
            raise ValueError("请设置启用状态")
        if type(config["interval"]) is not int or not 1 <= config["interval"] <= 1440:
            raise ValueError("采集间隔须为 1～1440 分钟")
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
        for key, (_, _, calendar, _) in SOURCES.items():
            if calendar and config["sources"][key] != old["sources"][key]:
                _, source, _ = source_details(key, old["sources"][key])
                self.db.execute("UPDATE platform_bulletin_events SET active=0 WHERE source=? AND due>?",
                                (source, time.time()))
        if config["sources"] != old["sources"]:
            self.weekly_calendar_check = (0.0, "")
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
        archive = [report for kind, limit in (("live", 24), ("event", 12), ("day", 10), ("week", 6), ("month", 4))
                   for report in self.reports(limit, kind=kind)]
        archive.sort(key=lambda report: report["generated"], reverse=True)
        return {"enabled": True, "model_ready": model_ready(config), "reports": archive,
                "quotes": quotes, "agenda": [dict(r) for r in agenda],
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
                "sources": [dict(id=key, label=name + ("日历" if calendar else ""),
                                 default_url=default, format=("ICS / JSON" if key == "bea_calendar" else "ICS")
                                 if calendar else "RSS / 官方报告" if key == "bls" else "RSS",
                                 alternative=SOURCE_ALTERNATIVES.get(key))
                            for key, (name, default, calendar, _) in SOURCES.items()],
                "search_ready": bool(os.environ.get("BRAVE_SEARCH_API_KEY", "").strip()),
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
                raise ValueError(f"HTTP {response.status}，来源未直接返回内容")
            data = bytearray()
            async for chunk in response.content.iter_chunked(16384):
                data.extend(chunk)
                if len(data) > 1_000_000:
                    raise ValueError("来源返回内容过大")
            return data.decode("utf-8-sig")

    @staticmethod
    @asynccontextmanager
    async def source_session(*, builtin=False):
        resolver = None if builtin else PublicSourceResolver()
        try:
            async with ClientSession(connector=TCPConnector(resolver=resolver), timeout=ClientTimeout(total=20)) as session:
                yield session
        finally:
            if resolver:
                await resolver.close()

    async def check_source(self, source_id, address):
        if not isinstance(source_id, str) or source_id not in SOURCES:
            raise ValueError("请选择要检查的订阅来源")
        address = address or SOURCES[source_id][1]
        if not address:
            raise ValueError("请先填写订阅地址")
        address = source_url(address)
        _, source, calendar = source_details(source_id, address)
        try:
            async with self.source_session(builtin=address in BUILTIN_SOURCE_URLS) as session:
                payload = await self.fetch(session, address)
            count = len(parse_source(payload, source, calendar, address))
        except Exception as error:
            return {"ok": False, "message": self.source_error(error)}
        if not count:
            return {"ok": False, "message": "可访问，但未解析到有效条目"}
        return {"ok": True, "message": f"连接成功，解析到 {count} 条"}

    @staticmethod
    def source_error(error):
        status = getattr(error, "status", 0)
        if status:
            hint = {401: "，来源拒绝访问", 403: "，来源拒绝访问", 429: "，来源请求限流"}
            return f"HTTP {status}" + hint.get(status, "")
        if isinstance(error, TimeoutError):
            return "连接超时"
        if isinstance(error, ClientConnectorCertificateError):
            return "TLS 证书校验失败"
        if isinstance(error, ClientConnectorDNSError):
            if str(error.__cause__) == "来源域名未解析到公网 IP":
                return str(error.__cause__)
            return "DNS 域名解析失败"
        if isinstance(error, ClientError):
            return "连接失败（请检查服务器出站网络）"
        if isinstance(error, (ElementTree.ParseError, UnicodeError)):
            return "来源格式无法解析"
        if type(error) is ValueError:
            return str(error)
        return "采集失败"

    async def search_news(self, query):
        key = os.environ.get("BRAVE_SEARCH_API_KEY", "").strip()
        if not key:
            return [], ""
        try:
            async with ClientSession(timeout=ClientTimeout(total=12)) as session:
                async with session.get(NEWS_SEARCH, params={"q": query[:300], "count": 10, "freshness": "pd"},
                                       headers={"X-Subscription-Token": key}, allow_redirects=False) as response:
                    if response.status != 200:
                        return [], f"联网新闻搜索（HTTP {response.status}）"
                    results = (await response.json()).get("results", [])
        except (TimeoutError, ClientError, ValueError):
            return [], "联网新闻搜索暂不可用"
        items = []
        for result in results:
            try:
                url = URL(result["url"])
                when = datetime.fromisoformat(result["page_age"].replace("Z", "+00:00"))
                stamp = (when if when.tzinfo else when.replace(tzinfo=ZoneInfo("UTC"))).timestamp()
                title = clean(result["title"])[:250]
                if url.scheme != "https" or not url.host or url.user or not title:
                    continue
                source = MEDIA_PREFIX + url.host.removeprefix("www.")
                items.append((str(url.with_fragment(None)), source, title,
                              clean(result.get("description"))[:450], stamp))
            except (AttributeError, KeyError, OverflowError, TypeError, ValueError):
                continue
        return items, ""

    async def collect(self):
        async def read(session, label, source, address, calendar=False):
            weekly = address == FOREX_FACTORY_CALENDAR
            checked, previous_error = self.weekly_calendar_check
            if weekly and checked and time.monotonic() - checked < 900:
                return [], previous_error
            try:
                payload = await self.fetch(session, address)
                if calendar:
                    events = [(*item, source) for item in parse_source(payload, source, True, address)]
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
                    if weekly:
                        self.weekly_calendar_check = (time.monotonic(), "")
                    return [], ""
                items = parse_source(payload, source, False, address)
                if not items:
                    raise ValueError("RSS 暂无可解析条目")
                return items, ""
            except Exception as error:
                reason = self.source_error(error)
                if weekly:
                    self.weekly_calendar_check = (time.monotonic(), reason)
                log.info("Bulletin source unavailable: %s (%s)", label, reason)
                return [], reason

        sources = [(*source_details(key, address), address) for key, address in self.config()["sources"].items() if address]
        async with AsyncExitStack() as stack:
            default_session = await stack.enter_async_context(self.source_session(builtin=True))
            custom_session = await stack.enter_async_context(self.source_session())
            results = await asyncio.gather(*(read(default_session if address in BUILTIN_SOURCE_URLS else custom_session,
                                                 label, source, address, calendar)
                                             for label, source, calendar, address in sources))
        failed = [f"{source[0]}（{error}）" for source, (_, error) in zip(sources, results) if error]
        media, search_error = await self.search_news("forex dollar euro yen central banks inflation jobs economic data markets")
        issues = failed + ([search_error] if search_error else [])
        warning = "暂未同步 " + "、".join(issues) if issues else ""
        if len(failed) == len(sources) and not media:
            raise ValueError(warning)
        if self.config()["enabled"]:
            self.ingest_news([item for items, _ in results for item in items] + media)
        return warning

    def ingest_news(self, items):
        now = time.time()
        fresh = []
        self.db.execute("SAVEPOINT ingest_bulletin_news")
        try:
            for url, source, title, excerpt, published in items:
                previous = self.db.execute("SELECT published,source FROM platform_bulletin_news WHERE url=?", (url,)).fetchone()
                if previous and (source.startswith(MEDIA_PREFIX) or
                                 (not previous["source"].startswith(MEDIA_PREFIX) and previous["published"] > published)):
                    continue
                self.db.execute("INSERT OR REPLACE INTO platform_bulletin_news VALUES(?,?,?,?,?)",
                                (url, source, title, excerpt, published))
                if (not previous or previous[0] < published) and now - 86400 <= published <= now:
                    fresh.append((url, source, title, excerpt, published))
            if fresh:
                fresh.sort(key=lambda item: (item[4], item[0]), reverse=True)
                headline = fresh[0][2]
                if fresh[0][1].startswith(MEDIA_PREFIX):
                    headline = "【非官方】" + headline
                label = headline if len(fresh) == 1 else f"{headline} 等 {len(fresh)} 条"
                fingerprint = json.dumps([(url, published) for url, _, _, _, published in fresh])
                ident = "live:" + hashlib.sha256(fingerprint.encode()).hexdigest()[:24]
                paragraphs = []
                for _, source, title, excerpt, published in fresh[:12]:
                    stamp = (datetime.fromtimestamp(published, EASTERN).strftime("%m-%d（美国东部日期）")
                             if source == DOL_BLS_SOURCE else datetime.fromtimestamp(published, ZONE).strftime("%m-%d %H:%M（北京时间）"))
                    paragraphs.append(f"**{title}**\n\n{source} · {stamp}" + (f"\n\n{excerpt}" if excerpt else ""))
                if len(fresh) > 12:
                    paragraphs.append(f"另有 {len(fresh) - 12} 条，见下方来源。")
                self.save_report(ident, "live", label, "\n\n".join(paragraphs),
                                 {"news_count": len(fresh), "media_count": sum(source.startswith(MEDIA_PREFIX) for _, source, _, _, _ in fresh),
                                  "links": [(f"【{source}】{title}" if source.startswith(MEDIA_PREFIX) else title, url)
                                            for url, source, title, _, _ in fresh]})
        except BaseException:
            self.db.execute("ROLLBACK TO ingest_bulletin_news")
            raise
        finally:
            self.db.execute("RELEASE ingest_bulletin_news")

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

    async def publish(self, kind, config, generation, *, event=None, source_warning="", event_media=()):
        now = datetime.now(ZONE)
        if event:
            label = event["title"]
            ident = "event:" + event["event"]
            start = datetime.fromtimestamp(event["due"] - 60, ZONE)
            end = now
        else:
            start, end = period_window(kind, now, config["time"])
            label = f"{start.date()} 至 {(end - timedelta(days=1)).date()}" if kind != "day" else str(start.date())
            ident = kind + ":" + str(start.date())
        news = [dict(row) for row in self.db.execute("""SELECT * FROM platform_bulletin_news
            WHERE published>=? AND published<? AND (? IS NULL OR source=?) ORDER BY published,url""",
            (start.timestamp(), end.timestamp(), event["source"] if event else None, event["source"] if event else None))]
        if event and event["source"] in ("美国劳工统计局", MEDIA_PREFIX + "Forex Factory"):
            date = datetime.fromtimestamp(event["due"], EASTERN).replace(hour=0, minute=0, second=0, microsecond=0)
            news.extend(dict(row) for row in self.db.execute(
                "SELECT * FROM platform_bulletin_news WHERE source=? AND published=? ORDER BY url",
                (DOL_BLS_SOURCE, date.timestamp())))
        known_urls = {item["url"] for item in news}
        news.extend(dict(url=url, source=source, title=title, excerpt=excerpt, published=published)
                    for url, source, title, excerpt, published in event_media
                    if url not in known_urls and start.timestamp() <= published <= end.timestamp())
        updates = []
        agenda = []
        update_links = []
        if kind == "day":
            hour, minute = map(int, config["time"].split(":"))
            cutoff = end.replace(hour=hour, minute=minute)
            previous = self.db.execute(
                "SELECT generated FROM platform_bulletins WHERE id=?", ("day:" + str(start.date() - timedelta(days=1)),),
            ).fetchone()
            updates_start = max((cutoff - timedelta(days=1)).timestamp(), previous[0] if previous else 0)
            updates = [dict(row) for row in self.db.execute(
                """SELECT id,kind,label,body,generated,details FROM platform_bulletins
                   WHERE kind IN ('live','event') AND generated BETWEEN ? AND ?
                   ORDER BY generated DESC,rowid DESC LIMIT 30""",
                (updates_start, now.timestamp()))]
            update_links = [link for item in updates for link in json.loads(item.pop("details")).get("links", [])]
            for item in updates:
                item["body"] = item["body"][:1400]
            today = now.replace(hour=0, minute=0, second=0, microsecond=0)
            agenda = [dict(row) for row in self.db.execute(
                "SELECT title,due,source FROM platform_bulletin_events WHERE active=1 AND due BETWEEN ? AND ? ORDER BY due LIMIT 20",
                (now.timestamp(), (today + timedelta(days=1)).timestamp() - 1))]
        if not (news or updates or agenda or event):
            return "暂无可核实素材，未发布空报"
        ledger = []
        if len(news) > 25:
            for index in range(0, len(news), 25):
                ledger.append((await self.ask(config, "将这一批来源压缩成简短事实清单，保留关键日期、来源分类、媒体域名和原文链接。",
                                              news[index:index + 25]))[:1500])
        payload = {"period": label, "type": KINDS[kind], "news": news if not ledger else ledger,
                   "items": len(news)}
        if kind == "day":
            payload.update(today_agenda=agenda, collected_updates=updates, source_status=source_warning)
        if event:
            payload["event"] = {"title": label, "due": event["due"], "task": event["task"], "source": event["source"]}
        body = await self.ask(config, "直接从收到的已核实事项写起，合并重复消息，把计划公布单列。"
            "没有结果的跟踪任务只能标为未核实。若有来源采集失败且影响覆盖，简短注明。"
            "每条二手报道使用【非官方】加媒体来源和原文链接。媒体报道中的指标数字只能归因转述。", payload)
        if generation != self.generation:
            return False
        links = {item["url"]: (f"【{item['source']}】{item['title']}" if item["source"].startswith(MEDIA_PREFIX)
                               else item["title"]) for item in news[-12:]}
        for title, url in update_links:
            links.setdefault(url, title)
        details = {"news_count": len(news), "media_count": sum(item["source"].startswith(MEDIA_PREFIX) for item in news),
                   "update_count": len(updates), "agenda_count": len(agenda),
                   "links": [(title, url) for url, title in list(links.items())[:20]]}
        self.save_report(ident, kind, label, body, details)
        return True

    def save_report(self, ident, kind, label, body, details):
        self.db.execute("INSERT OR REPLACE INTO platform_bulletins VALUES(?,?,?,?,?,?)",
                        (ident, kind, label, body[:7000], int(time.time()), json.dumps(details)))
        inbox = getattr(self.app, "agent_inbox", None)
        if inbox:
            inbox.market(ident, kind, f"{KINDS[kind]} · {label}")
        scheduler = getattr(self.app, "task_schedule", None)
        if scheduler:
            scheduler.event("bulletin", ident, None, {"id": ident, "kind": kind, "title": label})

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
                await self.track(action, self.publish_period(action, config, generation))
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

    async def publish_period(self, kind, config, generation):
        warning = ""
        if kind == "day":
            async def refresh_sources():
                nonlocal warning
                warning = await self.collect()
                return warning

            run = self.db.execute("SELECT attempted,error FROM platform_bulletin_runs WHERE kind='scan'").fetchone()
            if run and time.time() - run["attempted"] < 45:
                warning = run["error"] or ""
            elif not await self.track("scan", refresh_sources()):
                run = self.db.execute("SELECT error FROM platform_bulletin_runs WHERE kind='scan'").fetchone()
                warning = run["error"] or ""
        if generation != self.generation:
            return False
        note = await self.publish(kind, config, generation, source_warning=warning)
        if isinstance(note, str):
            return "；".join(filter(None, [note, warning]))
        return warning if note and warning else note

    async def track(self, name, job):
        now = int(time.time())
        self.db.execute("""INSERT INTO platform_bulletin_runs(kind,attempted) VALUES(?,?)
            ON CONFLICT(kind) DO UPDATE SET attempted=excluded.attempted""", (name, now))
        try:
            note = await job
        except Exception as error:
            log.warning("Market bulletin %s failed (%s)", name, type(error).__name__)
            self.db.execute("UPDATE platform_bulletin_runs SET error=? WHERE kind=?", (
                str(error) if isinstance(error, ValueError) and str(error).startswith("暂未同步")
                else "采集或模型调用失败，请检查数据源与模型配置", name))
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
                try:
                    await self.collect()
                except ValueError as error:
                    if not str(error).startswith("暂未同步"):
                        raise
                current = self.db.execute("SELECT title,due,active FROM platform_bulletin_events WHERE id=?",
                                          (event["event"],)).fetchone()
                rescheduled = not current or not current["active"] or current["due"] != event["due"]
                if rescheduled or generation != self.generation:
                    return False
                fresh = self.db.execute("""SELECT 1 FROM platform_bulletin_news WHERE
                    source=? AND published>=? AND published<=? LIMIT 1""",
                    (event["source"], event["due"] - 60, time.time())).fetchone()
                media, search_warning = await self.search_news(event["title"] + " release actual result")
                if generation != self.generation:
                    return False
                media = [item for item in media if event["due"] - 60 <= item[4] <= time.time()]
                self.ingest_news(media)
                if not fresh and event["attempts"] < 2:
                    return "尚未收到同机构的新公告，5 分钟后再次核查"
                published = await self.publish("event", config, generation, event=event, event_media=media)
                return search_warning if published and search_warning else published
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
        if now.strftime("%H:%M") < config["time"]:
            return
        for kind in config["periods"]:
            if generation != self.generation:
                return
            start, _ = period_window(kind, now, config["time"])
            saved = self.reports(1, report_id=kind + ":" + str(start.date()))
            empty_daily = kind == "day" and saved and not any(
                saved[0]["details"].get(key) for key in ("news_count", "update_count", "agenda_count"))
            if stamp - runs.get(kind, 0) >= 900 and (not saved or empty_daily):
                await self.track(kind, self.publish_period(kind, config, generation))

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
