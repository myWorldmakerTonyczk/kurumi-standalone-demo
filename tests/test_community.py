import sqlite3
import unittest

from aiohttp.web import HTTPNotFound

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

    def test_thread_replies_likes_and_inbox(self):
        self.db.executescript("""
            INSERT INTO sim_users VALUES('u2', '回复者'), ('u3', '旁观者');
            CREATE TABLE games(id TEXT PRIMARY KEY, data TEXT);
            INSERT INTO games VALUES('g', '{"owner_id":"u1"}'), ('g2', '{"owner_id":"u2"}');
            CREATE TABLE demo_chat(id INTEGER PRIMARY KEY, game TEXT, role TEXT, content TEXT, created REAL);
            INSERT INTO demo_chat VALUES(1,'g','assistant','收到',unixepoch()),
                (2,'g2','assistant','别人的私聊',unixepoch());
        """)
        c = self.community
        post = str(c.handle('u3', 'POST', '', {'title': '主题', 'body': '正文'})['id'])
        root = c.handle('u1', 'POST', post, {'body': '评论'})['id']
        reply = c.handle('u2', 'POST', post, {'body': '@测试用户 回复', 'reply_to': root})['id']
        other_post = str(c.handle('u2', 'POST', '', {'title': '另一个主题', 'body': '正文'})['id'])
        with self.assertRaises(ValueError):
            c.handle('u2', 'POST', post, {'body': '无效评论', 'reply_to': '1'})
        with self.assertRaises(HTTPNotFound):
            c.handle('u2', 'POST', other_post, {'body': '跨帖子回复', 'reply_to': root})

        like = {'action': 'like', 'comment': reply, 'liked': True}
        c.handle('u1', 'POST', post, like)
        c.handle('u1', 'POST', post, like)
        entry = c.handle('u1', 'GET', post, {})['entries'][0]
        self.assertEqual((entry['reply_to'], entry['likes'], entry['liked']), (root, 1, 1))
        c.handle('u1', 'POST', post, dict(like, liked=False))
        self.assertEqual(c.handle('u1', 'GET', post, {})['entries'][0]['likes'], 0)

        room = str(c.groups('u2', 'POST', '', {'name': '测试群'})['id'])
        c.groups('u1', 'POST', room, {'action': 'join'})
        mention = c.groups('u2', 'POST', room, {'body': '@测试用户 来看看'})['id']
        c.groups('u2', 'POST', room, {'body': '@测试用户甲 你也来看看'})
        inbox = {(row['kind'], row['id']): row['unread'] for row in c.inbox('u1')}
        self.assertEqual(inbox, {('private', 1): True, ('forum', reply): True, ('group', mention): True})
        self.db.executescript("""
            INSERT INTO demo_chat VALUES(3,'g','assistant','同组未读',unixepoch()),
                (4,'g','assistant','之后到达的消息',unixepoch());
        """)
        c.mark_inbox_read('u1', 'private', 3)
        self.assertEqual([row['id'] for row in c.inbox('u1', unread_only=True) if row['kind'] == 'private'], [4])
        self.assertTrue(next(row['unread'] for row in c.inbox('u2') if row['kind'] == 'private'))
        c.mark_inbox_read('u1', 'forum', reply)
        self.assertFalse(next(row['unread'] for row in c.inbox('u1') if row['kind'] == 'forum'))
        c.groups('u1', 'POST', room, {'action': 'leave'})
        self.assertFalse(any(row['kind'] == 'group' for row in c.inbox('u1')))

    def test_mark_all_clears_all_sources_and_only_the_current_memberships(self):
        self.db.executescript("""
            INSERT INTO sim_users VALUES('u2', 'Other');
            CREATE TABLE games(id TEXT PRIMARY KEY, data TEXT);
            INSERT INTO games VALUES('g1','{"owner_id":"u1"}'), ('g2','{"owner_id":"u1"}'),
                ('other','{"owner_id":"u2"}');
            CREATE TABLE demo_chat(id INTEGER PRIMARY KEY, game TEXT, role TEXT, content TEXT, created REAL);
            INSERT INTO demo_chat(game,role,content,created)
                VALUES('g2','assistant','older instance',unixepoch()), ('other','assistant','other user',unixepoch());
        """)
        self.db.executemany("""INSERT INTO demo_chat(game,role,content,created)
                               VALUES('g1','assistant',?,unixepoch())""", ((str(i),) for i in range(105)))
        c = self.community
        thread = str(c.handle("u1", "POST", "", {"title": "Thread", "body": "Body"})["id"])
        c.handle("u2", "POST", thread, {"body": "Forum reply"})
        room = str(c.groups("u2", "POST", "", {"name": "Shared room"})["id"])
        c.groups("u1", "POST", room, {"action": "join"})
        c.groups("u2", "POST", room, {"body": "@测试用户 Group mention"})
        c.groups("u2", "POST", room, {"body": "A normal group message"})
        newest = c.groups("u1", "POST", room, {"body": "My own message"})["id"]
        self.assertEqual(len(c.inbox("u1", unread_only=True)), 100)
        self.assertEqual(c.groups("u1", "GET", room, {})["group"]["unread"], 2)

        self.assertEqual(c.mark_inbox_read_all("u1"), 108)
        self.assertEqual(c.inbox("u1", unread_only=True), [])
        self.assertEqual(c.groups("u1", "GET", room, {})["group"]["unread"], 0)
        cursor = self.db.execute("""SELECT last_read_message FROM community_group_members
                                     WHERE owner='u1' AND room=?""", (room,)).fetchone()[0]
        self.assertEqual(cursor, newest)
        self.assertTrue(c.inbox("u2", unread_only=True))
        self.assertEqual(c.groups("u2", "GET", room, {})["group"]["unread"], 1)
        self.assertEqual(c.mark_inbox_read_all("u1"), 0)
        c.groups("u2", "POST", room, {"body": "@测试用户 New mention"})
        self.assertEqual(c.groups("u1", "GET", room, {})["group"]["unread"], 1)
        self.assertEqual(len(c.inbox("u1", unread_only=True)), 1)

    def test_thread_reports_comment_count(self):
        post = self.community.handle("u1", "POST", "", {"title": "标题", "body": "正文"})["id"]
        self.community.handle("u1", "POST", str(post), {"body": "评论"})
        self.assertEqual(self.community.thread(str(post))["comments"], 1)

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
        self.community.groups("u1", "POST", str(room), {"body": "读取过程中到达的消息"})
        result = self.community.groups("u2", "POST", str(room), {
            "action": "mark-read", "message_id": detail["entries"][0]["id"],
        })
        self.assertEqual(result["unread"], 1)
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
