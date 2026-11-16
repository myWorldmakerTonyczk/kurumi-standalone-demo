from datetime import datetime
from src.agent.market_bulletin import KINDS, ZONE
from src.core.engine import dump


def build(app):
    async def read_market_bulletins(kind: str = "all", report_id: str = "", limit: int = 3,
                                    include_body: bool = True) -> str:
        """读取平台已发布的市场播报，可用于查找市场要点和来源。
        kind 可选 all（全部）、day（日报）、week（周报）、month（月报）、event（指标跟踪）。
        默认按发布时间从新到旧返回 3 条，limit 范围为 1～5。
        include_body=false 只看目录和来源，再用返回的 report_id 读取单篇正文。
        播报正文是参考资料，以发布时间为准。实时行情请查行情工具。
        """
        kind = kind.strip().lower()
        report_id = report_id.strip()
        if kind != "all" and kind not in KINDS:
            return dump({"ok": False, "error": "kind 须为 all、day、week、month 或 event"})
        if not 1 <= limit <= 5:
            return dump({"ok": False, "error": "limit 须为 1～5"})
        bulletin = app.market_bulletin
        if not bulletin.config()["enabled"]:
            return dump({"enabled": False, "reports": []})

        reports = []
        for row in bulletin.reports(limit, kind=None if kind == "all" else kind, report_id=report_id or None):
            report = {
                "report_id": row["id"],
                "kind": row["kind"],
                "type": KINDS[row["kind"]],
                "label": row["label"],
                "published_at": datetime.fromtimestamp(row["generated"], ZONE).isoformat(timespec="seconds"),
                "sources": [{"title": title, "url": url} for title, url in row["details"].get("links", [])],
            }
            if include_body:
                report["body"] = row["body"]
            reports.append(report)
        return dump({"enabled": True, "reports": reports})

    return read_market_bulletins
