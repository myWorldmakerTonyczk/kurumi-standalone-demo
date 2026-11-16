from zoneinfo import ZoneInfo
from datetime import date, datetime, time, timedelta
from src.agent.memory import ranked_tags
from src.core.engine import dump
BEIJING = ZoneInfo("Asia/Shanghai")


def build(app, owner):
    async def search_memory(keyword: str = "", tags: list[str] | None = None,
                            person: str = "", start_date: str = "", end_date: str = "",
                            limit: int = 10, list_tags: bool = False) -> str:
        """查询当前用户的有效长期记忆，按当前强度降序。
        list_tags=true 时列出当前账号可检索记忆中按关联记忆的最高当前强度列出前 100 个标签，忽略其他筛选条件。
        keyword 搜索记忆正文。tags 填多个标签时须全部匹配。
        person 可填用户 ID、昵称、智能体名、原作人物姓名或 self（当前用户）。重名时用固定 ID 缩小范围。
        start_date、end_date 按北京时间筛选创建日期，格式 YYYY-MM-DD，包含首尾两天。
        所有条件可组合或留空，limit 为 1～20。
        """
        if not app.profile(owner).get("chat_memory", {}).get("enabled", True):
            return dump({"tags" if list_tags else "entries": [], "disabled": True})
        if list_tags:
            return dump({"tags": ranked_tags(app.memory.list(owner))})
        try:
            start = datetime.combine(date.fromisoformat(start_date), time.min, BEIJING).timestamp() if start_date else 0
            end = (datetime.combine(date.fromisoformat(end_date), time.min, BEIJING)
                   + timedelta(days=1)).timestamp() if end_date else float("inf")
        except (ValueError, OverflowError):
            return dump({"error": "日期请使用 YYYY-MM-DD"})
        if start >= end:
            return dump({"error": "开始日期不能晚于结束日期"})

        names = {}
        if person.strip():
            names.update({subject: (name,) for subject, name in app.memory.character_subjects().items()})
            for user in app.db.execute("SELECT id,nickname FROM sim_users"):
                uid = user["id"]
                names["self" if uid == owner else "user:" + uid] = (uid, user["nickname"])
                agent = app.profile(uid).get("agent_name", "久留美")
                names["agent:" + uid] = (uid, agent)

        person = person.strip().casefold()
        keyword = keyword.strip().casefold()
        wanted_tags = {tag.strip().casefold() for tag in tags or [] if tag.strip()}
        results = []
        for row in app.memory.list(owner):
            subject = row["subject"]
            identities = (subject, subject.partition(":")[2], *names.get(subject, ()))
            if row.get("permanent"):
                identities += tuple(row["tags"])
            if person and all(person != value.casefold() for value in identities):
                continue
            if (row["strength"] <= 0 or not start <= row["created"] < end
                    or keyword not in row["text"].casefold()
                    or not wanted_tags <= {tag.casefold() for tag in row["tags"]}):
                continue
            results.append(row)
        results.sort(key=lambda row: (row["strength"], row["created"], row["id"]), reverse=True)
        entries = []
        for row in results[:max(1, min(20, limit))]:
            entry = {key: row[key] for key in ("id", "text", "subject", "kind", "tags", "strength")}
            if row.get("permanent"):
                entry["permanent"] = True
                entry["person"] = row["name"]
            else:
                entry["user_id"] = owner if row["subject"] == "self" else row["subject"].partition(":")[2]
            entry["created_at"] = datetime.fromtimestamp(row["created"], BEIJING).isoformat(timespec="seconds")
            entries.append(entry)
        return dump({"entries": entries})

    return search_memory
