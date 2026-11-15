/* Community workspace shell: forum / group chat / AI assistant behind one tab bar.
   Entry point kept as `mountCommunity(root, ctx)` so app.js keeps its import. */
import {h, clear} from "/assets/ui/dom.js";
import {loadProfile, cachedProfile} from "/assets/ui/session.js";
import {createForum} from "/assets/community/forum.js";
import {createGroups} from "/assets/community/groups.js";
import {createAi} from "/assets/community/ai.js";

const TABS = [["forum", "论坛"], ["groups", "群聊"], ["ai", "AI 助手"]];

export function mountCommunity(root, ctx = {}) {
  const me = () => cachedProfile()?.nickname || "";
  const base = {game: ctx.game || (() => ""), pairs: ctx.pairs || (() => []), me, onNavigate: ctx.onNavigate};
  let current = "forum", unread = 0, tabBadge, demoNote;

  const forum = createForum(base);
  const groups = createGroups({...base, onSelect: () => { if (!floating.hidden) showFloating(false); }, onUnread: total => {
    unread = total;
    renderBadge();
    ctx.onUnread?.(total);
  }});
  const ai = createAi({...base,
    openPost: id => { show("forum"); forum.openPost(id); },
    onPublished: () => forum.refresh()});
  const views = {forum, groups, ai};

  const buttons = {};
  const panels = {};
  const bar = h("div", {class: "cm-tabs", role: "tablist", "aria-label": "社区板块"});
  for (const [key, label] of TABS) {
    const badge = key === "groups" ? (tabBadge = h("span", {class: "ui-count", hidden: true})) : null;
    buttons[key] = h("button", {type: "button", role: "tab", id: "cm-tab-" + key, "aria-controls": "cm-panel-" + key, class: "cm-tab", onClick: () => show(key, {fromTab: true})}, label, badge);
    panels[key] = h("div", {role: "tabpanel", id: "cm-panel-" + key, "aria-labelledby": "cm-tab-" + key, class: "cm-panel-wrap", hidden: true}, views[key].el);
    bar.append(buttons[key]);
  }
  bar.addEventListener("keydown", event => {
    const at = TABS.findIndex(([key]) => key === current);
    const step = event.key === "ArrowRight" ? 1 : event.key === "ArrowLeft" ? -1 : 0;
    if (!step) return;
    event.preventDefault();
    const next = TABS[(at + step + TABS.length) % TABS.length][0];
    show(next); buttons[next].focus();
  });

  demoNote = h("span", {class: "ui-badge ui-badge-muted", hidden: true, title: "当前为离线演示数据，不连接真实行情"}, "离线演示");
  root.replaceChildren(h("section", {class: "cm-shell"},
    h("div", {class: "cm-header"}, h("div", {}, h("h2", {}, "社区"), h("p", {}, "论坛沉淀观点，群聊实时交流，AI 助手只生成草稿与摘要。")), demoNote),
    bar, ...Object.values(panels)));

  function renderBadge() {
    tabBadge.hidden = unread <= 0;
    tabBadge.textContent = unread > 99 ? "99+" : String(unread);
    tabBadge.setAttribute("aria-label", `${unread} 条未读消息`);
  }

  const floating = h("div", {class: "cm-floating", hidden: true});
  const title = h("strong", {}, "群聊列表");
  const close = h("button", {type: "button", class: "ui-btn ui-btn-sm", "aria-label": "关闭群聊窗口", onClick: () => {
    floating.hidden = true;
    syncList();
  }}, "×");
  const pin = h("button", {type: "button", class: "ui-btn ui-btn-sm", onClick: () => {
    floating.hidden = true;
    ctx.dock?.();
    show("groups");
  }}, "固定");
  const fullscreen = h("button", {type: "button", class: "ui-btn ui-btn-sm", onClick: () => {
    floating.classList.toggle("is-full");
    fullscreen.textContent = floating.classList.contains("is-full") ? "退出全屏" : "全屏";
  }}, "全屏");
  const header = h("header", {}, title, pin, fullscreen, close);
  floating.append(header);
  document.body.append(floating);
  let collapsed = false;
  const toggle = h("button", {type: "button", class: "community-sidebar-toggle", hidden: true,
    "aria-label": "展开或收起群聊列表", onClick: () => { collapsed = !collapsed; syncList(); }});
  ctx.sidebar?.after(toggle);
  header.addEventListener("pointerdown", event => {
    if (event.button !== 0 || event.target.closest("button") || floating.classList.contains("is-full")) return;
    const rect = floating.getBoundingClientRect();
    header.setPointerCapture(event.pointerId);
    const move = e => {
      floating.style.left = Math.max(0, Math.min(innerWidth - rect.width, rect.left + e.clientX - event.clientX)) + "px";
      floating.style.top = Math.max(0, Math.min(innerHeight - header.offsetHeight, rect.top + e.clientY - event.clientY)) + "px";
      floating.style.right = floating.style.bottom = "auto";
    };
    header.addEventListener("pointermove", move);
    header.addEventListener("lostpointercapture", () => header.removeEventListener("pointermove", move), {once: true});
  });
  function syncList() {
    const sidebar = floating.hidden && current === "groups" && !root.hidden && ctx.sidebar;
    document.body.classList.toggle("community-groups-active", Boolean(sidebar));
    document.body.classList.toggle("community-groups-collapsed", Boolean(sidebar && collapsed));
    toggle.hidden = !sidebar;
    toggle.setAttribute("aria-expanded", String(!collapsed));
    (sidebar || groups.el).prepend(groups.listPanel);
    groups.el.classList.toggle("external-list", Boolean(sidebar));
    if (floating.hidden) panels.groups.append(groups.el);
  }
  function showFloating(list) {
    floating.hidden = false;
    floating.classList.toggle("is-picker", list);
    title.textContent = list ? "群聊列表" : "群聊";
    pin.hidden = fullscreen.hidden = list;
    floating.append(groups.el);
    syncList();
    groups.activate();
  }

  function show(key, {fromTab = false} = {}) {
    const reselected = fromTab && key === current && !panels[key].hidden;
    current = key;
    for (const [name] of TABS) {
      const on = name === key;
      buttons[name].setAttribute("aria-selected", String(on));
      buttons[name].tabIndex = on ? 0 : -1;
      buttons[name].classList.toggle("is-active", on);
      panels[name].hidden = !on;
    }
    if (reselected) views[key].home?.();
    syncList();
    views[key].activate();
    ctx.onNavigate?.();
  }

  // Keep the unread badge fresh while the community page is closed.
  let warmed = false;
  const poll = () => { if (warmed && !document.hidden && root.hidden) groups.refresh(); };
  setInterval(poll, 45000);

  return {
    openFloating() { showFloating(true); },
    setVisible() { syncList(); },
    async open(tab = current) {
      floating.hidden = true;
      try {
        const profile = await loadProfile();
        demoNote.hidden = !profile.demo;
      } catch { /* nickname only improves "mine" markers; the pages still work */ }
      show(tab);
    },
    async openDraft(values, origin, review) {
      try { await loadProfile(); } catch { /* optional */ }
      show("ai");
      ai.gotoDraft(values, origin, review);
    },
    openPost(id, commentId) { show("forum"); return forum.openPost(id, commentId); },
    openGroup(id, messageId) { show("groups"); return groups.openRoom(id, messageId); },
    canGoBack: () => Boolean(views[current].canGoBack?.()),
    back() { views[current].home?.(); },
    warm() { warmed = true; return groups.refresh(); },
    unread: () => unread,
  };
}
