/* Group chat: list with unread, join/leave/dissolve, members with inferred presence, messages, mark-read, AI summary. */
import {h, clear, append, formatTime, clockTime, timeEl, relativeTime, uid, isAbort} from "/assets/ui/dom.js";
import {groupApi, aiApi} from "/assets/ui/api.js";
import {confirmAction, toast, stateBlock, skeleton, withBusy, bindCounter} from "/assets/ui/feedback.js";

const NAME_MAX = 40, MSG_MAX = 1500, FETCH = 80, ACTIVE_SECONDS = 600;

export function createGroups(ctx) {
  const root = h("div", {class: "gr-layout"});
  const listPanel = h("section", {class: "gr-list-panel", "aria-label": "群聊列表"});
  const roomBox = h("section", {class: "gr-room-box", "aria-label": "群聊内容"});
  const memberPanel = h("section", {class: "gr-members-panel", "aria-label": "群成员"});
  root.append(listPanel, roomBox, memberPanel);

  const s = {
    groups: [], listStatus: "idle", listError: null, room: "",
    detail: null, members: [], messages: [], detailStatus: "idle", detailError: null,
    unread: new Map(), summary: null, summaryStatus: "idle", summaryError: null,
  };
  const drafts = new Map();
  let listCtl = null, detailCtl = null, sig = "", roomMode = "", actionSig = "", started = false, tick = 0;
  const current = () => s.groups.find(group => String(group.id) === s.room) || null;

  /* ---------- list panel ---------- */
  const nameInput = h("input", {id: uid("group-name"), maxlength: String(NAME_MAX), autocomplete: "off", placeholder: "群聊名称，1～40 字", "aria-label": "新群聊名称"});
  const privateRoom = h("input", {type: "checkbox"});
  const createError = h("p", {class: "ui-field-error", role: "alert"});
  const createSubmit = h("button", {type: "submit", class: "ui-btn ui-btn-primary ui-btn-sm"}, "创建");
  const createCancel = h("button", {type: "button", class: "ui-btn ui-btn-quiet ui-btn-sm", onClick: () => toggleCreate(false)}, "取消");
  const createForm = h("form", {class: "gr-create", hidden: true, novalidate: true, onSubmit: async event => {
    event.preventDefault();
    createError.textContent = "";
    const name = nameInput.value.trim();
    if (!name) { createError.textContent = "请输入群聊名称"; nameInput.focus(); return; }
    if (name.length > NAME_MAX) { createError.textContent = `名称不超过 ${NAME_MAX} 字`; return; }
    try {
      const result = await withBusy(createSubmit, () => groupApi.create(name, privateRoom.checked), "创建中…");
      toast(`已创建「${name}」`, "success");
      nameInput.value = "";
      toggleCreate(false);
      await loadList({silent: true});
      selectRoom(result.id);
    } catch (error) { createError.textContent = error.message; }
  }}, nameInput, h("label", {}, privateRoom, " 私有群聊"), createError, h("div", {class: "gr-create-actions"}, createCancel, createSubmit));
  const createToggle = h("button", {type: "button", class: "ui-btn ui-btn-sm", "aria-expanded": "false", onClick: () => toggleCreate(createForm.hidden)}, "创建群聊");
  const listBody = h("div", {class: "gr-list-body"});
  listPanel.append(h("div", {class: "gr-list-head"}, h("h3", {}, "群聊"), createToggle), createForm, listBody);

  function toggleCreate(open) {
    createForm.hidden = !open;
    createToggle.setAttribute("aria-expanded", String(open));
    createError.textContent = "";
    if (open) nameInput.focus(); else createToggle.focus();
  }

  const sortedGroups = () => [...s.groups].sort((a, b) =>
    (Number(Boolean(b.joined)) - Number(Boolean(a.joined))) || ((b.last_activity || b.created || 0) - (a.last_activity || a.created || 0)));

  function groupRow(group) {
    const id = String(group.id);
    const unread = group.joined ? (s.unread.get(id) || 0) : 0;
    const button = h("button", {type: "button", class: "gr-item" + (id === s.room ? " is-active" : ""), "aria-current": id === s.room ? "true" : null,
      onClick: () => selectRoom(id)},
      h("span", {class: "gr-item-top"},
        h("strong", {class: "gr-item-name"}, group.name),
        unread > 0 ? h("span", {class: "ui-count", "aria-label": `${unread} 条未读`}, unread > 99 ? "99+" : String(unread)) : null),
      h("span", {class: "gr-item-meta"},
        `${group.members} 人`, " · ", group.last_activity ? "活跃 " + relativeTime(group.last_activity) : "暂无消息"),
      h("span", {class: "gr-item-meta"},
        group.mine ? h("span", {class: "ui-badge ui-badge-owner"}, "我是群主") : `群主 ${group.leader}`,
        !group.joined ? h("span", {class: "ui-badge ui-badge-muted"}, "未加入") : null));
    return button;
  }

  function renderList() {
    clear(listBody);
    if (s.listStatus === "loading") { listBody.append(stateBlock("loading", "正在加载群聊…"), skeleton(3)); return; }
    if (s.listStatus === "error") { listBody.append(stateBlock("error", s.listError?.message || "群聊列表加载失败", {retry: () => loadList(), hint: "请检查网络后重试。"})); return; }
    if (!s.groups.length) {
      listBody.append(stateBlock("empty", "还没有群聊", {hint: "点击「创建群聊」开一个，或等待他人创建。"}));
      return;
    }
    const sorted = sortedGroups();
    const mine = sorted.filter(group => group.joined), others = sorted.filter(group => !group.joined);
    if (mine.length) listBody.append(h("h4", {class: "gr-section"}, `已加入 · ${mine.length}`), h("div", {class: "gr-items"}, mine.map(groupRow)));
    if (others.length) listBody.append(h("h4", {class: "gr-section"}, `可加入 · ${others.length}`), h("div", {class: "gr-items"}, others.map(groupRow)));
  }

  function reportUnread() {
    let total = 0;
    for (const group of s.groups) if (group.joined) total += s.unread.get(String(group.id)) || 0;
    ctx.onUnread?.(total);
  }

  async function loadList({silent = false} = {}) {
    listCtl?.abort();
    const ctl = listCtl = new AbortController();
    if (!silent) { s.listStatus = s.groups.length ? "ready" : "loading"; s.listError = null; renderList(); }
    try {
      const data = await groupApi.list(ctl.signal);
      if (ctl !== listCtl) return;
      const before = current();
      s.groups = Array.isArray(data.groups) ? data.groups : [];
      s.unread.clear();
      for (const group of s.groups) {
        if (group.joined) s.unread.set(String(group.id), Number(group.unread) || 0);
      }
      s.listStatus = "ready";
      if (s.room && !current()) {
        if (before) toast(`「${before.name}」已不存在，可能已被解散`, "error");
        s.room = ""; s.detail = null; detailCtl?.abort();
        renderRoom(); renderMembers();
      } else if (before && Boolean(before.joined) !== Boolean(current()?.joined)) { renderRoom(); renderMembers(); }
      renderList();
      reportUnread();
    } catch (error) {
      if (isAbort(error) || ctl !== listCtl) return;
      if (silent) return;
      s.listStatus = "error"; s.listError = error; renderList();
    }
  }

  /* ---------- room view (built once so the composer keeps focus and text across polls) ---------- */
  const R = {};
  function buildRoom() {
    R.title = h("h2", {tabindex: "-1"});
    R.meta = h("p", {class: "gr-meta"});
    R.actions = h("div", {class: "gr-actions"});
    R.unread = h("div", {class: "gr-unread", role: "status"});
    R.summary = h("div", {class: "gr-summary", hidden: true});
    R.log = h("div", {class: "gr-log", role: "log", "aria-live": "polite", "aria-relevant": "additions", "aria-label": "群聊消息", tabindex: "0"});
    R.input = h("textarea", {rows: "3", maxlength: String(MSG_MAX), "aria-label": "消息内容", placeholder: "输入消息，Ctrl + Enter 发送"});
    R.count = h("span", {class: "ui-counter", "aria-hidden": "true"});
    bindCounter(R.input, R.count, MSG_MAX);
    R.error = h("p", {class: "ui-field-error", role: "alert"});
    R.send = h("button", {type: "submit", class: "ui-btn ui-btn-primary"}, "发送");
    R.input.addEventListener("keydown", event => { if ((event.ctrlKey || event.metaKey) && event.key === "Enter") R.form.requestSubmit(); });
    R.form = h("form", {class: "gr-composer", novalidate: true, onSubmit: async event => {
      event.preventDefault();
      R.error.textContent = "";
      const text = R.input.value.trim();
      if (!text) { R.error.textContent = "请输入消息内容"; R.input.focus(); return; }
      const id = s.room;
      try {
        await withBusy(R.send, () => groupApi.send(id, text), "发送中…");
        if (id !== s.room) return;
        R.input.value = ""; drafts.delete(id); R.count.textContent = `0 / ${MSG_MAX}`;
        await loadDetail({silent: true, stick: true});
        R.input.focus();
      } catch (error) {
        R.error.textContent = error.message;
        if (error.status === 403) loadList({silent: true});
      }
    }}, h("div", {class: "ui-label-row"}, h("span", {class: "ui-muted"}, "Enter 换行，Ctrl + Enter 发送"), R.count), R.input, R.error, h("div", {class: "cm-form-actions"}, R.send));
    R.view = h("div", {class: "gr-room"}, h("div", {class: "gr-head"}, h("div", {class: "gr-head-text"}, R.title, R.meta), R.actions), R.unread, R.summary, R.log, R.form);
  }

  function messageRow(message) {
    const mine = Boolean(message.mine);
    return h("article", {class: "gr-msg" + (mine ? " is-mine" : "")},
      h("div", {class: "gr-msg-head"}, h("img", {class: "gr-message-avatar", src: message.avatar?.startsWith("data:image/webp;base64,") ? message.avatar : "/assets/" + (message.avatar || "user-avatar.svg"), alt: ""}),
        h("strong", {}, mine ? "我" : message.nickname),
        message.is_agent ? h("span", {class: "ui-badge"}, "智能体") : null,
        h("time", {datetime: new Date(message.created * 1000).toISOString(), title: formatTime(message.created)}, clockTime(message.created))),
      h("p", {class: "gr-msg-body"}, message.body));
  }

  function renderLog(stick) {
    const el = R.log, group = s.detail;
    const keep = el.scrollTop;
    const wasBottom = el.scrollHeight - el.scrollTop - el.clientHeight < 60;
    clear(el);
    if (!s.messages.length) el.append(stateBlock("empty", "还没有消息", {hint: "发出第一条消息，打个招呼。"}));
    else {
      if (group.messages_count > s.messages.length)
        el.append(h("p", {class: "ui-muted gr-trunc"}, `仅显示最近 ${s.messages.length} 条，共 ${group.messages_count} 条（接口暂不支持加载更早消息）`));
      el.append(...s.messages.map(messageRow));
    }
    el.scrollTop = stick || wasBottom ? el.scrollHeight : keep;
  }

  function renderActions() {
    const group = s.detail, isOwner = group.role === "owner", unread = s.unread.get(s.room) || 0;
    const key = [isOwner, unread > 0, s.summaryStatus === "loading", Boolean(ctx.game())].join();
    if (key === actionSig) return;
    actionSig = key;
    const markRead = h("button", {type: "button", class: "ui-btn ui-btn-sm", disabled: unread === 0, onClick: event => markAllRead(event.currentTarget)}, "标记已读");
    const summary = h("button", {type: "button", class: "ui-btn ui-btn-sm", disabled: s.summaryStatus === "loading" || !ctx.game(),
      title: ctx.game() ? "" : "需要先创建模拟交易才能使用 AI 功能", onClick: () => runSummary()}, "AI 群聊摘要");
    const leave = isOwner
      ? h("button", {type: "button", class: "ui-btn ui-btn-sm ui-btn-danger-outline", onClick: dissolve}, "解散群聊")
      : h("button", {type: "button", class: "ui-btn ui-btn-sm ui-btn-danger-outline", onClick: leaveRoom}, "退出群聊");
    clear(R.actions).append(markRead, summary, leave);
  }

  function renderRoomParts({stick = false} = {}) {
    const group = s.detail, isOwner = group.role === "owner", unread = s.unread.get(s.room) || 0;
    R.title.textContent = group.name;
    append(clear(R.meta), [
      h("span", {class: "ui-badge " + (isOwner ? "ui-badge-owner" : "")}, isOwner ? "我是群主" : "成员"),
      ` ${group.members_count} 人 · ${group.messages_count} 条消息 · 最近活跃 `,
      s.messages.length ? timeEl(s.messages[s.messages.length - 1].created) : "暂无",
      group.muted ? h("span", {class: "ui-badge ui-badge-muted"}, "已静音") : null]);
    clear(R.unread).append(...(unread > 0 ? [
      h("span", {}, `你有 ${unread} 条未读消息`),
      h("button", {type: "button", class: "ui-btn ui-btn-sm", onClick: event => markAllRead(event.currentTarget)}, "全部标为已读")] : []));
    R.unread.hidden = unread === 0;
    renderActions();
    renderLog(stick);
  }

  async function markAllRead(button) {
    const id = s.room;
    try {
      await withBusy(button, () => groupApi.markRead(id), "标记中…");
      s.unread.set(id, 0); reportUnread(); renderList();
      toast("已标记为已读", "success");
      if (id === s.room) await loadDetail({silent: true});
    } catch (error) { toast("标记失败：" + error.message, "error"); }
  }

  async function leaveRoom() {
    const group = s.detail;
    const ok = await confirmAction({title: `退出「${group.name}」？`, message: "退出后将无法查看群消息，之后可以重新加入。",
      details: [`当前成员 ${group.members_count} 人`], confirmText: "退出群聊", tone: "danger"});
    if (!ok) return;
    try {
      await groupApi.act(group.id, "leave");
      toast(`已退出「${group.name}」`, "success");
      afterGroupGone(group.id);
    } catch (error) { toast("退出失败：" + error.message, "error"); }
  }

  async function dissolve() {
    const group = s.detail;
    const ok = await confirmAction({title: `解散「${group.name}」？`,
      message: "解散后全部成员会被移出，群消息会被永久删除，无法恢复。",
      details: [`成员 ${group.members_count} 人`, `消息 ${group.messages_count} 条`], confirmText: "解散群聊", tone: "danger"});
    if (!ok) return;
    try {
      await groupApi.act(group.id, "delete");
      toast(`「${group.name}」已解散`, "success");
      afterGroupGone(group.id);
    } catch (error) { toast("解散失败：" + error.message, "error"); }
  }

  function afterGroupGone(id) {
    s.groups = s.groups.filter(group => String(group.id) !== String(id));
    s.unread.delete(String(id)); drafts.delete(String(id));
    s.room = ""; s.detail = null; detailCtl?.abort();
    reportUnread(); renderList(); renderRoom(); renderMembers();
    loadList({silent: true});
    listPanel.querySelector(".gr-item")?.focus();
  }

  /* ---------- AI summary (generation only; nothing is posted to the group) ---------- */
  async function runSummary() {
    const id = s.room;
    if (!ctx.game()) { toast("需要先创建模拟交易才能使用 AI 功能", "error"); return; }
    s.summaryStatus = "loading"; s.summaryError = null; s.summary = null;
    renderSummary(); actionSig = ""; renderActions();
    try {
      const result = await aiApi.groupSummary(Number(id), ctx.game());
      if (id !== s.room) return;
      s.summary = result; s.summaryStatus = "ready";
    } catch (error) {
      if (id !== s.room) return;
      s.summaryStatus = "error"; s.summaryError = error;
    }
    actionSig = ""; renderSummary(); renderActions();
  }

  function renderSummary() {
    clear(R.summary);
    R.summary.hidden = s.summaryStatus === "idle";
    if (s.summaryStatus === "loading") { R.summary.append(stateBlock("loading", "AI 正在整理群聊内容…")); return; }
    if (s.summaryStatus === "error") {
      R.summary.append(stateBlock("error", s.summaryError?.message || "摘要生成失败", {retry: runSummary}),
        h("button", {type: "button", class: "ui-btn ui-btn-quiet ui-btn-sm", onClick: closeSummary}, "关闭"));
      return;
    }
    const data = s.summary || {};
    R.summary.append(h("div", {class: "ai-box ai-box-draft"},
      h("div", {class: "ai-tagrow"}, h("span", {class: "ui-badge ui-badge-ai"}, "AI 生成"), h("span", {class: "ui-muted"}, "仅供参考，不会自动发送到群聊")),
      h("p", {class: "ai-text"}, data.summary || "暂无摘要"),
      data.topics?.length ? h("div", {class: "cm-tags"}, h("span", {class: "ui-muted"}, "涉及品种"), data.topics.map(topic => h("span", {class: "ui-chip ui-chip-sm ui-chip-static"}, topic))) : null,
      data.risk_points?.length ? h("ul", {class: "ai-risks"}, data.risk_points.map(point => h("li", {}, point))) : null,
      h("p", {class: "ui-muted"}, `依据 ${data.messages ?? 0} 条公开消息生成`),
      h("div", {class: "ai-actions"},
        h("button", {type: "button", class: "ui-btn ui-btn-sm", onClick: async () => {
          try { await navigator.clipboard.writeText(data.summary || ""); toast("摘要已复制", "success"); }
          catch { toast("无法访问剪贴板，请手动选择文字复制", "error"); }
        }}, "复制摘要"),
        h("button", {type: "button", class: "ui-btn ui-btn-quiet ui-btn-sm", onClick: closeSummary}, "关闭"))));
  }
  function closeSummary() { s.summary = null; s.summaryStatus = "idle"; renderSummary(); actionSig = ""; renderActions(); }

  /* ---------- room container states ---------- */
  function setRoom(mode, node) {
    if (mode === "room" && roomMode === "room") return false;
    clear(roomBox).append(node);
    roomMode = mode;
    return true;
  }

  function joinPrompt(group) {
    const button = h("button", {type: "button", class: "ui-btn ui-btn-primary", onClick: async event => {
      try {
        const result = await withBusy(event.currentTarget, () => groupApi.act(group.id, "join"), "加入中…");
        if (result.pending) { toast("已提交入群申请"); await loadList({silent: true}); renderRoom(); return; }
        toast(`已加入「${group.name}」`, "success");
        group.joined = 1;
        await loadList({silent: true});
        if (String(group.id) === s.room) { renderRoom(); loadDetail(); }
      } catch (error) { toast("加入失败：" + error.message, "error"); }
    }, disabled: Boolean(group.pending)}, group.pending ? "等待群主处理" : group.private ? "申请加入" : "加入群聊");
    return h("div", {class: "gr-gate"},
      h("h2", {tabindex: "-1"}, group.name),
      h("p", {class: "gr-gate-lead"}, "你还不是这个群聊的成员，加入后才能查看和发送消息。"),
      h("dl", {class: "ui-dl"},
        h("dt", {}, "群主"), h("dd", {}, group.leader),
        h("dt", {}, "成员"), h("dd", {}, `${group.members} 人`),
        h("dt", {}, "消息"), h("dd", {}, `${group.messages} 条`),
        h("dt", {}, "最近活跃"), h("dd", {}, group.last_activity ? formatTime(group.last_activity) : "暂无消息"),
        h("dt", {}, "创建时间"), h("dd", {}, formatTime(group.created))),
      h("div", {class: "cm-form-actions"}, button),
      h("p", {class: "ui-muted"}, "加入后你可以随时退出。"));
  }

  function renderRoom({focus = false} = {}) {
    const group = current();
    if (!s.room) { setRoom("none", stateBlock("empty", "选择一个群聊开始交流", {hint: "左侧「已加入」的群聊可以直接聊天；「可加入」的群聊需要先加入。"})); return; }
    if (!group) {
      if (s.listStatus === "loading" || s.listStatus === "idle") setRoom("wait", stateBlock("loading", "正在读取群聊信息…"));
      else setRoom("gone", stateBlock("empty", "这个群聊不存在或已被解散"));
      return;
    }
    if (!group.joined) { setRoom("join", joinPrompt(group)); if (focus) roomBox.querySelector("h2")?.focus(); return; }
    if (s.detailStatus === "loading" && !s.detail) { setRoom("loading", h("div", {}, stateBlock("loading", "正在打开群聊…"), skeleton(4))); return; }
    if (s.detailStatus === "error") {
      setRoom("error", stateBlock("error", s.detailError?.message || "群聊加载失败", {retry: () => loadDetail(), hint: "可检查网络后重试。"}));
      return;
    }
    if (!s.detail) return;
    if (!R.view) buildRoom();
    const entered = setRoom("room", R.view);
    if (entered) { R.input.value = drafts.get(s.room) || ""; R.count.textContent = `${R.input.value.length} / ${MSG_MAX}`; R.error.textContent = ""; actionSig = ""; renderSummary(); }
    renderRoomParts({stick: entered});
    if (focus && entered) R.title.focus({preventScroll: true});
  }

  function memberAction(label, action, params) {
    return h("button", {type: "button", class: "ui-btn ui-btn-sm", onClick: async event => {
      const id = s.room;
      try {
        await withBusy(event.currentTarget, () => groupApi.act(id, action, params));
        await loadList({silent: true});
        if (s.room === id) { sig = ""; await loadDetail({silent: true}); }
      } catch (error) { toast(error.message, "error"); }
    }}, label);
  }

  async function manageRequests() {
    const id = s.room;
    try {
      const {requests} = await groupApi.act(id, "requests");
      for (const user of requests) {
        const row = h("p", {}, user.nickname, " ",
          memberAction("通过", "approve", {applicant: user.id}),
          memberAction("拒绝", "reject", {applicant: user.id}));
        if (s.room === id) memberPanel.append(row);
      }
      if (!requests.length) toast("暂无入群申请");
    } catch (error) { toast(error.message, "error"); }
  }

  function renderMembers() {
    clear(memberPanel);
    if (!s.room || !s.detail || s.detailStatus !== "ready" || !current()?.joined) {
      memberPanel.append(h("h3", {}, "成员"), h("p", {class: "ui-muted"}, s.room && current() && !current().joined ? "加入群聊后可查看成员。" : "选择已加入的群聊后显示成员。"));
      return;
    }
    const now = Date.now() / 1000;
    const lastSpoke = new Map();
    for (const message of s.messages) lastSpoke.set(message.nickname, Math.max(lastSpoke.get(message.nickname) || 0, message.created));
    const me = ctx.me();
    const rows = s.members.map(member => {
      const self = Boolean(me) && member.nickname === me;
      const spoke = lastSpoke.get(member.nickname) || 0;
      const state = self ? "self" : now - spoke < ACTIVE_SECONDS ? "active" : "quiet";
      const label = self ? "当前在线" : state === "active" ? "刚刚发言" : spoke ? "发言于 " + relativeTime(spoke) : "近期无发言";
      return h("li", {class: "gr-member"},
        h("span", {class: "gr-avatar", "aria-hidden": "true"}, Array.from(member.nickname)[0] || "?"),
        h("span", {class: "gr-member-text"},
          h("span", {class: "gr-member-name"}, member.nickname, self ? h("span", {class: "ui-muted"}, " （我）") : null),
          h("span", {class: "gr-member-sub"}, h("span", {class: "ui-presence", "data-state": state, "aria-hidden": "true"}), label)),
        h("span", {class: "gr-member-tags"},
          member.role === "owner" ? h("span", {class: "ui-badge ui-badge-owner"}, "群主") : h("span", {class: "ui-badge"}, "成员"),
          member.muted ? h("span", {class: "ui-badge ui-badge-muted"}, "静音") : null));
    });
    const group = current();
    const agent = memberAction(group?.agent_joined ? "移出我的智能体" : "邀请我的智能体", group?.agent_joined ? "remove-agent" : "invite-agent");
    const invite = h("button", {type: "button", class: "ui-btn ui-btn-sm", onClick: () => {
      const applicant = window.prompt("输入要邀请的成员昵称")?.trim();
      if (applicant) memberAction("邀请成员", "invite-member", {applicant}).click();
    }}, "邀请成员");
    const requests = h("button", {type: "button", class: "ui-btn ui-btn-sm", onClick: manageRequests}, "入群申请");
    memberPanel.append(h("h3", {}, `成员 · ${s.members.length}`),
      h("div", {class: "gr-actions"}, agent, group?.mine ? invite : null, group?.mine && group.private ? requests : null), h("ul", {class: "gr-members"}, rows),
      h("p", {class: "ui-muted gr-legend"}, "状态依据本页已加载的最近消息推断，服务器目前不提供实时在线数据。"));
  }

  async function loadDetail({silent = false, stick = false, focus = false} = {}) {
    const id = s.room;
    if (!id || !current()?.joined) return;
    detailCtl?.abort();
    const ctl = detailCtl = new AbortController();
    if (!silent) { s.detailStatus = "loading"; s.detailError = null; renderRoom(); renderMembers(); }
    try {
      const data = await groupApi.detail(id, FETCH, ctl.signal);
      if (ctl !== detailCtl || id !== s.room) return;
      const messages = [...(data.entries || [])].reverse();
      const signature = JSON.stringify([data.group, (data.members || []).map(m => [m.owner, m.role, m.muted]), messages.map(m => m.id)]);
      s.detail = data.group; s.members = data.members || []; s.messages = messages; s.detailStatus = "ready";
      s.unread.set(id, Number(data.group.unread) || 0);
      const entry = current();
      if (entry) {
        entry.members = data.group.members_count; entry.messages = data.group.messages_count;
        if (messages.length) entry.last_activity = messages[messages.length - 1].created;
      }
      reportUnread();
      if (silent && signature === sig && !stick) return;
      sig = signature;
      renderList(); renderRoom({focus}); renderMembers();
    } catch (error) {
      if (isAbort(error) || ctl !== detailCtl) return;
      if (error.status === 403) {
        const entry = current();
        if (entry) entry.joined = false;
        s.detail = null; s.detailStatus = "idle";
        if (!silent) toast("你已不在这个群聊中，请重新加入", "error");
        renderList(); renderRoom(); renderMembers();
        return;
      }
      if (error.status === 404) { toast("这个群聊已被解散", "error"); afterGroupGone(id); return; }
      if (silent) return;
      s.detailStatus = "error"; s.detailError = error; renderRoom(); renderMembers();
    }
  }

  function selectRoom(id) {
    ctx.onSelect?.();
    id = String(id);
    if (R.input && s.room && roomMode === "room") { if (R.input.value) drafts.set(s.room, R.input.value); else drafts.delete(s.room); }
    s.room = id; s.detail = null; s.members = []; s.messages = [];
    s.detailStatus = "idle"; s.detailError = null; s.summary = null; s.summaryStatus = "idle";
    sig = ""; actionSig = ""; roomMode = "";
    detailCtl?.abort();
    renderList(); renderRoom({focus: true}); renderMembers();
    if (current()?.joined) loadDetail({focus: true});
  }

  const visible = () => root.isConnected && root.getClientRects().length > 0 && !document.hidden;
  setInterval(() => {
    if (!started || !visible()) return;
    tick++;
    if (s.room && current()?.joined && s.detailStatus === "ready") loadDetail({silent: true});
    if (tick % 4 === 0) loadList({silent: true});
  }, 4000);

  function renderAll() { renderList(); renderRoom(); renderMembers(); }

  return {
    el: root, listPanel,
    activate() {
      if (!started) { started = true; renderAll(); loadList(); }
      else { loadList({silent: true}); if (s.room && current()?.joined) loadDetail({silent: true}); }
    },
    refresh: () => { if (!started) { started = true; renderAll(); return loadList(); } return loadList({silent: true}); },
    openRoom: id => { started = true; renderAll(); return loadList().then(() => selectRoom(id)); },
    unread: () => [...s.groups].reduce((sum, group) => sum + (group.joined ? s.unread.get(String(group.id)) || 0 : 0), 0),
  };
}
