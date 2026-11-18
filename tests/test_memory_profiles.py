import json
import sqlite3
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from src.agent.memory import Memory


class MemoryProfileTests(unittest.IsolatedAsyncioTestCase):
    async def test_backfill_and_evidence_scoped_updates(self):
        db = sqlite3.connect(":memory:", isolation_level=None)
        self.addCleanup(db.close)
        db.row_factory = sqlite3.Row
        db.executescript("""
          CREATE TABLE sim_users(id TEXT PRIMARY KEY);
          INSERT INTO sim_users VALUES('owner'), ('other');
          CREATE TABLE games(id TEXT PRIMARY KEY,data TEXT);
          INSERT INTO games VALUES('game','{"owner_id":"owner"}');
          CREATE TABLE demo_chat(id INTEGER PRIMARY KEY, game TEXT, role TEXT, content TEXT, created REAL);
          INSERT INTO demo_chat VALUES
            (1,'game','user','Review before opening',1700043200), (2,'game','assistant','OK',1700043201),
            (3,'game','user','Review before opening',1700648000), (4,'game','assistant','OK',1700648001),
            (5,'game','user','Review before opening',1701252800), (6,'game','assistant','OK',1701252801);
        """)
        memory = Memory(db)
        memory.include_private_history("owner", "game")
        db.execute("UPDATE agent_memory_events SET done=1,text='',reply=''")
        profiles = memory.profiles
        scope = "private:game"
        action = {"dimension": "habit", "text": "Often reviews before opening", "basis": "observed",
                  "events": [1, 2, 3], "replace": []}
        with patch("src.agent.memory_profiles.model_client") as client:
            invoke = client.return_value.ainvoke = AsyncMock(return_value=SimpleNamespace(text=json.dumps([
                action | {"text": "Too little evidence", "events": [1, 1, 2]}, action,
            ])))
            await profiles.consolidate("owner", scope, {}, None)
            await profiles.consolidate("owner", scope, {}, None)
            invoke.assert_awaited_once()
        self.assertEqual([item["text"] for item in profiles.list("owner")], [action["text"]])
        record = profiles.list("owner")[0]
        self.assertEqual(len({item["date"] for item in record["evidence"]}), 3)
        self.assertEqual(profiles.context("owner", "group:game", ["self"]), [])
        self.assertEqual(profiles.context("other", scope, ["self"]), [])

        proof = profiles.sources("owner", scope, 3)
        correction = action | {"basis": "declared", "events": [3], "text": "Voluntarily declared a preference",
                               "replace": [record["id"]]}
        profiles.apply("owner", scope, correction, proof, {record["id"]: record})
        current = profiles.context("owner", scope, ["self"])
        self.assertEqual([item["text"] for item in current], [correction["text"]])
        self.assertEqual(current[0]["basis"], "declared")
        self.assertEqual(current[0]["last_evidence_date"], proof[3]["date"])
        self.assertTrue(current[0]["updated_at"].endswith("+08:00"))

    def test_automatic_recall_keeps_creation_time_after_reinforcement(self):
        db = sqlite3.connect(":memory:")
        self.addCleanup(db.close)
        db.row_factory = sqlite3.Row
        db.executescript("CREATE TABLE sim_users(id TEXT PRIMARY KEY); INSERT INTO sim_users VALUES('owner');")
        memory = Memory(db)
        db.execute("INSERT INTO agent_memory(owner,text,created,updated,kind) VALUES(?,?,?,?,?)",
                   ("owner", "Previous EURUSD plan", 1700043200, 1700648000, "event"))
        db.execute("INSERT INTO agent_memory(owner,text,created,updated,kind) VALUES(?,?,?,?,?)",
                   ("owner", "Legacy EURUSD note", 0, 1700648000, "knowledge"))
        with patch("src.agent.memory.time.time", return_value=1701252800):
            entries = {item["text"]: item for item in memory.recall("owner", "EURUSD")}
        self.assertEqual(entries["Previous EURUSD plan"]["created_at"], "2023-11-15T18:13:20+08:00")
        self.assertNotIn("created_at", entries["Legacy EURUSD note"])
        self.assertTrue(entries["Legacy EURUSD note"]["updated_at"].endswith("+08:00"))


    async def test_failed_model_call_preserves_unprocessed_profile_evidence(self):
        db = sqlite3.connect(":memory:", isolation_level=None)
        self.addCleanup(db.close)
        db.row_factory = sqlite3.Row
        db.executescript("""
          CREATE TABLE sim_users(id TEXT PRIMARY KEY);
          INSERT INTO sim_users VALUES('owner');
          CREATE TABLE games(id TEXT PRIMARY KEY,data TEXT);
          INSERT INTO games VALUES('game','{"owner_id":"owner"}');
          CREATE TABLE demo_chat(id INTEGER PRIMARY KEY, game TEXT, role TEXT, content TEXT, created REAL);
          INSERT INTO demo_chat VALUES
            (1,'game','user','Explicit requirement',1700043200), (2,'game','assistant','OK',1700043201);
        """)
        memory = Memory(db)
        memory.include_private_history("owner", "game")
        db.execute("UPDATE agent_memory_events SET done=1")
        with patch("src.agent.memory_profiles.model_client") as client:
            client.return_value.ainvoke = AsyncMock(side_effect=RuntimeError("unavailable"))
            with self.assertRaises(RuntimeError):
                await memory.profiles.consolidate("owner", "private:game", {}, None)
        self.assertEqual(memory.profiles.pending("owner", "private:game"), 1)
        self.assertEqual(memory.profiles.list("owner"), [])
