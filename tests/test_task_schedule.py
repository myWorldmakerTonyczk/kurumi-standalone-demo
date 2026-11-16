import asyncio
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import AsyncMock

from aiohttp.test_utils import TestClient, TestServer

from app import Demo
from src.agent.task_schedule import ACCOUNT_EVENTS
from src.core.engine import dump
from src.users.ownership import issue_id


class TaskScheduleTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.app = Demo(Path(directory.name) / "schedule.sqlite3")
        self.addCleanup(self.app.db.close)
        self.addAsyncCleanup(self.app.timeline.close)
        self.owner = await self.app.accounts.create_web_user("schedule-agent", "temporary-password")
        token = issue_id(self.app.db, self.owner)
        self.game = self.app.engine.create({"name": "Schedule", "user_binding": {"type": "random", "values": [token]}})
        self.app._agent = AsyncMock(return_value="checked")

    async def drain(self):
        self.app.task_schedule.tick()
        await asyncio.gather(*self.app.timeline.workers.values())

    async def test_transaction_rollback_does_not_wake_agent(self):
        with self.assertRaises(RuntimeError):
            with self.app.engine.transaction():
                self.app.engine.log(self.game, "system", "margin_call", "test", {})
                raise RuntimeError("rollback")
        await self.drain()
        self.app._agent.assert_not_called()

    async def test_five_account_events_and_system_appointment_dedup(self):
        scheduler = self.app.task_schedule
        appointment = scheduler.schedule(self.owner, "watch", "check stop", {"type": "account", "event": "stop_loss"})
        for action, source, reason, detail in [
            ("close", "system", "forced", {"kind": "liquidation"}),
            ("close", "system", "触发止损", {}),
            ("close", "system", "触发止盈", {}),
            ("margin_call", "system", "low", {}),
            ("deposit", "human", "funds", {"amount": "100"}),
        ]:
            self.app.engine.log(self.game, source, action, reason, detail)
        await self.drain()
        self.assertEqual(self.app._agent.await_count, 5)
        self.assertTrue(all(call.kwargs["readonly"] for call in self.app._agent.await_args_list))
        task = self.app.db.execute("SELECT task FROM agent_task_appointments WHERE id=?", (appointment["appointment_id"],)).fetchone()[0]
        report = self.app.timeline.report(self.owner, task)
        self.assertTrue(report["details"]["trigger"]["system"])
        self.assertEqual(report["details"]["trigger"]["appointments"][0]["id"], appointment["appointment_id"])
        self.assertEqual(report["details"]["report"], "checked")
        self.assertNotIn("details", self.app.timeline.report(self.owner, task, summary_only=True))
        self.app.db.execute("UPDATE agent_task_events SET handled=0")
        await self.drain()
        self.assertEqual(self.app._agent.await_count, 5)

    async def test_disabled_automatic_event_keeps_appointment_and_quote_triggers_once(self):
        self.app.db.execute("INSERT INTO demo_profiles VALUES(?,?)",
                            (self.owner, dump({"account_wakeups": {"deposit": False}})))
        self.app.engine.log(self.game, "human", "deposit", "first", {})
        await self.drain()
        self.app._agent.assert_not_called()
        scheduler = self.app.task_schedule
        scheduler.schedule(self.owner, "watch", "second", {"type": "account", "event": "deposit"})
        self.app.engine.log(self.game, "human", "deposit", "second", {})
        await self.drain()
        self.app._agent.assert_awaited_once()
        scheduler.schedule(self.owner, "watch", "watch cross",
                           {"type": "quote", "pair": "EUR/USD", "side": "bid", "condition": "cross_up", "price": "1.1"})
        for price in ("1.09", "1.10", "1.08", "1.11"):
            self.app.engine.quotes["EUR/USD"] = {"bid": price, "timestamp": time.time(), "tradeable": True}
            await self.drain()
        self.assertEqual(self.app._agent.await_count, 2)

    async def test_default_switches_and_appointment_survives_restart(self):
        async with TestClient(TestServer(self.app.app(False))) as client:
            await client.post("/api/login",json={"username":"schedule-agent","password":"temporary-password"})
            self.assertEqual(await (await client.get("/api/account-wakeups")).json(),
                             dict.fromkeys(ACCOUNT_EVENTS, True))
        pending = self.app.task_schedule.schedule(self.owner, "watch", "after reboot", {"type": "immediate"})
        self.app.task_schedule.tick()
        await self.app.timeline.close()
        self.app.task_schedule.recover()
        await asyncio.gather(*self.app.timeline.workers.values())
        self.app._agent.assert_awaited_once()
        ident = self.app.db.execute("SELECT task FROM agent_task_appointments WHERE id=?", (pending["appointment_id"],)).fetchone()[0]
        self.assertEqual(self.app.timeline.report(self.owner, ident)["state"], "completed")
