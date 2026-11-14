import sqlite3
import unittest

from src.portal.community import Community


class CommunityTests(unittest.TestCase):
    def setUp(self):
        self.db = sqlite3.connect(":memory:")
        self.db.row_factory = sqlite3.Row
        self.db.execute("CREATE TABLE sim_users(id TEXT PRIMARY KEY, nickname TEXT NOT NULL)")
        self.db.execute("INSERT INTO sim_users VALUES('u1', '测试用户')")
        self.community = Community(self.db)

    def test_populated_legacy_database_upgrades_and_remains_writable(self):
        db = sqlite3.connect(":memory:")
        self.addCleanup(db.close)
        db.row_factory = sqlite3.Row
        db.executescript("""
          CREATE TABLE sim_users(id TEXT PRIMARY KEY, nickname TEXT NOT NULL);
          INSERT INTO sim_users VALUES('u1','原群主'),('u2','新成员');
          CREATE TABLE community_posts(id INTEGER PRIMARY KEY, owner TEXT,
            title TEXT, body TEXT, created REAL DEFAULT (unixepoch()));
          CREATE TABLE community_messages(id INTEGER PRIMARY KEY, owner TEXT,
            post INTEGER, body TEXT, created REAL DEFAULT (unixepoch()));
          CREATE TABLE community_groups(id INTEGER PRIMARY KEY, owner TEXT, name TEXT);
          CREATE TABLE community_group_members(room INTEGER, owner TEXT, PRIMARY KEY(room,owner));
          CREATE TABLE community_group_messages(id INTEGER PRIMARY KEY, room INTEGER,
            owner TEXT, body TEXT, created REAL DEFAULT (unixepoch()));
          INSERT INTO community_posts(owner,title,body) VALUES('u1','旧帖子','保留正文');
          INSERT INTO community_messages(owner,post,body) VALUES('u1',1,'保留评论');
          INSERT INTO community_groups(owner,name) VALUES('u1','旧群');
          INSERT INTO community_group_members VALUES(1,'u1');
          INSERT INTO community_group_messages(room,owner,body) VALUES(1,'u1','保留消息');
        """)
        community = Community(db)
        first_joined = db.execute("SELECT joined FROM community_group_members").fetchone()[0]
        community = Community(db)
        self.assertEqual(community.thread('1')['body'], '保留正文')
        self.assertEqual(community.thread('1')['comments'], 1)
        group = community.groups('u1', 'GET', '1', {})
        self.assertEqual(group['group']['role'], 'owner')
        self.assertGreater(group['group']['created'], 0)
        self.assertEqual(group['entries'][0]['body'], '保留消息')
        self.assertEqual(db.execute("SELECT joined FROM community_group_members").fetchone()[0], first_joined)
        self.assertGreater(first_joined, 0)
        community.groups('u2', 'POST', '1', {'action': 'join'})
        self.assertGreater(db.execute("SELECT joined FROM community_group_members WHERE owner='u2'").fetchone()[0], 0)
        new_room = community.groups('u1', 'POST', '', {'name': '新群'})['id']
        self.assertGreater(community.groups('u1', 'GET', str(new_room), {})['group']['created'], 0)

    def test_seed_is_idempotent_and_exposes_stats(self):
        self.community.seed_demo("u1")
        self.community.seed_demo("u1")
        summary = self.community.summary("u1")
        self.assertEqual(summary["posts"], 3)
        self.assertEqual(summary["comments"], 3)
        self.assertEqual(summary["groups"], 1)
        self.assertEqual(summary["joined_groups"], 1)
        self.assertEqual(len(self.community.handle("u1", "GET", "", {})["entries"]), 3)
        groups = self.community.groups("u1", "GET", "", {})["groups"]
        self.assertEqual(groups[0]["members"], 1)
        self.assertEqual(groups[0]["messages"], 1)

    def test_thread_reports_comment_count(self):
        post = self.community.handle("u1", "POST", "", {"title": "标题", "body": "正文"})["id"]
        self.community.handle("u1", "POST", str(post), {"body": "评论"})
        self.assertEqual(self.community.thread(str(post))["comments"], 1)

    def test_moderation_rejects_missing_targets_and_requires_delete_reason(self):
        post = self.community.handle("u1", "POST", "", {"title": "待审核", "body": "正文"})["id"]
        with self.assertRaises(Exception):
            self.community.moderation("admin", {"action": "pin-post", "id": post + 1})
        with self.assertRaises(ValueError):
            self.community.moderation("admin", {"action": "delete-post", "id": post})
        row = self.db.execute("SELECT status,pinned FROM community_posts WHERE id=?", (post,)).fetchone()
        self.assertEqual(tuple(row), ("active", 0))

        self.community.moderation("admin", {"action": "delete-post", "id": post, "reason": "违反社区规则"})
        audit = self.db.execute("SELECT action,target_id,reason FROM community_audit_log").fetchone()
        self.assertEqual(tuple(audit), ("delete-post", str(post), "违反社区规则"))

    def test_search_tags_pagination_and_delete_permissions(self):
        ids = []
        for index in range(3):
            ids.append(self.community.handle("u1", "POST", "", {
                "title": f"EUR/USD 观察 {index}", "body": "风险控制", "tags": ["risk", "EUR/USD"]
            })["id"])
        result = self.community.handle("u1", "GET", "", {}, {"keyword": "EUR/USD", "tag": "risk", "limit": 2})
        self.assertEqual(len(result["entries"]), 2)
        self.assertTrue(result["has_more"])
        next_page = self.community.handle("u1", "GET", "", {}, {"before": result["next_before"], "limit": 2})
        self.assertEqual(len(next_page["entries"]), 1)
        self.db.execute("INSERT INTO sim_users VALUES('u2', '另一个用户')")
        with self.assertRaises(Exception):
            self.community.delete_post("u2", ids[0])
        self.community.delete_post("u1", ids[0])
        self.assertEqual(len(self.community.handle("u1", "GET", "", {}, {"keyword": "EUR/USD"})["entries"]), 2)

    def test_group_members_unread_and_non_member_access(self):
        self.db.execute("INSERT INTO sim_users VALUES('u2', '另一个用户')")
        room = self.community.groups("u1", "POST", "", {"name": "测试群"})["id"]
        with self.assertRaises(Exception):
            self.community.groups("u2", "GET", str(room), {})
        self.assertEqual(self.community.groups("u2", "GET", "", {})["groups"][0]["unread"], 0)
        self.community.groups("u2", "POST", str(room), {"action": "join"})
        self.community.groups("u1", "POST", str(room), {"body": "第一条"})
        detail = self.community.groups("u2", "GET", str(room), {})
        self.assertEqual(detail["group"]["unread"], 1)
        self.assertEqual(self.community.groups("u2", "GET", "", {})["groups"][0]["unread"], 1)
        self.community.groups("u2", "POST", str(room), {"action": "mark-read"})
        self.assertEqual(self.community.groups("u2", "GET", str(room), {})["group"]["unread"], 0)
        self.assertEqual(self.community.groups("u2", "GET", "", {})["groups"][0]["unread"], 0)

    def test_group_list_reports_unread_for_more_than_twelve_joined_groups(self):
        self.db.execute("INSERT INTO sim_users VALUES('u2', '另一个用户')")
        rooms = []
        for index in range(13):
            room = self.community.groups("u1", "POST", "", {"name": f"测试群 {index}"})["id"]
            self.community.groups("u2", "POST", str(room), {"action": "join"})
            self.community.groups("u1", "POST", str(room), {"body": "未读消息"})
            rooms.append(room)
        listed = {item["id"]: item for item in self.community.groups("u2", "GET", "", {})["groups"]}
        self.assertEqual(len(rooms), 13)
        self.assertTrue(all(listed[room]["unread"] == 1 for room in rooms))

    def test_join_starts_unread_tracking_from_join_time_and_read_id_is_bounded(self):
        self.db.execute("INSERT INTO sim_users VALUES('u2', '另一个用户')")
        room = self.community.groups("u1", "POST", "", {"name": "历史群聊"})["id"]
        self.community.groups("u1", "POST", str(room), {"body": "加入前的历史消息"})

        self.community.groups("u2", "POST", str(room), {"action": "join"})
        detail = self.community.groups("u2", "GET", str(room), {})
        self.assertEqual(detail["group"]["unread"], 0)

        self.community.groups("u2", "POST", str(room), {"action": "mark-read", "message_id": 10**12})
        self.community.groups("u1", "POST", str(room), {"body": "加入后的新消息"})
        detail = self.community.groups("u2", "GET", str(room), {})
        self.assertEqual(detail["group"]["unread"], 1)


if __name__ == "__main__":
    unittest.main()
