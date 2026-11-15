import base64
import json
import sqlite3
import unittest
from io import BytesIO
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from aiohttp.web import HTTPNotFound
from PIL import Image

from src.agent.image_library import ImageLibrary
from src.agent.tool.images import build_tools
from src.portal.community import Community


class ImageLibraryTests(unittest.IsolatedAsyncioTestCase):
    async def test_scan_save_search_and_send_with_source_permissions(self):
        db = sqlite3.connect(":memory:")
        self.addCleanup(db.close)
        db.row_factory = sqlite3.Row
        db.executescript("""
            CREATE TABLE sim_users(id TEXT PRIMARY KEY,nickname TEXT);
            INSERT INTO sim_users VALUES('u1','Owner'), ('u2','Member');
            CREATE TABLE games(id TEXT PRIMARY KEY,data TEXT);
            INSERT INTO games VALUES('g','{"owner_id":"u1"}');
            CREATE TABLE demo_chat(id INTEGER PRIMARY KEY,game TEXT,content TEXT,attachments TEXT);
        """)
        community = Community(db)
        app = SimpleNamespace(db=db, profile=lambda owner: {}, model_url=None,
                              call_logs=SimpleNamespace(callback=lambda *args: None))
        app.images = library = ImageLibrary(app)
        raw = BytesIO()
        Image.new("RGB", (8, 8), "red").save(raw, format="WEBP")
        src = "data:image/webp;base64," + base64.b64encode(raw.getvalue()).decode()
        media = {"kind": "image", "src": src}
        db.execute("INSERT INTO demo_chat VALUES(1,'g','Picture',?)", (json.dumps([media]),))
        post = community.handle("u1", "POST", "", {"title": "Example", "body": "Image", "title_image": src})["id"]
        room = str(community.groups("u1", "POST", "", {"name": "Group"})["id"])
        message = community.groups("u1", "POST", room, {"body": "Image", "image": src})["id"]

        with patch("src.agent.image_library.describe_image", new_callable=AsyncMock) as scan:
            scan.return_value = {"name": "Red chart", "category": "chart", "description": "Red square", "tags": ["red"]}
            with self.assertRaises(ValueError):
                await library.save("u2", "g", f"group:{room}:{message}")
            with self.assertRaises(ValueError):
                await library.save("u2", "g", "private:1:0")
            scan.assert_not_awaited()
            item = await library.save("u1", "g", "current", media)
            await library.save("u1", "g", "private:1:0")
            await library.save("u1", "g", f"group:{room}:{message}")
            item = await library.save("u1", "g", f"post:{post}:title", tags=["weekly"])
            scan.assert_awaited_once()
            self.assertTrue(scan.await_args.kwargs["catalog"])

        self.assertEqual(len(item["sources"]), 4)
        self.assertEqual(library.search("u1", "square", "chart", "weekly", "group")[0]["id"], item["id"])
        self.assertEqual(library.search("u2", "red"), [])
        with self.assertRaises(HTTPNotFound):
            library.get("u2", item["id"])
        library.update("u1", item["id"], {"name": "Revised", "category": "chart", "description": "Red", "tags": ["red"]})
        attachments = []
        send = build_tools(app, "u1", "g", attachments, None)[2]
        self.assertTrue(json.loads(await send(item["id"]))["ok"])
        self.assertEqual(attachments[0], {"kind": "image", "src": src, "label": "Revised"})
