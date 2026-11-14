/* Forum: search, tag filter, latest/hot sort, paged list, thread view, compose, comment, delete. */
import {h, clear, append, formatTime, timeEl, parseTags, highlight, debounce, uid, isAbort} from "/assets/ui/dom.js";
import {forumApi} from "/assets/ui/api.js";
import {confirmAction, toast, stateBlock, skeleton, withBusy, bindCounter} from "/assets/ui/feedback.js";

const PAGE_SIZE = 20;
const HOT_CAP = 50; // backend caps limit at 50; hot ordering has no usable cursor, so "more" re-requests a larger limit.
const TITLE_MAX = 100, POST_MAX = 6000, COMMENT_MAX = 1500;

export function badgesFor(post) {
  const out = [];
  if (post.pinned) out.push(h("span", {class: "ui-badge ui-badge-pin"}, "置顶"));
  if (post.featured) out.push(h("span", {class: "ui-badge ui-badge-feature"}, "精华"));
  if (post.reference_type === "trade_review")
    out.push(h("span", {class: "ui-badge ui-badge-review", title: "引用了一次交易复盘"}, post.pair ? "交易复盘 · " + post.pair : "交易复盘"));
  return out;
}

export function createForum(ctx) {
  const root = h("div", {class: "cm-split"});
  const main = h("div", {class: "cm-main"});
  const side = h("section", {class: "cm-side", "aria-label": "发帖与帖子信息"});
  root.append(main, side);

  const s = {
    keyword: "", tag: "", sort: "latest", entries: [], next: null, hasMore: false, hotLimit: PAGE_SIZE, capped: false,
    status: "idle", error: null, tags: new Map(), counted: new Set(),
    view: "list", postId: "", post: null, comments: [], threadStatus: "idle", threadError: null, lastOpened: "", newId: "",
  };
  let listCtl = null, threadCtl = null, threadSig = "", started = false;

  /* ---------- list view ---------- */
  const search = h("input", {type: "search", id: uid("forum-search"), placeholder: "搜索标题或正文", maxlength: "80", autocomplete: "off", "aria-label": "搜索帖子"});
  const sortButtons = [["latest", "最新"], ["hot", "热门"]].map(([value, label]) =>
    h("button", {type: "button", class: "ui-seg-btn", "data-sort": value, onClick: () => setSort(value)}, label));
  const sortBox = h("div", {class: "ui-seg", role: "group", "aria-label": "排序方式"}, sortButtons);
  const tagBox = h("div", {class: "cm-tagbar", role: "group", "aria-label": "按标签筛选"});
  const tagListId = uid("forum-tags");
  const tagList = h("datalist", {id: tagListId});
  const tagInput = h("input", {type: "text", class: "cm-taginput", list: tagListId, maxlength: "40", placeholder: "输入标签后回车", autocomplete: "off", "aria-label": "按标签名筛选"});
  tagInput.addEventListener("keydown", event => {
    if (event.key !== "Enter") return;
    event.preventDefault();
    const value = tagInput.value.trim();
    tagInput.value = "";
    if (value && value !== s.tag) { s.tag = value; loadList(); }
  });
  const filterNote = h("p", {class: "cm-filter-note", role: "status"});
  const listBox = h("div", {class: "cm-list", "aria-live": "polite"});
  const moreBox = h("div", {class: "cm-more"});
  const listView = h("section", {"aria-label": "帖子列表", class: "cm-listview"},
    h("div", {class: "cm-toolbar"}, h("div", {class: "cm-search"}, search), sortBox), tagBox, filterNote, listBox, moreBox);

  let composing = false;
  const runSearch = debounce(() => {
    const value = search.value.trim();
    if (value === s.keyword) return;
    s.keyword = value;
    loadList();
  }, 320);
  search.addEventListener("input", () => { if (!composing) runSearch(); });
  search.addEventListener("compositionstart", () => { composing = true; });
  search.addEventListener("compositionend", () => { composing = false; runSearch(); });
  search.addEventListener("keydown", event => {
    if (event.key === "Enter") { event.preventDefault(); runSearch.cancel(); s.keyword = search.value.trim(); loadList(); }
  });

  function setSort(value) {
    if (s.sort === value) return;
    s.sort = value;
    loadList();
  }
  function setTag(tag) {
    s.tag = s.tag === tag ? "" : tag;
    loadList();
  }
  function clearFilters() {
    s.keyword = ""; s.tag = ""; search.value = "";
    loadList();
  }
  function rememberTags(entries) {
    for (const post of entries) {
      if (s.counted.has(post.id)) continue;
      s.counted.add(post.id);
      for (const tag of post.tags || []) s.tags.set(tag, (s.tags.get(tag) || 0) + 1);
    }
  }

  function renderControls() {
    for (const button of sortButtons) button.setAttribute("aria-pressed", String(button.dataset.sort === s.sort));
    const tags = [...s.tags.entries()].sort((a, b) => b[1] - a[1] || a[0].localeCompare(b[0], "zh-CN")).map(([tag]) => tag).slice(0, 14);
    if (s.tag && !tags.includes(s.tag)) tags.unshift(s.tag);
    const keep = tagBox.contains(document.activeElement) ? (document.activeElement === tagInput ? "@input" : document.activeElement.dataset.chip) : null;
    clear(tagBox);
    clear(tagList).append(...[...s.tags.keys()].map(tag => h("option", {value: tag})));
    tagBox.append(h("span", {class: "cm-tagbar-label"}, "标签"),
      h("button", {type: "button", class: "ui-chip", "data-chip": "@all", "aria-pressed": String(!s.tag), onClick: () => { if (s.tag) setTag(s.tag); }}, "全部"),
      ...tags.map(tag => h("button", {type: "button", class: "ui-chip", "data-chip": tag, "aria-pressed": String(s.tag === tag), onClick: () => setTag(tag)}, tag)),
      tagInput, tagList);
    if (keep) (keep === "@input" ? tagInput : [...tagBox.querySelectorAll("[data-chip]")].find(el => el.dataset.chip === keep))?.focus();
    const active = [];
    if (s.keyword) active.push(`关键词「${s.keyword}」`);
    if (s.tag) active.push(`标签「${s.tag}」`);
    clear(filterNote);
    if (active.length) {
      filterNote.append(`当前筛选：${active.join(" · ")}`, " ",
        h("button", {type: "button", class: "ui-link", onClick: clearFilters}, "清除筛选"));
    }
  }

  function postRow(post) {
    const open = () => openPost(post.id);
    const title = h("button", {type: "button", class: "cm-post-title", onClick: open, "aria-label": "查看帖子：" + post.title}, highlight(post.title, s.keyword));
    const row = h("article", {class: "cm-post" + (String(post.id) === s.newId ? " is-new" : ""), "data-id": String(post.id)},
      h("div", {class: "cm-post-head"}, badgesFor(post), h("h3", {}, title)),
      h("p", {class: "cm-post-body"}, highlight(post.body, s.keyword)),
      h("div", {class: "cm-post-meta"},
        h("span", {}, post.nickname), h("span", {class: "ui-dot", "aria-hidden": "true"}, "·"),
        h("span", {}, "发布 ", timeEl(post.created)), h("span", {class: "ui-dot", "aria-hidden": "true"}, "·"),
        h("span", {}, "最近活跃 ", timeEl(post.last_activity)), h("span", {class: "ui-dot", "aria-hidden": "true"}, "·"),
        h("span", {class: "cm-count"}, `${post.comments} 条评论`)),
      post.tags?.length ? h("div", {class: "cm-tags"}, post.tags.map(tag =>
        h("button", {type: "button", class: "ui-chip ui-chip-sm", "aria-pressed": String(s.tag === tag), "aria-label": "筛选标签：" + tag, onClick: event => { event.stopPropagation(); setTag(tag); }}, tag))) : null);
    row.addEventListener("click", event => { if (!event.target.closest("button, a")) open(); });
    return row;
  }

  function renderList() {
    renderControls();
    clear(listBox); clear(moreBox);
    listBox.setAttribute("aria-busy", String(s.status === "loading"));
    if (s.status === "loading") { listBox.append(stateBlock("loading", "正在加载帖子…"), skeleton(4)); return; }
    if (s.status === "error") { listBox.append(stateBlock("error", s.error?.message || "帖子加载失败", {retry: () => loadList(), hint: "可检查网络后重试，已有内容不受影响。"})); return; }
    if (!s.entries.length) {
      const filtered = s.keyword || s.tag;
      const block = stateBlock("empty", filtered ? "没有符合条件的帖子" : "论坛里还没有帖子",
        {hint: filtered ? "换个关键词或标签试试。" : "在右侧写下第一篇交流，或者去 AI 助手生成一份复盘草稿。"});
      if (filtered) block.append(h("button", {type: "button", class: "ui-btn", onClick: clearFilters}, "清除筛选"));
      listBox.append(block);
      return;
    }
    listBox.append(...s.entries.map(postRow));
    if (s.status === "loadingMore") moreBox.append(stateBlock("loading", "正在加载更多…"));
    else if (s.hasMore && !s.capped) moreBox.append(h("button", {type: "button", class: "ui-btn", onClick: () => loadList({more: true})}, "加载更多"));
    else if (s.capped) moreBox.append(h("p", {class: "ui-muted"}, `热门榜单最多展示前 ${HOT_CAP} 条。想看更早的内容，请切换到「最新」。`));
    else moreBox.append(h("p", {class: "ui-muted"}, `已显示全部 ${s.entries.length} 篇`));
  }

  async function loadList({more = false} = {}) {
    listCtl?.abort();
    const ctl = listCtl = new AbortController();
    const hot = s.sort === "hot";
    s.status = more ? "loadingMore" : "loading";
    if (!more) s.error = null;
    renderList();
    const limit = hot ? Math.min(HOT_CAP, more ? s.hotLimit + PAGE_SIZE : PAGE_SIZE) : PAGE_SIZE;
    try {
      const data = await forumApi.list({keyword: s.keyword, tag: s.tag, sort: s.sort, limit, before: !hot && more ? s.next : ""}, ctl.signal);
      if (ctl !== listCtl) return;
      const incoming = Array.isArray(data.entries) ? data.entries : [];
      if (hot) { s.entries = incoming; s.hotLimit = limit; }
      else if (more) {
        const seen = new Set(s.entries.map(post => post.id));
        s.entries = s.entries.concat(incoming.filter(post => !seen.has(post.id))); // pinned posts repeat on every page
      } else s.entries = incoming;
      s.next = data.next_before ?? null;
      s.hasMore = Boolean(data.has_more);
      s.capped = hot && limit >= HOT_CAP && s.hasMore;
      rememberTags(incoming);
      s.status = "ready";
    } catch (error) {
      if (isAbort(error) || ctl !== listCtl) return;
      if (more) { s.status = "ready"; toast(error.message, "error"); }
      else { s.status = "error"; s.error = error; }
    }
    renderList();
    if (s.newId) {
      const row = listBox.querySelector(`[data-id="${CSS.escape(s.newId)}"]`);
      row?.scrollIntoView({block: "nearest"});
      setTimeout(() => { s.newId = ""; row?.classList.remove("is-new"); }, 3500);
    }
  }

  /* ---------- compose (side panel in list mode) ---------- */
  function composePanel() {
    const titleId = uid("post-title"), tagsId = uid("post-tags"), bodyId = uid("post-body");
    const title = h("input", {id: titleId, name: "title", maxlength: String(TITLE_MAX), required: true, autocomplete: "off"});
    const tags = h("input", {id: tagsId, name: "tags", placeholder: "用逗号分隔，最多 10 个", autocomplete: "off"});
    const body = h("textarea", {id: bodyId, name: "body", rows: "6", maxlength: String(POST_MAX), required: true});
    const count = h("span", {class: "ui-counter", "aria-hidden": "true"});
    bindCounter(body, count, POST_MAX);
    const error = h("p", {class: "ui-field-error", role: "alert"});
    const submit = h("button", {type: "submit", class: "ui-btn ui-btn-primary"}, "发布帖子");
    const form = h("form", {class: "cm-compose", novalidate: true, onSubmit: async event => {
      event.preventDefault();
      error.textContent = "";
      const values = {title: title.value.trim(), body: body.value.trim()};
      const parsed = parseTags(tags.value);
      const problem = !values.title ? "请填写标题" : !values.body ? "请填写正文" : parsed.error;
      if (problem) { error.textContent = problem; (!values.title ? title : !values.body ? body : tags).focus(); return; }
      try {
        const result = await withBusy(submit, () => forumApi.create({title: values.title, body: values.body, tags: parsed.tags}), "发布中…");
        form.reset(); count.textContent = `0 / ${POST_MAX}`;
        toast("帖子已发布", "success");
        s.keyword = ""; s.tag = ""; s.sort = "latest"; search.value = ""; s.newId = String(result.id);
        await loadList();
      } catch (failure) { error.textContent = failure.message; toast("发布失败：" + failure.message, "error"); }
    }},
      h("label", {for: titleId}, "标题", title),
      h("label", {for: tagsId}, "标签（选填）", tags),
      h("label", {for: bodyId}, h("span", {class: "ui-label-row"}, "正文", count), body),
      error,
      h("p", {class: "ui-muted"}, "发布后所有社区成员可见。只有你自己能删除这篇帖子。"),
      submit);
    return h("section", {class: "cm-panel"}, h("h3", {}, "发布帖子"), form);
  }
  let composeEl = null;

  /* ---------- thread view ---------- */
  const T = {};
  const isMine = post => Boolean(ctx.me()) && post?.nickname === ctx.me();

  function buildThread() {
    T.back = h("button", {type: "button", class: "ui-btn ui-btn-quiet", onClick: backToList}, "← 返回帖子列表");
    T.head = h("div", {class: "cm-thread-head"});
    T.commentsHead = h("h3", {class: "cm-comments-title"}, "评论");
    T.comments = h("div", {class: "cm-comments", "aria-live": "polite"});
    const area = h("textarea", {name: "body", rows: "3", maxlength: String(COMMENT_MAX), required: true, "aria-label": "评论内容", placeholder: "写下你的看法或补充…"});
    const count = h("span", {class: "ui-counter", "aria-hidden": "true"});
    bindCounter(area, count, COMMENT_MAX);
    const error = h("p", {class: "ui-field-error", role: "alert"});
    const submit = h("button", {type: "submit", class: "ui-btn ui-btn-primary"}, "发表评论");
    area.addEventListener("keydown", event => { if ((event.ctrlKey || event.metaKey) && event.key === "Enter") form.requestSubmit(); });
    const form = h("form", {class: "cm-comment-form", novalidate: true, onSubmit: async event => {
      event.preventDefault();
      error.textContent = "";
      const text = area.value.trim();
      if (!text) { error.textContent = "请填写评论内容"; area.focus(); return; }
      try {
        await withBusy(submit, () => forumApi.comment(s.postId, {body: text}), "发送中…");
        area.value = ""; count.textContent = `0 / ${COMMENT_MAX}`;
        toast("评论已发表", "success");
        await loadThread({silent: true});
        T.comments.lastElementChild?.scrollIntoView({block: "nearest"});
      } catch (failure) { error.textContent = failure.message; toast("评论失败：" + failure.message, "error"); }
    }}, h("div", {class: "ui-label-row"}, h("span", {}, "发表评论"), count), area, error,
      h("div", {class: "cm-form-actions"}, h("span", {class: "ui-muted"}, "Ctrl + Enter 快速发送"), submit));
    T.form = form;
    T.view = h("section", {class: "cm-thread", "aria-label": "帖子详情"}, T.back, T.head, T.commentsHead, T.comments, form);
    T.info = h("section", {class: "cm-panel"});
  }

  function commentRow(comment) {
    const remove = comment.mine ? h("button", {type: "button", class: "ui-btn ui-btn-quiet ui-btn-sm ui-danger-text", onClick: () => removeComment(comment)}, "删除") : null;
    return h("article", {class: "cm-comment" + (comment.mine ? " is-mine" : "")},
      h("div", {class: "cm-comment-head"}, h("strong", {}, comment.nickname), comment.mine ? h("span", {class: "ui-badge"}, "我") : null, timeEl(comment.created), remove),
      h("p", {}, comment.body));
  }

  function renderThread() {
    const post = s.post;
    if (s.threadStatus === "loading" && !post) {
      clear(T.head).append(stateBlock("loading", "正在打开帖子…"));
      clear(T.comments); T.commentsHead.hidden = true; T.form.hidden = true; clear(T.info);
      return;
    }
    if (s.threadStatus === "error" || s.threadStatus === "missing") {
      const missing = s.threadStatus === "missing";
      clear(T.head).append(stateBlock(missing ? "empty" : "error", missing ? "这篇帖子不存在或已被删除" : (s.threadError?.message || "帖子加载失败"),
        missing ? {hint: "返回列表查看最新内容。"} : {retry: () => loadThread(), hint: "可检查网络后重试。"}));
      clear(T.comments); T.commentsHead.hidden = true; T.form.hidden = true; clear(T.info);
      return;
    }
    if (!post) return;
    T.commentsHead.hidden = false; T.form.hidden = false;
    const ref = post.reference_type === "trade_review" ? h("p", {class: "cm-ref"},
      h("strong", {}, "引用交易复盘"), post.pair ? ` · ${post.pair}` : "",
      Number(post.reference_id) > 0 ? ` · 复盘截止 ${formatTime(post.reference_id)}` : "",
      h("span", {class: "ui-muted"}, " 数据来自作者的模拟账本，不代表真实收益。")) : null;
    const title = h("h2", {tabindex: "-1", id: "cm-thread-title"}, post.title);
    append(clear(T.head), [
      h("div", {class: "cm-post-head"}, badgesFor(post)), title,
      h("div", {class: "cm-post-meta"}, h("span", {}, post.nickname), h("span", {class: "ui-dot", "aria-hidden": "true"}, "·"),
        h("span", {title: formatTime(post.created)}, "发布于 ", timeEl(post.created))),
      ref, h("div", {class: "cm-post-full"}, post.body),
      post.tags?.length ? h("div", {class: "cm-tags"}, post.tags.map(tag =>
        h("button", {type: "button", class: "ui-chip ui-chip-sm", "aria-label": "查看标签：" + tag, onClick: () => { s.tag = tag; backToList({reload: true}); }}, tag))) : null]);
    T.commentsHead.textContent = `评论（${s.comments.length}）`;
    clear(T.comments);
    if (!s.comments.length) T.comments.append(stateBlock("empty", "还没有评论", {hint: "来写第一条吧。"}));
    else T.comments.append(...s.comments.map(commentRow));
    const mine = isMine(post);
    clear(T.info).append(h("h3", {}, "帖子信息"),
      h("dl", {class: "ui-dl"},
        h("dt", {}, "作者"), h("dd", {}, post.nickname, mine ? " （我）" : ""),
        h("dt", {}, "发布时间"), h("dd", {}, formatTime(post.created)),
        h("dt", {}, "评论"), h("dd", {}, `${s.comments.length} 条`)),
      mine ? h("div", {class: "cm-danger-zone"}, h("p", {class: "ui-muted"}, "删除后帖子和全部评论都会从社区移除。"),
        h("button", {type: "button", class: "ui-btn ui-btn-danger-outline", onClick: removePost}, "删除帖子"))
        : h("p", {class: "ui-muted"}, "只有作者可以删除帖子。"));
  }

  async function loadThread({silent = false} = {}) {
    threadCtl?.abort();
    const ctl = threadCtl = new AbortController();
    if (!silent) { s.threadStatus = "loading"; s.threadError = null; renderThread(); }
    try {
      const data = await forumApi.thread(s.postId, ctl.signal);
      if (ctl !== threadCtl) return;
      const comments = [...(data.entries || [])].reverse(); // backend sends newest first
      const sig = JSON.stringify([data.post?.id, data.post?.title, comments.map(c => c.id), data.post?.pinned, data.post?.featured]);
      s.post = data.post; s.comments = comments; s.threadStatus = "ready";
      if (silent && sig === threadSig) return;
      threadSig = sig;
      renderThread();
      renderSide();
    } catch (error) {
      if (isAbort(error) || ctl !== threadCtl) return;
      if (silent) return;
      s.threadStatus = error.status === 404 ? "missing" : "error";
      s.threadError = error;
      renderThread();
    }
  }

  async function openPost(id) {
    s.view = "thread"; s.postId = String(id); s.lastOpened = String(id);
    s.post = null; s.comments = []; threadSig = "";
    if (!T.view) buildThread();
    renderAll();
    await loadThread();
    root.querySelector("#cm-thread-title")?.focus({preventScroll: true});
    T.view.scrollIntoView({block: "start"});
  }

  function backToList({reload = false} = {}) {
    threadCtl?.abort();
    s.view = "list";
    renderAll();
    if (reload || s.status === "idle") loadList();
    const target = listBox.querySelector(`[data-id="${CSS.escape(s.lastOpened)}"] .cm-post-title`);
    (target || search).focus({preventScroll: true});
  }

  async function removeComment(comment) {
    const confirmed = await confirmAction({title: "删除这条评论？", message: "删除后无法恢复。",
      details: [`评论内容：${comment.body.length > 60 ? comment.body.slice(0, 60) + "…" : comment.body}`], confirmText: "删除评论", tone: "danger"});
    if (!confirmed) return;
    try {
      await forumApi.removeComment(comment.id);
      toast("评论已删除", "success");
      await loadThread({silent: true});
    } catch (error) { toast("删除失败：" + error.message, "error"); }
  }

  async function removePost() {
    const post = s.post;
    if (!post) return;
    const confirmed = await confirmAction({title: "删除这篇帖子？",
      message: "帖子及其全部评论会从社区移除，无法恢复。",
      details: [`标题：${post.title}`, `评论：${s.comments.length} 条`], confirmText: "删除帖子", tone: "danger"});
    if (!confirmed) return;
    try {
      await forumApi.removePost(post.id);
      toast("帖子已删除", "success");
      s.entries = s.entries.filter(item => String(item.id) !== String(post.id));
      s.lastOpened = "";
      backToList({reload: true});
    } catch (error) { toast("删除失败：" + error.message, "error"); }
  }

  /* ---------- assembly ---------- */
  function renderSide() {
    clear(side);
    if (s.view === "list") { composeEl ||= composePanel(); side.append(composeEl); }
    else if (T.info) side.append(T.info);
  }
  function renderAll() {
    clear(main);
    if (s.view === "list") { main.append(listView); renderList(); }
    else { main.append(T.view); renderThread(); }
    renderSide();
  }

  setInterval(() => { if (!root.closest("[hidden]") && root.isConnected && !document.hidden && s.view === "thread" && s.threadStatus === "ready") loadThread({silent: true}); }, 10000);

  return {
    el: root,
    activate() {
      if (!started) { started = true; renderAll(); loadList(); }
    },
    openPost(id) { started = true; return openPost(id); },
    /** Re-selecting the already-open 论坛 tab returns from a thread to the list. */
    home() { if (s.view === "thread") backToList(); },
    refresh: () => (s.view === "thread" ? loadThread() : loadList()),
  };
}