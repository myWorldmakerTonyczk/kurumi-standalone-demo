import json
import os
import sqlite3
import time
import unittest
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from langchain_core.messages import AIMessage

from src.agent.market_bulletin import BEA_CALENDAR_JSON, DOL_BLS_RELEASES, DOL_BLS_SOURCE, FOREX_FACTORY_CALENDAR, LEGACY_BLS_SOURCES, MarketBulletin, ZONE, parse_calendar, parse_rss, parse_source, period_window


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

    async def test_admin_trace_is_opt_in_and_redacts_configured_keys(self):
        self.agent.save({"url": "https://test.example/v1", "model": "test", "key": "private-admin-key"})

        async def job():
            config = self.agent.config()
            await self.agent.ask(config, "Summarize public material", {"value": "private-admin-key"})
            self.agent.save_report("day:test", "day", "Daily report", "body", {})
            return True

        response = AIMessage(content="report", additional_kwargs={"reasoning_content": "provider rationale"},
                             usage_metadata={"input_tokens": 10, "output_tokens": 4, "total_tokens": 14})
        with patch("src.agent.market_bulletin.model_client", return_value=SimpleNamespace(
                ainvoke=AsyncMock(return_value=response))):
            self.assertTrue(await self.agent.track("day", job()))
            self.assertEqual(self.agent.admin()["traces"], [])
            self.agent.save({"trace_enabled": True})
            self.assertTrue(await self.agent.track("day", job()))

        entry = self.agent.admin()["traces"][0]
        self.assertEqual(entry["state"], "ok")
        model, published = self.agent.trace_detail(entry["id"])
        self.assertEqual(model["response"]["reasoning_content"], "provider rationale")
        self.assertEqual(model["response"]["usage_metadata"]["total_tokens"], 14)
        self.assertEqual(published["report_id"], "day:test")
        self.assertNotIn("private-admin-key", json.dumps(model))
        self.assertNotIn("traces", self.agent.public())

        async def broken():
            raise ValueError("bad result")

        self.assertFalse(await self.agent.track("scan", broken()))
        failed = self.agent.admin()["traces"][0]
        self.assertEqual(failed["state"], "failed")
        self.assertEqual(self.agent.trace_detail(failed["id"])[0]["message"], "bad result")

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

    async def test_daily_refresh_includes_new_live_and_indicator_updates(self):
        agent = self.agent
        agent.save({"url": "https://test.example/v1", "model": "model", "key": "test-key"})
        now = time.time()
        agent.save_report("event:jobs", "event", "就业指标", "官方跟踪结果", {"news_count": 1, "links": []})
        item = ("https://example.gov/fresh", "机构", "新公告", "新数据摘要", now - 60)
        async def collect():
            agent.ingest_news([item])
            return "暂未同步 BLS（HTTP 403）"
        agent.collect = AsyncMock(side_effect=collect)
        agent.ask = AsyncMock(return_value="日报正文")
        await agent.run("day")
        agent.collect.assert_awaited_once()
        payload = agent.ask.await_args.args[2]
        self.assertEqual({report["kind"] for report in payload["collected_updates"]}, {"live", "event"})
        self.assertIn("HTTP 403", payload["source_status"])
        daily = agent.reports(1, kind="day")[0]
        self.assertEqual(daily["details"]["update_count"], 2)
        self.assertIn(["新公告", "https://example.gov/fresh"], daily["details"]["links"])
        agent.ingest_news([item])
        self.assertEqual(len(agent.reports(10, kind="live")), 1)
        read = build_tool(SimpleNamespace(market_bulletin=agent))
        self.assertEqual(json.loads(await read(kind="live"))["reports"][0]["kind"], "live")

    async def test_no_material_does_not_publish_an_empty_daily_report(self):
        agent = self.agent
        agent.save({"url": "https://test.example/v1", "model": "model", "key": "test-key"})
        agent.collect = AsyncMock(return_value="")
        agent.ask = AsyncMock()
        await agent.run("day")
        self.assertFalse(agent.reports(1, kind="day"))
        agent.ask.assert_not_awaited()
        self.assertIn("未发布空报", next(row for row in agent.admin()["runs"] if row["kind"] == "day")["error"])

    async def test_media_reports_are_labeled_and_keep_the_original_link(self):
        agent = self.agent
        stamp = time.time() - 100
        url = "https://media.example/economy"
        media = (url, "非官方 · media.example", "Economy update", "Media summary", stamp)
        agent.ingest_news([media])
        agent.ingest_news([(url, media[1], media[2], media[3], stamp + 60)])
        reports = agent.reports(10, kind="live")
        self.assertEqual(len(reports), 1)
        report = reports[0]
        self.assertIn("非官方", report["label"])
        self.assertIn("非官方 · media.example", report["body"])
        self.assertEqual(report["details"]["links"], [["【非官方 · media.example】Economy update", url]])
        self.assertEqual(report["details"]["media_count"], 1)

    def test_bls_defaults_migrate_only_legacy_addresses(self):
        agent = self.agent
        self.assertEqual(agent.config()["sources"]["bls"], DOL_BLS_RELEASES)
        self.assertEqual(agent.config()["sources"]["bls_calendar"], FOREX_FACTORY_CALENDAR)
        agent.save({"sources": LEGACY_BLS_SOURCES | {"fed": "https://example.org/fed.rss"}})
        agent.db.execute("INSERT INTO platform_bulletin_events(id,title,due) VALUES('old','Old schedule',?)",
                         (int(time.time()) + 3600,))
        agent.db.execute("INSERT INTO platform_bulletin_runs(kind,attempted) VALUES('scan',1)")
        restarted = MarketBulletin(agent.app)
        self.assertEqual(restarted.config()["sources"]["bls"], DOL_BLS_RELEASES)
        self.assertEqual(restarted.config()["sources"]["bls_calendar"], FOREX_FACTORY_CALENDAR)
        self.assertEqual(restarted.config()["sources"]["fed"], "https://example.org/fed.rss")
        self.assertEqual(agent.db.execute("SELECT active FROM platform_bulletin_events").fetchone()[0], 0)
        self.assertEqual(agent.db.execute("SELECT attempted FROM platform_bulletin_runs").fetchone()[0], 0)
        restarted.save({"sources": {"bls": "https://example.org/custom.rss"}})
        self.assertEqual(MarketBulletin(agent.app).config()["sources"]["bls"], "https://example.org/custom.rss")

    async def test_configurable_sources_and_check_without_importing(self):
        agent = self.agent
        default = agent.config()["sources"]["bls"]
        media = "https://media.example/rss"
        agent.save({"sources": {"bls": "https://blsmon1.bls.gov/feed/bls_latest.rss", "media": media}})
        self.assertEqual(agent.config()["sources"]["media"], media)
        self.assertEqual(agent.config()["sources"]["bls"], "https://blsmon1.bls.gov/feed/bls_latest.rss")
        for bad in ("http://media.example/rss", "https://127.0.0.1/rss", "https://127.1/rss"):
            with self.assertRaisesRegex(ValueError, "公网 HTTPS"):
                await agent.check_source("media", bad)
        agent.fetch = AsyncMock(return_value="""<rss><channel><item><title>Indicator</title>
            <link>https://example.gov/release</link><pubDate>Tue, 10 Nov 2026 13:30:00 GMT</pubDate>
            </item></channel></rss>""")
        self.assertEqual(await agent.check_source("media", media), {"ok": True, "message": "连接成功，解析到 1 条"})
        self.assertFalse((await agent.check_source("bls_calendar", ""))["ok"])
        self.assertFalse(agent.reports(10))
        agent.save({"sources": {"bls": "", "media": ""}})
        self.assertEqual(agent.config()["sources"]["bls"], default)
        self.assertEqual(agent.config()["sources"]["media"], "")

    def test_bea_json_calendar_ignores_metadata_and_duplicate_dates(self):
        when = "2026-11-25T13:30:00+00:00"
        payload = json.dumps({"GDP": {"release_dates": [when, when, "bad date"]}, "file_last_updated": "2026-11-16"})
        first, = parse_source(payload, "BEA", True, BEA_CALENDAR_JSON)
        self.assertEqual(first[1:], ("GDP", int(datetime.fromisoformat(when).timestamp())))

    def test_third_party_weekly_calendar_selects_us_events(self):
        calendar = """BEGIN:VCALENDAR
BEGIN:VEVENT
UID:us
SUMMARY:⁂ US Non-Farm Employment Change
DTSTART;TZID=America/New_York:20261106T083000
END:VEVENT
BEGIN:VEVENT
UID:ca
SUMMARY:⁂ CA Employment Change
DTSTART;TZID=America/New_York:20261106T083000
END:VEVENT
END:VCALENDAR"""
        event, = parse_source(calendar, "Forex Factory", True, FOREX_FACTORY_CALENDAR)
        self.assertEqual(event[1], "Non-Farm Employment Change")
        self.assertEqual(datetime.fromtimestamp(event[2], ZONE).hour, 21)

    def test_department_of_labor_reports_keep_official_pdf_links(self):
        html = '''
          <a href="/newsroom/economicdata/cpi_11102026.pdf"><b>Consumer Price Index</b></a>
          <a href="https://example.com/newsroom/economicdata/cpi_11102026.pdf">Ignored</a>
          <a href="/newsroom/economicdata/cpi_99999999.pdf">Invalid date</a>
        '''
        report, = parse_source(html, DOL_BLS_SOURCE, False, DOL_BLS_RELEASES)
        self.assertEqual(report[:3], ("https://www.dol.gov/newsroom/economicdata/cpi_11102026.pdf",
                                     DOL_BLS_SOURCE, "Consumer Price Index"))

    async def test_optional_search_uses_deepseek_key_and_keeps_credentials_private(self):
        agent = self.agent
        with patch.dict(os.environ, {"BRAVE_SEARCH_API_KEY": ""}):
            self.assertEqual(agent.config()["search_provider"], "off")
            self.assertEqual(await agent.search_news("test"), ([], ""))
            agent.save({"search_provider": "auto", "url": "https://api.deepseek.com/v1", "key": "test-model-secret",
                        "brave_search_key": "test-brave-secret"})
            now = datetime.fromtimestamp(time.time() - 5, ZONE).isoformat()
            response = MagicMock(status=200)
            response.json = AsyncMock(return_value={"results": [
                {"title": "Release", "url": "https://media.example/report", "date": now, "snippet": "Summary"},
                {"title": "Undated", "url": "https://media.example/undated", "date": None},
            ]})
            session = MagicMock()
            session.request.return_value.__aenter__.return_value = response
            client = MagicMock()
            client.__aenter__.return_value = session
            with patch("src.agent.market_bulletin.ClientSession", return_value=client):
                items, warning = await agent.search_news("forex update")
            self.assertFalse(warning)
            self.assertEqual([item[:4] for item in items],
                             [("https://media.example/report", "非官方 · media.example", "Release", "Summary")])
            self.assertEqual(session.request.call_args.args, ("POST", "https://api.deepseek.com/v1/tools/search"))
            self.assertEqual(session.request.call_args.kwargs["headers"], {"Authorization": "Bearer test-model-secret"})
            self.assertNotIn("-secret", json.dumps(agent.admin()))
            self.assertNotIn("-secret", json.dumps(agent.public()))
            agent.save({"url": "https://proxy.example/v1", "key": "proxy-key"})
            self.assertEqual(agent.search_credentials(agent.config()), ("brave", "test-brave-secret"))
            agent.save({"search_provider": "deepseek", "deepseek_search_key": "test-search-secret"})
            self.assertEqual(agent.admin()["search"]["active"], "deepseek")
            agent.save({"search_provider": "off"})
            self.assertEqual(await agent.search_news("test"), ([], ""))

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
