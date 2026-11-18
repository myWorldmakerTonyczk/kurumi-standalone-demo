"""Admin and user HTTP API dispatch."""
import base64
import hmac
import json
import os
import time

from aiohttp import web

from src.agent.chat_debounce import debounce_config
from src.agent.decision_settings import settings as rest_settings
from src.agent.memory import MEMORY_DAYS
from src.agent.model_registry import connections, public_models, selected, update as update_models
from src.agent.social import social_config
from src.agent.task_schedule import ACCOUNT_EVENTS
from src.agent.workflow import agent_real_name, persona_body
from src.core.engine import PAIRS, dump
from src.core.trading import public_snapshot
from src.diary.settings import settings as diary_settings
from src.notifications.settings import validate as notice_settings
from src.portal.community import community_image
from src.portal.leaderboard import standings
from src.users.ownership import issue_id
from src.web.config import BULLETIN_MODEL_PRESETS, DEFAULT_PERSONA, MODEL_PRESETS, listen_port


class ApiMixin:
    async def api_dispatch(self, request, admin):
        action = request.match_info.get("action")
        if not action and request.match_info.get("subaction"):
            action = "ai-" + request.match_info["subaction"]
        if not action and request.match_info.get("subroute"):
            action = "forum-" + request.match_info["subroute"]
        if action in {"forum", "onboarding", "user-settings", "agent-identity"} and request.method == "POST":
            request = request.clone(client_max_size=160 * 1024)
        body = await request.json() if request.method in {"POST", "DELETE"} and request.content_type == "application/json" else {}
        if admin:
            if request.method == "GET" and action == "login-mode":
                return web.json_response({"setup": self.admin_config is None})
            if request.method == "POST" and action in {"login", "setup"}:
                return web.json_response(await self.admin_login(action, body))
            if not hmac.compare_digest(request.headers.get("Authorization", ""), "Bearer " + self.admin):
                raise web.HTTPUnauthorized()
            if request.method == "GET" and action == "user-credentials":
                owner = request.query.get("owner", "")
                username = self.accounts.username(owner)
                if not username:
                    raise web.HTTPNotFound(reason="用户没有登录账号")
                return web.json_response(dict(username=username, password=self.accounts.admin_password(owner) or None),
                                         headers={"Cache-Control": "no-store"})
            if action == "market-bulletin-trace" and request.method == "GET":
                ident = request.query.get("id", "")
                if not ident.isascii() or not ident.isdecimal() or len(ident) > 18:
                    raise ValueError("日志编号无效")
                steps = self.market_bulletin.trace_detail(int(ident))
                if steps is None:
                    raise web.HTTPNotFound()
                return web.json_response({"steps": steps}, headers={"Cache-Control": "no-store"})
            if action == "market-bulletin":
                if request.method == "POST":
                    operation = body.get("action", "save")
                    if operation == "save":
                        self.market_bulletin.save(body)
                    elif operation == "check-source":
                        result = await self.market_bulletin.check_source(body.get("source"), body.get("url"))
                        return web.json_response(result, headers={"Cache-Control": "no-store"})
                    elif operation == "scan":
                        if not self.market_bulletin.config()["enabled"]:
                            raise ValueError("请先启用市场播报")
                        await self.market_bulletin.run(operation)
                    elif operation in ("cancel", "retry"):
                        self.market_bulletin.manage_task(operation, body.get("event"))
                    else:
                        raise ValueError("未知播报操作")
                elif request.method != "GET":
                    raise web.HTTPMethodNotAllowed(request.method, ["GET", "POST"])
                return web.json_response(self.market_bulletin.admin() | {"presets": BULLETIN_MODEL_PRESETS},
                                         headers={"Cache-Control": "no-store"})
            if request.method == "POST" and action == "create-user":
                owner = await self.accounts.create_web_user(body.get("username"), body.get("password"))
                return web.json_response({"id": owner, "username": self.accounts.username(owner)})
            if request.method == "POST" and action == "create":
                game = self.engine.create(dict(name=str(body.get("name") or "模拟账户"),
                    initial=self.dashboard.initial_funds(), user_nickname=str(body.get("nickname", "")), user_binding={"type": "random", "values": []},
                    _issue_user_token=False, auto=body.get("auto") is True))
                username, password = await self.accounts.issue(game["owner_id"])
                self.accounts.allow_trade(game["id"], True)
                return web.json_response(dict(username=username,password=password))
            if request.method == "GET" and action == "state":
                return web.json_response(dict(demo=self.demo, portal=os.getenv("KURUMI_PUBLIC_URL",
                    "http://127.0.0.1:8791"), games=[dict(id=g["id"], name=g["name"],
                    owner=g["owner_name"], username=self.accounts.username(g["owner_id"]), auto=g["auto"])
                    for g in self.engine.games()]))
            return web.json_response(await self.dashboard.admin(request, action, body))
        portal = self.dashboard.portal
        if action == "health" and request.method == "GET":
            response = web.json_response({"service": "kurumi-user-portal", "ok": True})
            if origin := request.headers.get("Origin"):
                if origin == f"http://127.0.0.1:{listen_port(True)}":
                    response.headers["Access-Control-Allow-Origin"] = origin
            return response
        if action == "workspace" and request.method == "GET":
            return await portal.workspace_state(request, controls=self.dashboard.workspace_controls())
        if action == "workspace-control" and request.method == "POST":
            session = portal.identity(request)
            if session["mode"] != "account":
                raise web.HTTPForbidden()
            game = portal.game(request, session)
            operation = body.get("action")
            if not isinstance(operation, str):
                raise ValueError("操作无效")
            control = {"pause": "pause", "resume": "pause", "decision": "decision",
                       "borrow": "loan", "repay": "loan"}.get(operation)
            if not control or not self.dashboard.workspace_controls()[control]:
                raise web.HTTPForbidden(reason="管理员已停用此操作")
            if game["status"] == "ended":
                raise ValueError("本局已结束")
            if operation == "decision":
                task = await self.run_decision(game, manual=True)
                return web.json_response({"message": "看盘已完成", "summary": task})
            return web.json_response(await self.dashboard.command(body | {"game": game["id"]}))
        portal_routes = {"session": portal.session,
                         "review": portal.review, "history": portal.history, "journal": portal.journal,
                         "notebook": portal.notebook, "diaries": portal.diaries, "account": portal.account_state}
        if request.method == "GET" and action in portal_routes:
            return await portal_routes[action](request)
        if request.method == "POST" and action in {"logout", "action"}:
            return await (portal.logout if action == "logout" else portal.action)(request)
        if action == "login" and request.method == "POST":
            username = str(body.get("username", ""))[:300]
            if not self.accounts.rate_limit(username):
                raise web.HTTPTooManyRequests()
            token = await self.accounts.login(username, body.get("password"))
            if not token:
                raise web.HTTPUnauthorized()
            response = web.json_response({"ok": True})
            response.set_cookie("kurumi_session", token, httponly=True, samesite="Strict",
                                secure=self.public_url().startswith("https://"), max_age=28800)
            return response
        if action == "external-tools" and request.method == "POST":
            return web.json_response(await self.external.call(request, body))
        session = self.accounts.access.session(request.cookies.get("kurumi_session", ""))
        if not session:
            raise web.HTTPUnauthorized()
        owner = session["owner"]
        if self.demo and session["mode"] == "account":
            self.community.seed_demo(owner)
        if action == "account-wakeups":
            if session["mode"] != "account":
                raise web.HTTPForbidden()
            profile = self.profile(owner)
            if request.method == "POST":
                if any(type(body.get(key)) is not bool for key in ACCOUNT_EVENTS):
                    raise ValueError("账户事件唤起设置无效")
                profile["account_wakeups"] = {key: body[key] for key in ACCOUNT_EVENTS}
                self.db.execute("INSERT OR REPLACE INTO demo_profiles VALUES(?,?)", (owner, dump(profile)))
            elif request.method != "GET":
                raise web.HTTPMethodNotAllowed(request.method, ["GET", "POST"])
            return web.json_response({key: profile.get("account_wakeups", {}).get(key, True)
                                      for key in ACCOUNT_EVENTS}, headers={"Cache-Control": "no-store"})
        if action == "agent-timeline":
            if session["mode"] != "account":
                raise web.HTTPForbidden()
            profile = self.profile(owner)
            if request.method == "POST":
                if type(body.get("enabled")) is not bool:
                    raise ValueError("时间线日志开关无效")
                profile["timeline_debug"] = body["enabled"]
                self.db.execute("INSERT OR REPLACE INTO demo_profiles VALUES(?,?)", (owner, dump(profile)))
            elif request.method != "GET":
                raise web.HTTPMethodNotAllowed(request.method, ["GET", "POST"])
            enabled = bool(profile.get("timeline_debug"))
            task_id = request.query.get("task")
            if task_id and enabled:
                task = self.timeline.report(owner, int(task_id))
                if not task:
                    raise web.HTTPNotFound()
                task["trace"] = self.timeline.trace_events(owner, int(task_id))
                return web.json_response(task, headers={"Cache-Control": "no-store"})
            trace_kind = {"memory": "memory", "memory-generation": "memory-generation"}.get(request.query.get("filter"), "")
            return web.json_response({"enabled": enabled,
                "tasks": self.timeline.history(owner, limit=50, trace_kind=trace_kind) if enabled else [],
                "logs": self.timeline.logs(owner) if enabled else []}, headers={"Cache-Control": "no-store"})
        if action == "agent-inbox":
            if session["mode"] != "account":
                raise web.HTTPForbidden()
            enabled = bool(self.profile(owner).get("show_agent_inbox"))
            if request.method == "POST":
                if type(body.get("enabled")) is not bool:
                    raise ValueError("邮箱选项无效")
                enabled = body["enabled"]
                self.db.execute("INSERT OR REPLACE INTO demo_profiles VALUES(?,?)",
                                (owner, dump(self.profile(owner) | {"show_agent_inbox": enabled})))
            elif request.method != "GET":
                raise web.HTTPMethodNotAllowed(request.method, ["GET", "POST"])
            result = {"enabled": enabled}
            if enabled:
                result.update(self.agent_inbox.history(owner,
                    source=request.query.get("source", ""), before=int(request.query.get("before", "0")),
                    unread=request.query.get("unread") == "1"))
                result["logs"] = self.agent_inbox.logs(owner)
            return web.json_response(result, headers={"Cache-Control": "no-store"})
        if action == "model-call-logs":
            if session["mode"] != "account":
                raise web.HTTPForbidden()
            headers = {"Cache-Control": "no-store"}
            if request.method == "POST":
                if type(body.get("enabled")) is not bool:
                    raise ValueError("日志开关格式无效")
                self.call_logs.set_enabled(owner, body["enabled"])
            elif request.method == "DELETE":
                self.call_logs.clear(owner)
            elif request.method != "GET":
                raise web.HTTPMethodNotAllowed(request.method, ["GET", "POST", "DELETE"])
            entry = request.query.get("id") if request.method == "GET" else None
            if entry is not None:
                if not entry.isascii() or not entry.isdecimal() or len(entry) > 18:
                    raise ValueError("日志编号无效")
                detail = self.call_logs.detail(owner, int(entry))
                if detail is None:
                    raise web.HTTPNotFound()
                return web.json_response(detail, headers=headers)
            return web.json_response({"enabled": self.call_logs.enabled(owner),
                "entries": self.call_logs.entries(owner, request.query.get("task", ""))}, headers=headers)
        if action == "market-bulletin" and request.method == "GET":
            return web.json_response(self.market_bulletin.public(), headers={"Cache-Control": "no-store"})
        if action == "agent-images":
            if session["mode"] != "account":
                raise web.HTTPForbidden()
            image_id = request.query.get("id")
            headers = {"Cache-Control": "private, no-store"}
            if request.method == "GET":
                if image_id:
                    row = self.images.get(owner, image_id)
                    prefix, payload = row["src"].split(",", 1)
                    return web.Response(body=base64.b64decode(payload),
                        content_type=prefix.split(";", 1)[0][5:], headers=headers)
                before = int(request.query.get("before", 0))
                if not 0 <= before < 2**63:
                    raise ValueError("图片分页参数无效")
                entries = self.images.search(owner, request.query.get("query", "")[:200],
                    request.query.get("category", "")[:40], request.query.get("tag", "")[:32],
                    request.query.get("source", ""), before=before, limit=31)
                return web.json_response({"entries": entries[:30], "has_more": len(entries) > 30}, headers=headers)
            if not image_id:
                raise ValueError("请指定图片")
            if request.method == "POST":
                self.images.update(owner, image_id, body)
            elif request.method == "DELETE":
                self.images.get(owner, image_id)
                self.db.execute("DELETE FROM agent_images WHERE owner=? AND id=?", (owner, image_id))
            else:
                raise web.HTTPMethodNotAllowed(request.method, ["GET", "POST", "DELETE"])
            return web.json_response({"ok": True}, headers=headers)
        if action == "agent-leaderboard":
            if session["mode"] != "account":
                raise web.HTTPForbidden()
            if request.method != "GET":
                raise web.HTTPMethodNotAllowed(request.method, ["GET"])
            return web.json_response(standings(self, owner, request.query.get("board", "wealth")),
                                     headers={"Cache-Control": "no-store"})
        if action == "community-summary" and request.method == "GET":
            if session["mode"] != "account":
                raise web.HTTPForbidden()
            return web.json_response(self.community.summary(owner))
        if action in {"agent-mode", "external-token"}:
            if session["mode"] != "account":
                raise web.HTTPForbidden()
            config = self.external.settings(owner)
            if action == "external-token":
                if request.method != "POST" or config["mode"] != "external":
                    raise web.HTTPForbidden()
                if body.get("action") == "revoke":
                    self.external.revoke(owner)
                    return web.json_response({"ok": True})
                if body.get("action") != "issue":
                    raise ValueError("无效的令牌操作")
                return web.json_response(self.external.issue(owner))
            if request.method == "POST":
                current = self.profile(owner)
                settings = self.mode_values(body, current)
                self.db.execute("INSERT OR REPLACE INTO demo_profiles VALUES(?,?)",
                                (owner, dump(current | settings)))
                config = self.external.settings(owner)
                if config["mode"] == "platform":
                    self.external.revoke(owner)
            expiry = self.db.execute("SELECT expires FROM external_tokens WHERE owner=?", (owner,)).fetchone()
            return web.json_response(config | {"username": self.accounts.username(owner),
                "endpoint": self.public_url().rstrip("/") + "/api/external-tools",
                "expires": expiry[0] if expiry and expiry[0] > time.time() else None})
        if action == "groups":
            if session["mode"] != "account":
                raise web.HTTPForbidden()
            room = request.query.get("room", "")
            attachment = request.query.get("attachment")
            if request.method == "GET" and attachment is not None:
                if not all(value.isascii() and value.isdecimal() and 0 < len(value) <= 18
                           for value in (room, attachment)):
                    raise ValueError("群聊附件编号无效")
                return web.Response(body=self.community.group_attachment(owner, int(room), int(attachment)),
                                    content_type="image/webp", headers={"Cache-Control": "private, no-store"})
            if body.get("action") == "invite-agent" and self.external.settings(owner)["mode"] != "platform":
                raise web.HTTPForbidden(reason="请先启用平台智能体")
            result = self.community.groups(owner, request.method, room, body, request.query)
            if result.get("message_id"):
                self.social.on_message(room, result["message_id"])
            if "entries" in result:
                result["entries"] = self.social.decorate(result["entries"])
                for entry in result["entries"]:
                    quote = entry.get("quote")
                    if quote:
                        if quote["owner"] is not None:
                            self.social.decorate([quote])
                            quote.pop("avatar", None)
                        else:
                            quote.pop("owner")
                result["agents"] = self.social.members(room)
                result["members"] = [row | self.social.decorate([row | {"is_agent": False}])[0]
                                     for row in result["members"]]
            for group in result.get("groups", []):
                group["agent_joined"] = self.social.joined(str(group["id"]), owner)
            return web.json_response(result, headers={"Cache-Control": "no-store"})
        if action == "inbox" and session["mode"] == "account":
            if request.method == "POST":
                if body.get("all") is True:
                    return web.json_response({"ok": True, "marked": self.community.mark_inbox_read_all(owner)})
                self.community.mark_inbox_read(owner, body.get("kind"), body.get("id"))
                return web.json_response({"ok": True})
            if request.method == "GET":
                return web.json_response({"entries": self.social.decorate(
                    self.community.inbox(owner, unread_only=request.query.get("unread") == "1"))},
                                         headers={"Cache-Control": "no-store"})
        if action in {"forum", "group-chat"}:
            if session["mode"] != "account":
                raise web.HTTPForbidden()
            post = request.query.get("post", "") if action == "forum" else None
            result = self.community.handle(owner, request.method, post, body, request.query)
            if post and request.method == "POST" and "id" in result:
                thread = self.community.thread(post)
                if thread["is_agent"] and body.get("body", "").strip():
                    self.remember_community(thread["owner"], owner, body["body"], "forum",
                                            stream=post, event="comment:" + str(result["id"]))
            if post and request.method == "GET":
                thread = self.community.thread(post, viewer=owner)
                result["post"] = self.social.decorate([thread | {"mine": thread["owner"] == owner}])[0]
            if request.method == "GET":
                result["entries"] = self.social.decorate(result["entries"])
            return web.json_response(result)
        games = [g for g in self.engine.games() if g.get("owner_id") == owner]
        game = portal.game(request, session) if request.query.get("game") else next(iter(games), None)
        if action == "ai" or action.startswith("ai-"):
            if session["mode"] != "account":
                raise web.HTTPForbidden()
            ai_action = action[3:] if action.startswith("ai-") else body.get("action", "")
            if ai_action != "community-daily-summary" and not game:
                raise ValueError("请先创建模拟交易")
            if ai_action == "trade-review":
                return web.json_response(await self.generate_trade_review(game, days=body.get("days", 1),
                    pair=body.get("pair", ""), end=body.get("end", 0)))
            if ai_action == "group-summary":
                return web.json_response(await self.summarize_group(game, owner, body.get("room"), limit=body.get("limit", 80)))
            if ai_action == "forum-draft":
                return web.json_response(await self.draft_forum_post(game, owner, body))
            if ai_action == "community-daily-summary":
                return web.json_response(await self.community_daily_summary(owner))
            raise web.HTTPNotFound()
        if action == "forum-share-review" and request.method == "POST":
            if session["mode"] != "account" or not game:
                raise web.HTTPForbidden()
            generated = await self.generate_trade_review(game, days=body.get("days", 1), pair=body.get("pair", ""), end=body.get("end", 0))
            result = self.community.share_review(owner, generated, body.get("title", ""), body.get("body", ""), body.get("tags"), body.get("pair", ""))
            return web.json_response({"ok": True, "post": result, "review": generated})
        if action == "trading-summary" and request.method == "GET":
            if not game:
                raise ValueError("请先创建模拟交易")
            return web.json_response(await self.generate_trade_review(game,
                days=request.query.get("days", 1), pair=request.query.get("pair", ""),
                end=request.query.get("end", 0)))
        if action == "model-presets" and request.method == "GET":
            return web.json_response(dict(presets=MODEL_PRESETS, persona=DEFAULT_PERSONA, agent_name="久留美", agent_real_name="福贺久留美", user_relationship="朋友",
                                          initial_funds=self.dashboard.initial_funds()))
        if action == "onboarding" and request.method == "POST":
            if games:
                return web.json_response({"id": games[0]["id"]})
            nickname = body.get("nickname")
            avatar = body.get("avatar") or "user-avatar.svg"
            if not isinstance(nickname, str) or not 1 <= len(nickname.strip()) <= 40 or any(ord(c) < 32 for c in nickname):
                raise ValueError("请输入 1～40 字的昵称")
            avatar = self.avatar_value(avatar)
            identity = self.agent_identity(body, owner)
            setup = {key: str(body.get(key, "")).strip() for key in ("url", "model", "key")}
            if any(len(value) > 4000 for value in setup.values()):
                raise ValueError("配置内容过长")
            setup["user_relationship"] = self.validated_user_relationship(body.get("user_relationship", ""))
            setup["persona"] = self.validated_persona(body.get("persona", ""))
            if setup["persona"] == DEFAULT_PERSONA:
                setup["persona"] = ""
            setup.update(self.mode_values(body, setup))
            if setup["key"]:
                self.model_url(setup["url"])
                if not setup["model"]:
                    raise ValueError("请输入模型 ID")
            row = self.db.execute("SELECT token FROM sim_user_tokens WHERE user_id=?", (owner,)).fetchone()
            user_token = row[0] if row else issue_id(self.db, owner)
            self.db.execute("UPDATE sim_users SET nickname=? WHERE id=?", (self.unique_name(nickname.strip(), owner), owner))
            game = self.engine.create(dict(name="外汇日记", initial=self.dashboard.initial_funds(), pairs=list(PAIRS), auto=True,
                user_binding={"type": "random", "values": [user_token]}))
            self.accounts.allow_trade(game["id"], True)
            self.db.execute("INSERT OR REPLACE INTO demo_profiles VALUES(?,?)",
                            (owner, dump(self.profile(owner) | setup | identity | {"avatar": avatar})))
            return web.json_response({"id": game["id"]})
        current = self.profile(owner)
        if action == "user-settings":
            if session["mode"] != "account":
                raise web.HTTPForbidden(reason="请先用账号密码登录")
            if request.method == "POST":
                nickname = body.get("nickname")
                if not isinstance(nickname, str) or not 1 <= len(nickname.strip()) <= 40:
                    raise ValueError("昵称须为 1～40 字")
                avatar = self.avatar_value(body.get("avatar") or current.get("avatar", "user-avatar.svg"))
                username = body.get("username")
                if username != self.accounts.username(owner) or body.get("new_password"):
                    await self.accounts.change_credentials(owner, request.cookies.get("kurumi_session", ""),
                        username, body.get("password"), body.get("new_password", ""))
                self.db.execute("UPDATE sim_users SET nickname=? WHERE id=?",
                                (self.unique_name(nickname.strip(), owner), owner))
                current = self.profile(owner) | {"avatar": avatar}
                self.db.execute("INSERT OR REPLACE INTO demo_profiles VALUES(?,?)", (owner, dump(current)))
            elif request.method != "GET":
                raise web.HTTPMethodNotAllowed(request.method, ["GET", "POST"])
            return web.json_response(dict(username=self.accounts.username(owner),
                nickname=self.db.execute("SELECT nickname FROM sim_users WHERE id=?", (owner,)).fetchone()[0],
                avatar=current.get("avatar", "user-avatar.svg")), headers={"Cache-Control":"no-store"})
        if action == "social-settings":
            if session["mode"] != "account":
                raise web.HTTPForbidden()
            if request.method not in ("GET", "POST"):
                raise web.HTTPMethodNotAllowed(request.method, ["GET", "POST"])
            config = social_config(current.get("social", {}))
            if request.method == "POST":
                config = social_config(body)
                self.db.execute("INSERT OR REPLACE INTO demo_profiles VALUES(?,?)",
                                (owner, dump(current | {"social": config})))
            return web.json_response(config)
        if action == "chat-debounce":
            if session["mode"] != "account":
                raise web.HTTPForbidden()
            if request.method not in ("GET", "POST"):
                raise web.HTTPMethodNotAllowed(request.method, ["GET", "POST"])
            config = debounce_config(body if request.method == "POST" else current.get("chat_debounce", {}))
            if request.method == "POST":
                self.db.execute("INSERT OR REPLACE INTO demo_profiles VALUES(?,?)",
                                (owner, dump(current | {"chat_debounce": config})))
            return web.json_response(config)
        if action == "chat-memory":
            if session["mode"] != "account":
                raise web.HTTPForbidden()
            config = {"enabled": True, "automatic": True} | current.get("chat_memory", {})
            subjects = {"self": self.db.execute(
                "SELECT nickname FROM sim_users WHERE id=?", (owner,)
            ).fetchone()[0]}
            for user in self.db.execute("SELECT id,nickname FROM sim_users WHERE id<>? ORDER BY nickname", (owner,)):
                subjects["user:" + user["id"]] = user["nickname"] + "（用户）"
                agent_name = self.profile(user["id"]).get("agent_name")
                if agent_name:
                    subjects["agent:" + user["id"]] = agent_name + "（智能体）"
            subjects.update(self.memory.character_subjects())
            models = [item for item in public_models(current) if item["kind"] == "chat"]
            if request.method == "POST":
                if body.get("action") == "settings":
                    model = body.get("model") or ""
                    if model and not any(item["id"] == model for item in models):
                        raise ValueError("请选择已配置的对话模型")
                    config = dict(enabled=body.get("enabled") is True,
                                  automatic=body.get("automatic") is True, model=model)
                    self.memory.cancel(owner)
                    self.db.execute("INSERT OR REPLACE INTO demo_profiles VALUES(?,?)",
                                    (owner, dump(current | {"chat_memory": config})))
                else:
                    self.memory.update(owner, body, subjects)
            elif request.method != "GET":
                raise web.HTTPMethodNotAllowed(request.method, ["GET", "POST"])
            if config["enabled"] and config["automatic"] and self.external.settings(owner)["mode"] == "platform":
                chosen = next((item for item in connections(current)
                               if item["id"] == config.get("model") and item.get("kind", "chat") == "chat"
                               and item.get("key")), selected(current))
                if chosen.get("key") and chosen.get("model"):
                    for owned_game in games:
                        self.memory.include_private_history(owner, owned_game["id"])
                    for stream, in self.db.execute("""SELECT stream FROM agent_memory_events
                        WHERE owner=? GROUP BY stream""", (owner,)).fetchall():
                        self.memory.start(owner, stream, chosen, self.model_url)
            return web.json_response(dict(config=config, entries=self.memory.list(owner), subjects=subjects,
                                          profiles=self.memory.profiles.list(owner),
                                          user_id=owner, progress=self.memory.progress(owner),
                                          agent=current.get("agent_name", "久留美"), days=MEMORY_DAYS,
                                          models=[{key: item[key] for key in ("id", "label")} for item in models]))
        if action == "stickers":
            if session["mode"] != "account":
                raise web.HTTPForbidden()
            if request.method == "POST":
                if body.get("action") == "settings":
                    probability = body.get("probability")
                    if type(probability) not in (float, int) or not 0 <= probability <= 100:
                        raise ValueError("发送概率须在 0～100% 之间")
                    current = current | {"sticker_probability": probability}
                    self.db.execute("INSERT OR REPLACE INTO demo_profiles VALUES(?,?)", (owner, dump(current)))
                else:
                    self.stickers.update(owner, body)
            elif request.method != "GET":
                raise web.HTTPMethodNotAllowed(request.method, ["GET", "POST"])
            return web.json_response({"stickers": self.stickers.list(owner),
                                      "categories": self.stickers.categories(owner),
                                      "probability": current.get("sticker_probability", 100)})
        if action == "models":
            if request.method == "POST":
                current = update_models(current, body, self.model_url)
                self.mode_values(self.external.settings(owner), current)
                self.db.execute("INSERT OR REPLACE INTO demo_profiles VALUES(?,?)", (owner, dump(current)))
            elif request.method != "GET":
                raise web.HTTPMethodNotAllowed(request.method, ["GET", "POST"])
            return web.json_response(dict(models=public_models(current), roles=current.get("model_roles", {}),
                                          policy=current.get("model_policy", {}), presets=MODEL_PRESETS))
        if action == "reply-settings" and request.method == "POST":
            if type(body.get("split_reply")) is not bool:
                raise ValueError("短句切分设置无效")
            minimum = body.get("split_delay_min", current.get("split_delay_min", 1.5))
            maximum = body.get("split_delay_max", current.get("split_delay_max", 3))
            if not (type(minimum) in (int, float) and type(maximum) in (int, float)
                    and 0 <= minimum <= maximum <= 60):
                raise ValueError("延迟范围须在 0～60 秒之间，且最短延迟不能超过最长延迟")
            pattern = body.get("split_pattern", "")
            if not isinstance(pattern, str) or len(pattern) > 240:
                raise ValueError("切分正则最多 240 个字符")
            self.db.execute("INSERT OR REPLACE INTO demo_profiles VALUES(?,?)",
                            (owner, dump(current | {"split_reply": body["split_reply"], "split_pattern": pattern,
                                                   "split_delay_min": minimum, "split_delay_max": maximum})))
            return web.json_response({"ok": True})
        if action == "agent-identity" and request.method == "GET":
            return web.json_response({
                "agent_name": current.get("agent_name") or "久留美",
                "agent_real_name": agent_real_name(current),
                "avatar": current.get("agent_avatar") or "avatar.png",
            }, headers={"Cache-Control": "no-store"})
        if action == "agent-identity" and request.method == "POST":
            body.setdefault("agent_avatar", current.get("agent_avatar") or "avatar.png")
            identity = self.agent_identity(body, owner)
            self.db.execute("INSERT OR REPLACE INTO demo_profiles VALUES(?,?)",
                            (owner, dump(current | {key: value for key, value in identity.items() if key != "split_reply"})))
            return web.json_response(identity | {"avatar": identity["agent_avatar"]})
        if action == "reports" and request.method == "GET":
            return web.json_response({"entries": self.reports(game)})
        if action == "preferences":
            values = self.preferences(owner, game)
            if request.method == "POST":
                interval, auto = body.get("interval"), body.get("auto")
                if type(interval) is not int or not 60 <= interval <= 86400 or type(auto) is not bool:
                    raise ValueError("看盘间隔须为 1～1440 分钟")
                values = dict(interval=interval, auto=auto, rest=rest_settings(body.get("rest")),
                              diary=diary_settings(body.get("diary")), notices=notice_settings(body.get("notices")))
                self.db.execute("INSERT OR REPLACE INTO demo_profiles VALUES(?,?)",
                                (owner, dump(dict(current, preferences=values))))
                if game:
                    self.db.execute("DELETE FROM demo_schedule WHERE game=?", (game["id"],))
            return web.json_response(values)
        if action == "profile" and request.method == "POST":
            if "models" in current and ({"url", "model", "key", "clear"} & body.keys()):
                raise ValueError("请在模型与 API 中更新模型连接")
            settings = {k: str(body.get(k, ""))[:4000].strip() for k in ("url", "model", "key") if k in body}
            if "agent_name" in body:
                settings["agent_name"] = self.validated_agent_name(body["agent_name"], owner)
                settings["agent_real_name"] = agent_real_name(current)
            if "agent_real_name" in body:
                settings["agent_real_name"] = self.validated_agent_real_name(body["agent_real_name"])
            if "user_relationship" in body:
                settings["user_relationship"] = self.validated_user_relationship(body["user_relationship"])
            if "persona" in body:
                settings["persona"] = self.validated_persona(body["persona"])
                if settings["persona"] == self.default_persona:
                    settings["persona"] = ""
            if "url" in settings:
                self.model_url(settings["url"])
                if (settings["url"] or MODEL_PRESETS[1]["url"]) != (current.get("url") or MODEL_PRESETS[1]["url"]) and current.get("key") and not settings.get("key"):
                    raise ValueError("更换接口地址时，请填写新接口的 Key，或先清除旧 Key")
            if body.get("clear") is True:
                settings["key"] = ""
            elif "key" in settings and not settings["key"]:
                settings.pop("key")
            mode = self.external.settings(owner)
            if mode["mode"] == "external" and (mode["auto"] or mode["diary"]):
                self.mode_values(mode, current | settings)
            self.db.execute("INSERT OR REPLACE INTO demo_profiles VALUES(?,?)", (owner, dump(current | settings)))
            return web.json_response({"ok": True})
        if action == "proactive-chat" and request.method == "GET":
            if session["mode"] != "account":
                raise web.HTTPForbidden()
            after = request.query.get("after", "0")
            if not after.isascii() or not after.isdecimal() or len(after) > 18:
                raise ValueError("消息编号无效")
            rows = self.db.execute("""SELECT id,content,created FROM demo_chat
                WHERE game=? AND proactive=1 AND id>? ORDER BY id LIMIT 20""",
                (game["id"], int(after))) if game else ()
            return web.json_response({"entries": [dict(row) for row in rows]},
                                     headers={"Cache-Control": "no-store"})
        if action == "chat" and request.method == "POST":
            if self.external.settings(owner)["mode"] == "external":
                raise web.HTTPForbidden(reason="当前由外部 Agent 负责对话")
            if not game:
                raise ValueError("请先创建模拟交易")
            message = str(body.get("message", "")).strip()
            user_image = community_image(body["image"]) if body.get("image") is not None else None
            if len(message) > 2000 or not (message or user_image):
                raise ValueError("请输入 1～2000 个字符或添加一张图片")
            reply, attachments, entry = await self.social.user_chat(game, message, user_image=user_image)
            return web.json_response({"reply": reply, "attachments": attachments, "entry": entry})
        if action == "chat-history" and request.method == "GET" and session["mode"] == "account" and game:
            before = int(request.query.get("before", "0"))
            if not 0 < before < 2**63:
                raise ValueError("私聊历史游标无效")
            rows = self.db.execute("""SELECT id,role,content,attachments,created FROM demo_chat
                WHERE game=? AND id<? ORDER BY id DESC LIMIT 21""", (game["id"], before)).fetchall()
            return web.json_response({
                "entries": [dict(row, attachments=json.loads(row["attachments"])) for row in reversed(rows[:20])],
                "has_more": len(rows) > 20,
            }, headers={"Cache-Control": "no-store"})
        if action == "chat-message" and request.method == "GET" and session["mode"] == "account" and game:
            entry = self.db.execute("""SELECT id,role,content,attachments,created FROM demo_chat
                WHERE game=? AND id=? AND role='assistant'""", (game["id"], request.query.get("id", ""))).fetchone()
            if not entry:
                raise web.HTTPNotFound(reason="私聊消息不存在")
            return web.json_response({"entry": dict(entry, attachments=json.loads(entry["attachments"]))},
                                     headers={"Cache-Control": "no-store"})
        if action == "state" and request.method == "GET":
            rows = self.db.execute("SELECT id,role,content,attachments,created FROM demo_chat WHERE game=? ORDER BY id DESC LIMIT 21",
                                   (game["id"],)).fetchall() if game else []
            return web.json_response(dict(avatar=current.get("avatar", "user-avatar.svg"),
                nickname=self.db.execute("SELECT nickname FROM sim_users WHERE id=?", (owner,)).fetchone()[0],
                agent_real_name=agent_real_name(current),
                split_delay_min=current.get("split_delay_min", 1.5),
                split_delay_max=current.get("split_delay_max", 3),
                agent_mode=current.get("agent_mode", "platform"), split_pattern=current.get("split_pattern", ""), split_reply=current.get("split_reply", True), agent_name=current.get("agent_name", "久留美"), agent_avatar=current.get("agent_avatar", "avatar.png"), state=public_snapshot(self, game["id"], 20) if game else None,
                chat=[dict(row, attachments=json.loads(row["attachments"])) for row in reversed(rows[:20])],
                chat_has_more=len(rows) > 20, profile={k: current.get(k, "")
                for k in ("persona", "url", "model")} | {"persona": persona_body(current.get("persona") or ""),
                    "user_relationship": current.get("user_relationship") or "朋友"},
                has_key=bool(selected(current).get("key")), demo=self.demo))
        raise web.HTTPNotFound()
