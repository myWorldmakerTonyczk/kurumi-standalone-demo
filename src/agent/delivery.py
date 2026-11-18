"""Persistence and delivery adapters for chat messages."""
import time

from src.core.engine import dump
from .media import normalize_attachment


def deliver_private(app, game, content, attachments, *, proactive=False, limit=1500):
    owner = game["owner_id"]
    cursor = app.db.execute("""INSERT INTO demo_chat(game,role,content,created,proactive,attachments)
        VALUES(?, 'assistant', ?, ?, ?, ?)""",
        (game["id"], content[:limit], time.time(), int(proactive), dump(attachments)))
    app.timeline.reference(owner, {"type": "private_message", "game_id": game["id"], "id": cursor.lastrowid})
    return cursor.lastrowid


def deliver_group(app, owner, room, content, attachment=None, *, mention_depth=0):
    return app.social.send(owner, room, content, normalize_attachment(attachment),
                           mention_depth=mention_depth)
