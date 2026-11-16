import asyncio
import base64
from datetime import date, timedelta
from src.core.engine import dump
from src.core.replay import review


def build(app, gid, attachments):
    async def send_review_image(kind: str = "candles", pair: str = "", days: int = 1, end: int = 0,
                                kinds: list[str] | None = None, diary_date: str = "", text: str = "") -> str:
        """直接附上图片。K 线用 candles；还可生成净值、收益、回撤、日记等复盘图。"""
        from src.core.replay_image import CHARTS, render_chart_sheet

        try:
            selected = list(dict.fromkeys(kinds or [kind]))
            if not selected or any(item not in CHARTS for item in selected):
                raise ValueError("可选图表：" + "、".join(CHARTS))
            if sum(item["kind"] == "image" for item in attachments) >= 2:
                raise ValueError("每条回复最多附两张图")
            data = await review(app, gid, pair, days, end, include_candles="candles" in selected)
            data["text_panels"] = {}
            if "diary" in selected:
                before = (date.fromisoformat(diary_date) + timedelta(days=1)).isoformat() if diary_date else ""
                rows = app.diary.store.list(gid, before, 1, successful_only=True)
                if not rows or (diary_date and diary_date != rows[0]["day"]):
                    raise ValueError("没有对应的日记")
                data["text_panels"]["diary"] = dict(title="交易日记", subtitle=rows[0]["day"], content=rows[0]["content"][:6000])
            if "text" in selected:
                if not 1 <= len(text.strip()) <= 6000:
                    raise ValueError("图片正文须为 1～6000 字")
                data["text_panels"]["text"] = dict(title="文字记录", subtitle="", content=text)
            png = await asyncio.to_thread(render_chart_sheet, data, selected)
            attachments.append({"kind": "image", "label": "交易复盘图",
                                "src": "data:image/png;base64," + base64.b64encode(png).decode("ascii")})
            return dump({"ok": True, "message": "图片已附在回复中"})
        except ValueError as exc:
            return dump({"ok": False, "error": str(exc)})

    return send_review_image
