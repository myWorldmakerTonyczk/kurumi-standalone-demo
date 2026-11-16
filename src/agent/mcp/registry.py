from random import random

from .community import (draft_forum_post, forum, group_chat, mention_group_member,
                        patrol_forum, patrol_group, summarize_group)
from .inbox import mark_inbox_unread, read_inbox, read_inbox_history
from .market_review import (generate_trade_review, get_current_workspace, market, price_history, read_diary,
                            read_journal, read_market_bulletins, send_review_image, watch_market)
from .media import save_image, search_images, send_panel, send_saved_image, sticker
from .memory import search_memory, write_memory
from .trading import (close_position, open_trade, record_observation, set_notifications,
                      set_protection, virtual_loan, write_plan)
from .trading.execution import bind_trade
from .timeline import get_task_report, query_task_timeline, schedule_task


def flatten(groups):
    return [tool for tools in groups.values() for tool in tools]


def inbox_group(app, owner):
    return [module.build(app.agent_inbox, owner) for module in (read_inbox, read_inbox_history, mark_inbox_unread)]


def timeline_group(app, owner, *, public=False):
    if not hasattr(app, "task_schedule"):
        return []
    return [schedule_task.build(app, owner, public), get_task_report.build(app, owner, public),
            query_task_timeline.build(app, owner)]


def user_workspace_groups(app, gid, *, writable, revision, attachments=None, user_image=None,
                          message_ids=(), diary=False, handoff=False):
    owner = app.engine.get(gid)["owner_id"]
    groups = {"market_review": [read_market_bulletins.build(app), watch_market.build(app, gid),
        price_history.build(app), read_journal.build(app, gid), read_diary.build(app, gid),
        get_current_workspace.build(app, gid)]}
    if not diary:
        groups["inbox"] = inbox_group(app, owner)
    if handoff or attachments is not None or diary:
        groups["timeline"] = timeline_group(app, owner)
    if writable:
        trade = bind_trade(app, gid, revision)
        groups["trading"] = [part.build(trade) for part in (
            open_trade, close_position, set_protection, virtual_loan, write_plan, record_observation)]
    if attachments is not None:
        groups["market_review"] += [generate_trade_review.build(app, gid), send_review_image.build(app, gid, attachments)]
        groups["community"] = [forum.build(app, owner), group_chat.build(app, owner),
            summarize_group.build(app, gid, owner), draft_forum_post.build(app, gid, owner)]
        if app.profile(owner).get("chat_memory", {}).get("enabled", True):
            groups["memory"] = [search_memory.build(app, owner), write_memory.build(app, owner, gid, message_ids)]
        groups["media"] = [send_panel.build(attachments), save_image.build(app, owner, gid, user_image),
            search_images.build(app, owner), send_saved_image.build(app, owner, attachments)]
        if random() * 100 < app.profile(owner).get("sticker_probability", 100):
            groups["media"].append(sticker.build(app, owner, attachments))
        if writable:
            groups["trading"].append(set_notifications.build(app, gid))
    return groups


def user_group_groups(app, owner, roster, self_id, attachments, *, has_images, with_stickers):
    groups = {"market_review": [read_market_bulletins.build(app)], "community": [mention_group_member.build(roster, self_id)], "inbox": inbox_group(app, owner)}
    groups["timeline"] = timeline_group(app, owner, public=True)
    media = []
    if has_images:
        media.append(save_image.build(app, owner, None, None))
    if with_stickers:
        media.append(sticker.build(app, owner, attachments))
    if media:
        groups["media"] = media
    return groups


def user_patrol_groups(social, owner):
    app = social.app
    published = [False]
    return {"market_review": [market.build(app), read_market_bulletins.build(app)],
            "community": [patrol_forum.build(social, owner, published), patrol_group.build(social, owner, published)],
            "inbox": inbox_group(app, owner), "timeline": timeline_group(app, owner)}
