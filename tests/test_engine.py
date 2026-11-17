import tempfile
import uuid
import time
import unittest
from pathlib import Path
from decimal import Decimal

from src.core.engine import USD_MIGRATION_RATE, Engine, number


class LedgerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "test.sqlite3"
        self.e = Engine(self.path)
        self.quote("EUR/USD", "1.1000", "1.1002")
        self.quote("USD/JPY", "150", "150.02")
        self.g = self.e.create(
            {"pairs": ["EUR/USD", "USD/JPY"], "fee_rate": "0.00002", "leverage": 10}
        )
        self.gid = self.g["id"]

    def tearDown(self):
        self.e.db.close()
        self.tmp.cleanup()

    def quote(self, pair, bid, ask, age=0):
        self.e.quotes[pair] = dict(
            bid=bid, ask=ask, timestamp=time.time() - age, tradeable=True
        )

    def buy(self, **kwargs):
        p = dict(pair="EUR/USD", side="buy", units="1000", reason="测试交易")
        p.update(kwargs)
        return self.e.action(self.gid, "open", p)

    def test_limit_orders_fill_at_favorable_quotes_only_once(self):
        order = dict(pair="EUR/USD", units="1000", reason="limit", side="buy", limit_price="1.1001")
        placed = self.e.action(self.gid, "place_limit_order", order, key="same")
        self.assertEqual(placed, self.e.action(self.gid, "place_limit_order", order, key="same"))
        self.e.action(self.gid, "place_limit_order", order | {"side": "sell", "limit_price": "1.1003"})
        self.e.tick(self.gid)
        self.assertFalse(self.e.get(self.gid)["positions"])
        self.quote("EUR/USD", "1.0998", "1.0999", age=40)
        self.e.tick(self.gid)
        self.assertEqual(len(self.e.get(self.gid)["orders"]), 2)
        self.quote("EUR/USD", "1.0998", "1.0999")
        self.e.tick(self.gid)
        self.e.tick(self.gid)
        self.assertEqual([p["entry"] for p in self.e.get(self.gid)["positions"]], ["1.0999"])
        self.quote("EUR/USD", "1.1004", "1.1005")
        self.e.tick(self.gid)
        game = self.e.get(self.gid)
        self.assertEqual([p["entry"] for p in game["positions"]], ["1.0999", "1.1004"])
        self.assertEqual(game["orders"], [])

    def test_limits_respect_pause_cancel_and_insufficient_margin(self):
        order = dict(pair="EUR/USD", units="1000", reason="limit", side="buy", limit_price="1.1002")
        ai = self.e.action(self.gid, "place_limit_order", order, "ai")
        self.e.action(self.gid, "pause", {})
        self.e.action(self.gid, "place_limit_order", order)
        self.e.action(self.gid, "place_limit_order", order | {"units": "100000000"})
        self.e.tick(self.gid)
        game = self.e.get(self.gid)
        self.assertEqual(len(game["positions"]), 1)
        self.assertEqual([item["id"] for item in game["orders"]], [ai["order_id"]])
        rejected = self.e.logs(self.gid)[0]
        self.assertEqual(rejected["status"], "rejected")
        self.e.action(self.gid, "cancel_limit_order", {"order_id": ai["order_id"]})
        self.e.action(self.gid, "resume", {})
        self.e.tick(self.gid)
        self.assertEqual(len(self.e.get(self.gid)["positions"]), 1)

    def test_same_pair_multi_positions_keep_independent_ids(self):
        self.buy(units="1000", sl="1.09")
        self.buy(units="2000", tp="1.12")
        self.buy(units="500", side="sell")
        positions = self.e.get(self.gid)["positions"]
        self.assertEqual(len(positions), 3)
        self.assertEqual(len({p["id"] for p in positions}), 3)
        value = self.e.valuation(self.e.get(self.gid))
        self.assertEqual(len(value["positions"]), 3)
        self.e.action(self.gid, "close", {"position": positions[1]["id"]})
        remaining = self.e.get(self.gid)["positions"]
        self.assertEqual(
            {p["id"] for p in remaining}, {positions[0]["id"], positions[2]["id"]}
        )

    def test_multi_position_stop_only_closes_matching_position(self):
        self.buy(sl="1.09")
        self.buy(sl="1.08")
        self.quote("EUR/USD", "1.0850", "1.0852")
        self.e.tick(self.gid)
        positions = self.e.get(self.gid)["positions"]
        self.assertEqual(len(positions), 1)
        self.assertEqual(positions[0]["sl"], "1.08")

    def test_failed_extra_position_not_counted_or_charged(self):
        self.buy()
        before = self.e.get(self.gid)
        with self.assertRaises(ValueError):
            self.buy(units="100000000")
        self.assertEqual(self.e.get(self.gid), before)
        self.assertEqual(self.e.logs(self.gid)[0]["status"], "rejected")

    def test_open_spread_fee_and_margin(self):
        self.buy()
        g = self.e.get(self.gid)
        v = self.e.valuation(g)
        self.assertEqual(number(g["fees"]), Decimal("0.022004") * Decimal("150.01"))
        self.assertEqual(number(v["floating"]), Decimal("-0.2") * Decimal("150.01"))
        self.assertEqual(number(v["margin"]), Decimal("110") * Decimal("150.01"))

    def test_roundtrip_fees(self):
        self.buy()
        p = self.e.get(self.gid)["positions"][0]
        self.quote("EUR/USD", "1.1100", "1.1102")
        self.e.action(self.gid, "close", {"position": p["id"]})
        self.assertEqual(
            number(self.e.get(self.gid)["balance"]), Decimal("200000") + Decimal("9.755796") * Decimal("150.01")
        )

    def test_short_profit(self):
        self.buy(side="sell")
        self.quote("EUR/USD", "1.09", "1.0902")
        self.assertEqual(
            number(self.e.valuation(self.e.get(self.gid))["floating"]), Decimal("9.8") * Decimal("150.01")
        )

    def test_jpy_conversion(self):
        self.buy(pair="USD/JPY", units="1000")
        self.quote("USD/JPY", "151", "151.02")
        value = number(self.e.valuation(self.e.get(self.gid))["floating"])
        self.assertEqual(value, 980)

    def test_deposit_not_profit(self):
        self.e.action(self.gid, "deposit", {"amount": "5000"})
        v = self.e.valuation(self.e.get(self.gid))
        self.assertEqual(number(v["profit"]), 0)
        self.assertEqual(number(v["return_rate"]), 0)

    def test_deposit_preserves_return_and_drawdown(self):
        self.buy()
        self.quote("EUR/USD", "1.09", "1.0902")
        self.e.tick(self.gid)
        old = self.e.get(self.gid)
        self.e.action(self.gid, "deposit", {"amount": "5000"})
        new = self.e.get(self.gid)
        self.assertEqual(new["index"], old["index"])
        self.assertEqual(new["drawdown"], old["drawdown"])

    def test_idempotency(self):
        for _ in range(2):
            self.e.action(self.gid, "deposit", {"amount": "5000"}, key="same")
        self.assertEqual(number(self.e.get(self.gid)["balance"]), 205000)

    def test_insufficient_margin_atomic_and_logged(self):
        with self.assertRaises(ValueError):
            self.buy(units="10000000")
        g = self.e.get(self.gid)
        self.assertEqual(g["balance"], "200000")
        self.assertEqual(g["positions"], [])
        self.assertEqual(self.e.logs(self.gid)[0]["status"], "rejected")

    def test_stale_quote_refused(self):
        self.quote("EUR/USD", "1.1", "1.1002", 60)
        with self.assertRaises(ValueError):
            self.buy()

    def test_nontradeable_quote(self):
        self.e.quotes["EUR/USD"]["tradeable"] = False
        with self.assertRaises(ValueError):
            self.buy()

    def test_ai_cannot_deposit(self):
        with self.assertRaises(ValueError):
            self.e.action(
                self.gid, "deposit", {"amount": "1000", "reason": "救场"}, "ai"
            )

    def test_ai_reason_required(self):
        with self.assertRaises(ValueError):
            self.e.action(self.gid, "observe", {}, "ai")

    def test_paused_allows_human_but_not_ai(self):
        self.e.action(self.gid, "pause", {})
        with self.assertRaises(ValueError):
            self.e.action(self.gid, "observe", {"reason": "test"}, "ai")
        self.buy()

    def test_stop_loss_gap_at_available_price(self):
        self.buy(sl="1.09")
        self.quote("EUR/USD", "1.08", "1.0802")
        self.e.tick(self.gid)
        self.assertEqual(self.e.get(self.gid)["positions"], [])
        log = next(
            entry for entry in self.e.logs(self.gid) if entry["action"] == "close"
        )
        self.assertEqual(log["reason"], "触发止损")
        self.assertEqual(log["detail"]["position"]["current"], "1.08")

    def test_failure_wins_simultaneous_conditions(self):
        g = self.e.get(self.gid)
        g["rules"] = {"profit": "0.01", "floor": "220000"}
        self.e.save(g)
        self.buy()
        self.quote("EUR/USD", "1.2", "1.2002")
        self.e.tick(self.gid)
        self.assertEqual(self.e.get(self.gid)["outcome"], "失败")

    def test_ended_cannot_recharge(self):
        g = self.e.get(self.gid)
        g["rules"] = {"floor": "210000"}
        self.e.save(g)
        self.e.tick(self.gid)
        with self.assertRaises(ValueError):
            self.e.action(self.gid, "deposit", {"amount": "10000"})

    def test_unauthorized_pair(self):
        with self.assertRaises(ValueError):
            self.buy(pair="AUD/USD")

    def test_invalid_stop(self):
        with self.assertRaises(ValueError):
            self.buy(sl="2")

    def test_invalid_numeric(self):
        for x in ["NaN", "Infinity", "-Infinity", "hello", 1e13]:
            with self.assertRaises(ValueError):
                number(x)

    def test_restart_persists_ledger(self):
        self.buy()
        self.e.db.close()
        self.e = Engine(self.path)
        self.assertEqual(len(self.e.get(self.gid)["positions"]), 1)
        self.assertEqual(self.e.logs(self.gid)[0]["action"], "open")

    def test_low_margin_forces_close(self):
        self.buy(units="11000")
        self.quote("EUR/USD", "1.02", "1.0202")
        self.e.tick(self.gid)
        self.assertEqual(self.e.get(self.gid)["positions"], [])
        self.assertTrue(
            any("强平" in entry["reason"] for entry in self.e.logs(self.gid))
        )

    def test_legacy_migration_scales_money_once_and_keeps_quote_prices(self):
        game = self.e.get(self.gid)
        game.pop("currency")
        game.update(balance="1000", initial="1000", invested="1000", last_equity="1000",
                    rules={"floor": "100", "profit": "10"})
        self.e.save(game)
        self.e.log(game, "human", "close", "旧记录", {"fee": "1", "position": {"entry": "1.1", "pnl": "5"}})
        self.e.db.execute("UPDATE replay_equity SET equity=1000 WHERE game=?", (self.gid,))
        self.e.db.close()
        self.e = Engine(self.path)
        migrated = self.e.get(self.gid)
        self.assertEqual(migrated["currency"], "JPY")
        self.assertEqual(number(migrated["balance"]), 1000 * USD_MIGRATION_RATE)
        self.assertEqual(number(migrated["rules"]["floor"]), 100 * USD_MIGRATION_RATE)
        self.assertEqual(migrated["rules"]["profit"], "10")
        log = self.e.logs(self.gid)[0]["detail"]
        self.assertEqual(log["position"]["entry"], "1.1")
        self.assertEqual(number(log["position"]["pnl"]), 5 * USD_MIGRATION_RATE)
        self.assertEqual(self.e.db.execute("SELECT equity FROM replay_equity WHERE game=?", (self.gid,)).fetchone()[0], 159525)
        self.e.migrate_account_currency()
        self.assertEqual(self.e.get(self.gid), migrated)

    def test_platform_grant_once_per_user_and_pending_onboarding(self):
        active = self.e.create_virtual_user("现有用户")
        waiting = self.e.create_virtual_user("待开户")
        binding = {"type": "random", "values": [active["public_id"]]}
        first = self.e.create({"user_binding": binding, "allow_deposit": False})
        second = self.e.create({"user_binding": binding})
        self.e.quotes.clear()
        key = uuid.uuid4().hex
        for _ in range(2):
            self.assertEqual(self.e.grant_every_user("5000", key), {"credited": 1, "pending": 1})
        self.assertEqual(number(self.e.get(first["id"])["balance"]), 205000)
        self.assertEqual(number(self.e.get(second["id"])["balance"]), 200000)
        with self.assertRaises(ValueError):
            self.e.grant_every_user("8000", key)
        opened = self.e.create({"user_binding": {"type": "random", "values": [waiting["public_id"]]}})
        self.assertEqual(number(opened["balance"]), 205000)
        self.assertEqual(number(opened["invested"]), 205000)
        self.assertEqual(opened["index"], "1")
        self.assertEqual(self.e.logs(opened["id"])[0]["action"], "deposit")


if __name__ == "__main__":
    unittest.main()
