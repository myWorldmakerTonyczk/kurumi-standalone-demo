import json
import sqlite3
import time
import unittest
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

from src.agent.market_bulletin import MarketBulletin, ZONE, parse_calendar, parse_rss, period_window


from src.agent.tool.bulletins import build_tool


class MarketBulletinTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        db = sqlite3.connect(":memory:", isolation_level=None)
        self.addCleanup(db.close)
        db.row_factory = sqlite3.Row
        app = SimpleNamespace(db=db, model_url=lambda value: value, engine=SimpleNamespace(quotes={}), demo=False)
        self.agent = MarketBulletin(app)

    def test_frequency_windows_and_public_credential_isolation(self):
        now = datetime(2026, 11, 2, 8, tzinfo=ZONE)
        for kind, start, end in [
            ("day", "2026-10-31", "2026-11-01"), ("week", "2026-10-19", "2026-10-26"),
            ("month", "2026-10-01", "2026-11-01"),
        ]:
            actual = period_window(kind, now, "08:30")
            self.assertEqual(tuple(item.date().isoformat() for item in actual), (start, end))
        self.assertTrue(self.agent.public()["enabled"])
        self.assertFalse(self.agent.public()["model_ready"])
        self.agent.save({"enabled": False})
        self.assertEqual(self.agent.public(), {"enabled": False})
        self.agent.save({"enabled": True, "url": "https://test.example/v1", "model": "model", "key": "example-key"})
        self.agent.save({"interval": 120, "key": ""})
        self.assertEqual(self.agent.config()["key"], "example-key")
        self.assertNotIn("example-key", json.dumps(self.agent.admin()))
        self.assertNotIn("example-key", json.dumps(self.agent.public()))
        self.agent.save({"url": "https://other.example/v1"})
        self.assertFalse(self.agent.admin()["config"]["has_key"])

    async def test_agent_reads_only_published_bulletins(self):
        self.agent.save({"enabled": True, "key": "private-admin-key"})
        self.agent.db.executemany("INSERT INTO platform_bulletins VALUES(?,?,?,?,?,?)", [
            ("month:2026-10-01", "month", "October", "**Monthly**", 100,
             json.dumps({"links": [["Source", "https://example.gov/release"]]})),
            ("event:cpi", "event", "CPI", "Release follow-up", 200, "{}"),
        ])
        read = build_tool(SimpleNamespace(market_bulletin=self.agent))
        catalog = json.loads(await read(include_body=False))
        self.assertEqual([row["kind"] for row in catalog["reports"]], ["event", "month"])
        self.assertNotIn("body", catalog["reports"][0])
        month = json.loads(await read(kind="month"))["reports"][0]
        self.assertEqual(month["body"], "**Monthly**")
        self.assertEqual(month["sources"][0]["url"], "https://example.gov/release")
        self.assertEqual(json.loads(await read(report_id=month["report_id"]))["reports"], [month])
        self.assertNotIn("private-admin-key", json.dumps(catalog))
        self.assertFalse(json.loads(await read(kind="invalid"))["ok"])
        self.agent.save({"enabled": False})
        self.assertEqual(json.loads(await read()), {"enabled": False, "reports": []})

    async def test_default_collects_without_calling_model(self):
        self.agent.collect = AsyncMock(return_value="")
        self.agent.ask = AsyncMock()
        await self.agent.run("tick")
        self.agent.collect.assert_awaited_once()
        self.agent.ask.assert_not_awaited()
        with self.assertRaisesRegex(ValueError, "配置管理员播报模型"):
            await self.agent.run("day")

    def test_official_formats_and_timezone(self):
        rss = """<rss><channel><item><title>Indicator</title><link>https://example.gov/release</link>
          <pubDate>Tue, 10 Nov 2026 13:30:00 GMT</pubDate><description>Summary</description></item></channel></rss>"""
        stamp = list(parse_rss(rss, "agency"))[0][-1]
        calendar = """BEGIN:VEVENT
UID:indicator
DTSTART;TZID=America/New_York:20261110T083000
SUMMARY:Consumer Price
 Index
END:VEVENT"""
        _, title, due = list(parse_calendar(calendar))[0]
        self.assertEqual(title, "Consumer PriceIndex")
        self.assertEqual(due, stamp)

    async def test_agent_cannot_invent_event_and_due_task_publishes_once(self):
        agent = self.agent
        db = agent.db
        agent.save({"enabled": True, "url": "https://test.example/v1", "model": "model",
                    "key": "example-key", "periods": []})
        generation = agent.generation
        db.execute("INSERT INTO platform_bulletin_events(id,title,due) VALUES('cpi','CPI',?)", (int(time.time()) + 30,))
        agent.ask = AsyncMock(return_value=json.dumps([
            {"event": "invented", "task": "invalid"}, {"event": "cpi", "task": "Verify the official release"},
        ]))
        await agent.plan(agent.config(), generation)
        self.assertEqual([item["event"] for item in agent.admin()["tasks"]], ["cpi"])
        db.execute("UPDATE platform_bulletin_events SET due=? WHERE id='cpi'", (int(time.time()) - 60,))
        db.execute("INSERT INTO platform_bulletin_runs(kind,attempted) VALUES('scan',?)", (int(time.time()),))
        agent.collect = AsyncMock(return_value="")
        agent.ask = AsyncMock(return_value="Official release is available.")
        await agent.run("tick")
        agent.ask.assert_not_awaited()
        self.assertEqual(agent.admin()["tasks"][0]["state"], "pending")
        db.execute("UPDATE platform_bulletin_wakeups SET retry_at=0")
        db.execute("INSERT INTO platform_bulletin_news VALUES(?,?,?,?,?)",
                   ("https://example.gov/official", "美国劳工统计局", "CPI", "Release", time.time() - 10))
        await agent.run("tick")
        await agent.run("tick")
        agent.ask.assert_awaited_once()
        self.assertEqual(agent.admin()["tasks"][0]["state"], "done")
        self.assertEqual(agent.public()["reports"][0]["kind"], "event")
        agent.save({"enabled": False})
        self.assertEqual(len(agent.admin()["reports"]), 1)
