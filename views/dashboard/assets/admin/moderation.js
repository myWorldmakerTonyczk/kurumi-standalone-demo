/* Admin moderation: list posts/comments, pin/feature toggles, soft delete with an audit reason.
   GET/POST /api/community-moderation (admin bearer token). Admin page only. */
import {h, clear, formatTime, uid, isAbort} from "/assets/ui/dom.js";
import {api} from "/assets/ui/api.js";
import {confirmAction, toast, stateBlock, skeleton} from "/assets/ui/feedback.js";

const token = () => sessionStorage.getItem("kurumi-admin") || "";
const call = (body, signal) => api("community-moderation", {
  method: body ? "POST" : "GET", body: body || undefined, signal, headers: {Authorization: "Bearer " + token()},
});
const cut = (text, n) => (String(text).length > n ? String(text).slice(0, n) + "…" : String(text));

export function createModeration(root) {
  const s = {status: "idle", error: null, posts: [], comments: [], tab: "posts", filter: "active", keyword: ""};
  let ctl = null, busy = new Set();
  const idBase = uid("mod");

  const search = h("input", {type: "search", id: idBase + "-q", "aria-label": "按标题或作者筛选", placeholder: "按标题、正文或作者筛选", autocomplete: "off", maxlength: "80"});
  search.addEventListener("input", () => { s.keyword = search.value.trim().toLowerCase(); render(); });
  const tabs = [["posts", "帖子"], ["comments", "评论"]].map(([key, label]) =>
    h("button", {type: "button", class: "ui-seg-btn", onClick: () => { s.tab = key; render(); }}, label));
  const tabBox = h("div", {class: "ui-seg", role: "group", "aria-label": "审核对象"}, tabs);
  const filter = h("select", {id: idBase + "-f", "aria-label": "状态筛选", onChange: () => { s.filter = filter.value; render(); }},
    [["active", "仅正常"], ["deleted", "仅已删除"], ["all", "全部"]].map(([v, t]) => h("option", {value: v}, t)));
  const refresh = h("button", {type: "button", class: "ui-btn ui-btn-sm", onClick: () => load()}, "刷新");
  const body = h("div", {class: "mod-body", "aria-live": "polite"});
  const note = h("p", {class: "ui-muted"}, "最近 100 条帖子与评论。所有管理操作都会写入审计日志，删除为软删除。");

  root.replaceChildren(h("section", {class: "mod", "aria-label": "社区审核"},
    h("div", {class: "mod-bar"}, h("h3", {}, "社区审核"), tabBox, filter, search, refresh), note, body));

  const visible = rows => rows.filter(row => (s.filter === "all" || (s.filter === "deleted") === (row.status === "deleted"))
    && (!s.keyword || `${row.title ?? ""} ${row.body ?? ""} ${row.nickname}`.toLowerCase().includes(s.keyword)));

  async function act(action, row, label, reasonRequired) {
    if (busy.has(action + row.id)) return;
    const destructive = action.startsWith("delete");
    const result = await confirmAction({
      title: `${label}？`,
      message: destructive ? "此操作会隐藏该内容，并写入审计日志。" : "此操作会写入审计日志。",
      details: [`${action.endsWith("comment") ? "评论" : "帖子"} #${row.id}${row.title ? "：" + cut(row.title, 40) : ""}`, `作者：${row.nickname}`],
      confirmText: label, tone: destructive ? "danger" : "primary",
      reason: {label: reasonRequired ? "原因（必填，会记入审计日志）" : "原因（选填）", required: reasonRequired},
    });
    if (!result) return;
    busy.add(action + row.id);
    render();
    try {
      await call({action, id: row.id, reason: result.reason});
      toast(`${label}已完成`, "success");
      await load({silent: true});
    } catch (error) { toast(`${label}失败：${error.message}`, "error"); }
    finally { busy.delete(action + row.id); render(); }
  }

  function postRow(post) {
    const deleted = post.status === "deleted";
    const disabled = busy.has("delete-post" + post.id) || busy.has("pin-post" + post.id) || busy.has("feature-post" + post.id);
    return h("tr", {class: deleted ? "is-deleted" : ""},
      h("td", {class: "num"}, String(post.id)),
      h("th", {scope: "row", class: "mod-title"}, post.title),
      h("td", {}, post.nickname),
      h("td", {}, formatTime(post.created)),
      h("td", {}, deleted ? h("span", {class: "ui-badge ui-badge-muted"}, "已删除") : h("span", {class: "mod-flags"},
        post.pinned ? h("span", {class: "ui-badge ui-badge-pin"}, "置顶") : null,
        post.featured ? h("span", {class: "ui-badge ui-badge-feature"}, "精华") : null,
        !post.pinned && !post.featured ? h("span", {class: "ui-muted"}, "正常") : null)),
      h("td", {class: "mod-actions"}, deleted ? h("span", {class: "ui-muted"}, "—") : [
        h("button", {type: "button", class: "ui-btn ui-btn-sm", disabled, onClick: () => act("pin-post", post, post.pinned ? "取消置顶" : "置顶", false)}, post.pinned ? "取消置顶" : "置顶"),
        h("button", {type: "button", class: "ui-btn ui-btn-sm", disabled, onClick: () => act("feature-post", post, post.featured ? "取消精华" : "设为精华", false)}, post.featured ? "取消精华" : "设为精华"),
        h("button", {type: "button", class: "ui-btn ui-btn-sm ui-btn-danger-outline", disabled, onClick: () => act("delete-post", post, "删除帖子", true)}, "删除")]));
  }

  function commentRow(comment) {
    const deleted = comment.status === "deleted";
    const disabled = busy.has("delete-comment" + comment.id);
    return h("tr", {class: deleted ? "is-deleted" : ""},
      h("td", {class: "num"}, String(comment.id)),
      h("td", {class: "num"}, "#" + comment.post),
      h("th", {scope: "row", class: "mod-title"}, cut(comment.body, 90)),
      h("td", {}, comment.nickname),
      h("td", {}, formatTime(comment.created)),
      h("td", {}, deleted ? h("span", {class: "ui-badge ui-badge-muted"}, "已删除") : h("span", {class: "ui-muted"}, "正常")),
      h("td", {class: "mod-actions"}, deleted ? h("span", {class: "ui-muted"}, "—") :
        h("button", {type: "button", class: "ui-btn ui-btn-sm ui-btn-danger-outline", disabled, onClick: () => act("delete-comment", comment, "删除评论", true)}, "删除")));
  }

  function render() {
    tabs.forEach((button, i) => button.setAttribute("aria-pressed", String(["posts", "comments"][i] === s.tab)));
    clear(body);
    if (s.status === "loading" && !s.posts.length && !s.comments.length) { body.append(stateBlock("loading", "正在读取社区内容…"), skeleton(4)); return; }
    if (s.status === "error") { body.append(stateBlock("error", s.error?.message || "加载失败", {retry: () => load(), hint: s.error?.status === 401 ? "管理员登录已过期，请重新登录。" : ""})); return; }
    const rows = visible(s.tab === "posts" ? s.posts : s.comments);
    if (!rows.length) { body.append(stateBlock("empty", s.keyword || s.filter !== "all" ? "没有符合筛选条件的内容" : "社区里还没有内容")); return; }
    const heads = s.tab === "posts" ? ["编号", "标题", "作者", "发布时间", "状态", "操作"] : ["编号", "帖子", "内容", "作者", "发布时间", "状态", "操作"];
    body.append(h("div", {class: "hm-tablewrap"}, h("table", {class: "hm-table mod-table"},
      h("caption", {class: "sr-only"}, s.tab === "posts" ? "帖子审核列表" : "评论审核列表"),
      h("thead", {}, h("tr", {}, heads.map(t => h("th", {scope: "col"}, t)))),
      h("tbody", {}, rows.map(s.tab === "posts" ? postRow : commentRow)))));
  }

  async function load({silent = false} = {}) {
    ctl?.abort();
    const mine = ctl = new AbortController();
    if (!silent) { s.status = "loading"; render(); }
    try {
      const data = await call(null, mine.signal);
      if (mine !== ctl) return;
      s.posts = data.posts || []; s.comments = data.comments || []; s.status = "ready";
    } catch (error) {
      if (isAbort(error) || mine !== ctl) return;
      if (silent) { toast("刷新失败：" + error.message, "error"); return; }
      s.status = "error"; s.error = error;
    }
    render();
  }

  render();
  return {load: () => (s.status === "idle" ? load() : load({silent: true}))};
}