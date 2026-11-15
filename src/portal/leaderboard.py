"""Community rankings based on the simulated ledger."""
import json
from collections import defaultdict
from datetime import datetime, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

from src.core.engine import number

ZONE = ZoneInfo("Asia/Shanghai")
BOARDS = {"wealth", "weekly", "return"}
ZERO = Decimal(0)
CENT = Decimal("0.01")


def standings(app, viewer, board, *, now=None):
    if board not in BOARDS:
        raise ValueError("未知财富榜类型")
    now = datetime.now(ZONE) if now is None else now.astimezone(ZONE)
    week = (now - timedelta(days=now.weekday())).replace(hour=0, minute=0, second=0, microsecond=0)
    owners = {row[0] for row in app.db.execute("SELECT owner FROM demo_profiles JOIN sim_users ON sim_users.id=owner")}
    totals = {}
    games = {}
    for game in app.engine.games():
        owner = game.get("owner_id")
        if owner not in owners:
            continue
        games[game["id"]] = owner
        totals.setdefault(owner, {"equity": ZERO, "invested": ZERO, "estimated": False})
        row = totals[owner]
        if board != "weekly":
            try:
                equity = app.engine.valuation(game)["equity"]
            except ValueError:
                equity = game.get("last_equity", game["initial"])
                row["estimated"] = True
            row["equity"] += number(equity)
            row["invested"] += number(game["invested"])

    weekly = defaultdict(lambda: ZERO)
    if board == "weekly" and games:
        for row in app.db.execute("""SELECT game,data FROM logs WHERE created>=? AND created<=?
                AND json_extract(data,'$.status')='success'
                AND json_extract(data,'$.action') IN ('open','close')""", (week.timestamp(), now.timestamp())):
            owner = games.get(row["game"])
            if owner is None:
                continue
            record = json.loads(row["data"])
            detail = record["detail"]
            weekly[owner] -= number(detail.get("fee", "0"))
            if record["action"] == "close":
                weekly[owner] += number(detail["position"].get("pnl", "0"))

    scores = []
    for owner, row in totals.items():
        if board == "wealth":
            score = row["equity"]
        elif board == "weekly":
            score = weekly[owner]
        elif row["invested"] > 0:
            score = (row["equity"] / row["invested"] - 1) * 100
        else:
            continue
        scores.append((score.quantize(CENT), owner))
    scores.sort(key=lambda item: (-item[0], item[1]))

    entries = []
    my_rank = None
    previous_score = None
    rank = 0
    for index, (score, owner) in enumerate(scores, 1):
        if score != previous_score:
            rank = index
        previous_score = score
        if owner == viewer:
            my_rank = rank
        if index > 30 and owner != viewer:
            continue
        profile = app.profile(owner)
        entries.append({
            "rank": rank, "name": profile.get("agent_name") or "久留美",
            "avatar": profile.get("agent_avatar") or "avatar.png",
            "value": str(score), "mine": owner == viewer,
            "estimated": totals[owner]["estimated"],
        })
    return {"entries": entries, "total": len(scores), "my_rank": my_rank,
            "week_start": week.date().isoformat(), "updated_at": int(now.timestamp())}
