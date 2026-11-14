import asyncio
import os
import shutil
import tempfile
import time
import unittest
from pathlib import Path

from aiohttp.test_utils import TestClient, TestServer

from app import Demo


class CommunityApiFlow(unittest.IsolatedAsyncioTestCase):
    async def test_forum_groups_and_offline_ai(self):
        directory = tempfile.mkdtemp()
        try:
            demo = Demo(Path(directory) / "ledger.sqlite3")
            headers = {"Authorization": "Bearer " + demo.admin}
            async with TestClient(TestServer(demo.app(True))) as admin, TestClient(TestServer(demo.app(False))) as user:
                created = await admin.post("/api/create", headers=headers,
                                           json={"nickname": "API用户", "name": "API用户"})
                self.assertEqual(created.status, 200, await created.text())
                credentials = await created.json()
                login = await user.post("/api/login", json=credentials)
                self.assertEqual(login.status, 200, await login.text())
                game_id = demo.engine.games()[0]["id"]
                demo.db.execute("INSERT INTO replay_equity(game,t,equity,return_rate,drawdown) VALUES(?,?,?,?,?)",
                                (game_id, time.time(), 1000, 0, 4.25))

                forum = await user.get("/api/forum?tag=行情&limit=1")
                self.assertEqual(forum.status, 200, await forum.text())
                page = await forum.json()
                self.assertEqual(len(page["entries"]), 1)
                self.assertTrue(page["entries"][0]["tags"])

                post = await user.post("/api/forum", json={"title": "API复盘", "body": "观察 EUR/USD 风险", "tags": ["复盘", "EUR/USD"]})
                self.assertEqual(post.status, 200, await post.text())
                post_id = (await post.json())["id"]
                comment = await user.post(f"/api/forum?post={post_id}", json={"body": "补充止损条件"})
                self.assertEqual(comment.status, 200, await comment.text())
                comment_id = (await comment.json())["id"]

                own_delete = await user.delete(f"/api/forum?comment={comment_id}")
                self.assertEqual(own_delete.status, 200, await own_delete.text())
                own_post_delete = await user.delete(f"/api/forum?post={post_id}")
                self.assertEqual(own_post_delete.status, 200, await own_post_delete.text())

                groups = await user.get("/api/groups")
                self.assertEqual(groups.status, 200, await groups.text())
                room = (await groups.json())["groups"][0]["id"]
                detail = await user.get(f"/api/groups?room={room}")
                self.assertEqual(detail.status, 200, await detail.text())
                self.assertIn("members", await detail.json())
                sent = await user.post(f"/api/groups?room={room}", json={"body": "API群聊消息"})
                self.assertEqual(sent.status, 200, await sent.text())
                marked = await user.post(f"/api/groups?room={room}", json={"action": "mark-read"})
                self.assertEqual(marked.status, 200, await marked.text())

                review = await user.post("/api/ai/trade-review", json={"days": 1})
                self.assertEqual(review.status, 200, await review.text())
                review_data = await review.json()
                self.assertIn("risk_points", review_data)
                self.assertIn("stats", review_data)
                self.assertEqual(review_data["stats"]["max_drawdown"], 4.25)
                self.assertTrue(any("4.25%" in item for item in review_data["risk_points"]))

                summary = await user.post("/api/ai/group-summary", json={"room": room})
                self.assertEqual(summary.status, 200, await summary.text())
                self.assertIn("summary", await summary.json())

                draft = await user.post("/api/ai/forum-draft", json={"topic": "仓位管理"})
                self.assertEqual(draft.status, 200, await draft.text())
                self.assertTrue((await draft.json())["requires_confirmation"])

                review_draft_response = await user.post("/api/ai/forum-draft", json={
                    "topic": "交易复盘草稿", "trade_review": True, "days": 1, "pair": ""
                })
                self.assertEqual(review_draft_response.status, 200, await review_draft_response.text())
                review_draft = await review_draft_response.json()
                self.assertEqual(review_draft["review"]["days"], 1)
                self.assertGreater(review_draft["review"]["end"], 0)
                shared = await user.post("/api/forum/share-review", json={
                    **review_draft["review"], "title": review_draft["title"],
                    "body": review_draft["body"], "tags": review_draft["tags"]
                })
                self.assertEqual(shared.status, 200, await shared.text())
                shared_data = await shared.json()
                self.assertEqual(shared_data["review"]["stats"]["end"], review_draft["review"]["end"])
                shared_thread = await user.get(f"/api/forum?post={shared_data['post']['id']}")
                self.assertEqual(shared_thread.status, 200, await shared_thread.text())
                saved_review = (await shared_thread.json())["post"]
                self.assertEqual(saved_review["reference_type"], "trade_review")
                self.assertEqual(saved_review["reference_id"], str(review_draft["review"]["end"]))
                self.assertEqual(saved_review["pair"], review_draft["review"]["pair"])

                daily = await user.get("/api/ai/community-daily-summary")
                self.assertEqual(daily.status, 200, await daily.text())
                self.assertIn("hot_posts", await daily.json())
            demo.db.close()
        finally:
            shutil.rmtree(directory, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
