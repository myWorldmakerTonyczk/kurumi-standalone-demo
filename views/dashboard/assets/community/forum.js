/* Forum: search, tag filter, latest/hot sort, paged list, thread view, compose, comment, delete. */
import {h, clear, append, formatTime, timeEl, parseTags, highlight, debounce, uid, isAbort} from "/assets/ui/dom.js";
import {forumApi} from "/assets/ui/api.js";
import {prepareImage} from "/assets/community/media.js";
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

  function authorAvatar(author) {
    const fallback = author.is_agent ? "avatar.png" : "user-avatar.svg";
    const avatar = author.avatar || fallback;
    const src = avatar.startsWith("data:image/webp;base64,") ? avatar : "/assets/" +
      (["avatar.png", "neutral.png", "laugh.png", "user-avatar.svg"].includes(avatar) ? avatar : fallback);
    return h("img", {class: "cm-author-avatar", src, alt: "", width: 28, height: 28, loading: "lazy"});
  }

  function renderMedia(media, inTitle = false) {
    if (!["image", "sticker"].includes(media?.kind) || !media.src?.startsWith("data:image/webp;base64,")) return null;
    const style = media.kind === "sticker" ? " is-sticker" : "";
    return h("img", {class: "cm-media-img" + style + (inTitle ? " is-title" : ""),
      src: media.src, alt: media.label || "图片", loading: "lazy"});
  }

  let stickerLibrary;
  function getStickers() {
    stickerLibrary ||= fetch("/api/stickers", {cache: "no-store"}).then(async response => {
      if (!response.ok) throw Error("无法读取表情包库");
      return (await response.json()).stickers || [];
    }).catch(error => { stickerLibrary = null; throw error; });
    return stickerLibrary;
  }

  function mediaInput(field, label, error) {
    let media = null, revision = 0, pending = false;
    const preview = h("div", {class: "cm-media-preview", hidden: true});
    const menu = h("select", {hidden: true, "aria-label": label + "表情包"});
    const pick = h("input", {type: "file", hidden: true, accept: "image/*", "aria-label": label + "图片"});
    function set(value) {
      media = value;
      preview.hidden = !media;
      preview.replaceChildren();
      if (media) preview.append(renderMedia(media, true),
        h("button", {type: "button", class: "ui-btn ui-btn-quiet ui-btn-sm", onClick: clearMedia}, "移除"));
    }
    function clearMedia() {
      revision++;
      pending = false;
      pick.value = "";
      menu.hidden = true;
      menu.replaceChildren();
      set(null);
    }
    async function loadImage(file) {
      if (!file) return;
      const token = ++revision;
      pending = true;
      error.textContent = "";
      try {
        const src = await prepareImage(file);
        if (token === revision) set({kind: "image", src});
      } catch (failure) {
        if (token === revision) error.textContent = failure.message || "图片无法读取";
      } finally {
        if (token === revision) pending = false;
      }
    }
    pick.onchange = () => { loadImage(pick.files[0]); pick.value = ""; };
    field.addEventListener("dragover", event => {
      if (event.dataTransfer.types.includes("Files")) event.preventDefault();
    });
    field.addEventListener("drop", event => {
      const file = event.dataTransfer.files[0];
      if (!file) return;
      event.preventDefault();
      loadImage(file);
    });
    const stickers = h("button", {type: "button", class: "ui-btn ui-btn-quiet ui-btn-sm", onClick: async () => {
      if (!menu.hidden) { menu.hidden = true; return; }
      try {
        const token = revision;
        const items = await getStickers();
        if (token !== revision) return;
        menu.replaceChildren(h("option", {value: ""}, items.length ? "选择一个表情包" : "表情包库为空"),
          ...items.map(item => h("option", {value: item.id}, item.label)));
        menu.onchange = () => {
          const item = items.find(entry => entry.id === menu.value);
          if (!item) return;
          revision++;
          pending = false;
          set({kind: "sticker", src: item.src, label: item.label, id: item.id});
          menu.hidden = true;
        };
        menu.hidden = false;
        menu.focus();
      } catch (failure) { error.textContent = failure.message; }
    }}, "表情包");
    return {
      element: h("div", {class: "cm-media-controls"}, pick,
        h("div", {class: "cm-media-actions"},
          h("button", {type: "button", class: "ui-btn ui-btn-quiet ui-btn-sm", onClick: () => pick.click()}, "添加图片"), stickers),
        menu, preview),
      clear: clearMedia,
      hasMedia: () => Boolean(media),
      pending: () => pending,
      payload: (prefix = "") => media?.kind === "image" ? {[prefix + "image"]: media.src}
        : media ? {[prefix + "sticker_id"]: media.id} : {},
    };
  }

  function postRow(post) {
    const open = () => openPost(post.id);
    const title = h("button", {type: "button", class: "cm-post-title", onClick: open, "aria-label": "查看帖子：" + (post.title || "媒体帖子")},
      highlight(post.title || "", s.keyword), renderMedia(post.title_media, true));
    const row = h("article", {class: "cm-post" + (String(post.id) === s.newId ? " is-new" : ""), "data-id": String(post.id)},
      h("div", {class: "cm-post-head"}, badgesFor(post), h("h3", {}, title)),
      h("p", {class: "cm-post-body"}, highlight(post.body, s.keyword)),
      h("div", {class: "cm-post-meta"},
        authorAvatar(post), h("span", {}, post.nickname), h("span", {class: "ui-dot", "aria-hidden": "true"}, "·"),
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
    const titleMedia = mediaInput(title, "标题", error), bodyMedia = mediaInput(body, "正文", error);
    const form = h("form", {class: "cm-compose", novalidate: true, onSubmit: async event => {
      event.preventDefault();
      error.textContent = "";
      const values = {title: title.value.trim(), body: body.value.trim()};
      const parsed = parseTags(tags.value);
      if (submit.disabled || titleMedia.pending() || bodyMedia.pending()) return;
      const missingTitle = !values.title && !titleMedia.hasMedia();
      const missingBody = !values.body && !bodyMedia.hasMedia() && !titleMedia.hasMedia();
      const problem = missingTitle ? "请填写标题或添加标题附件" : missingBody ? "请填写正文或添加附件" : parsed.error;
      if (problem) { error.textContent = problem; (missingTitle ? title : missingBody ? body : tags).focus(); return; }
      try {
        const result = await withBusy(submit, () => forumApi.create({...values, tags: parsed.tags,
          ...titleMedia.payload("title_"), ...bodyMedia.payload()}), "发布中…");
        form.reset(); titleMedia.clear(); bodyMedia.clear(); count.textContent = `0 / ${POST_MAX}`;
        toast("帖子已发布", "success");
        s.keyword = ""; s.tag = ""; s.sort = "latest"; search.value = ""; s.newId = String(result.id);
        await loadList();
      } catch (failure) { error.textContent = failure.message; toast("发布失败：" + failure.message, "error"); }
    }},
      h("label", {for: titleId}, "标题", title), titleMedia.element,
      h("label", {for: tagsId}, "标签（选填）", tags),
      h("label", {for: bodyId}, h("span", {class: "ui-label-row"}, "正文", count), body), bodyMedia.element,
      error,
      h("p", {class: "ui-muted"}, "发布后所有社区成员可见。只有你自己能删除这篇帖子。"),
      submit);
    return h("section", {class: "cm-panel"}, h("h3", {}, "发布帖子"), form);
  }
  let composeEl = null;

  /* ---------- thread view ---------- */
  const T = {};
  const isMine = post => Boolean(post.mine);

  function buildThread() {
    T.head = h("div", {class: "cm-thread-head"});
    T.commentsHead = h("h3", {class: "cm-comments-title"}, "评论");
    T.comments = h("div", {class: "cm-comments", "aria-live": "polite"});
    const area = h("textarea", {name: "body", rows: "3", maxlength: String(COMMENT_MAX), required: true, "aria-label": "评论内容", placeholder: "写下你的看法或补充…"});
    const count = h("span", {class: "ui-counter", "aria-hidden": "true"});
    bindCounter(area, count, COMMENT_MAX);
    const error = h("p", {class: "ui-field-error", role: "alert"});
    const submit = h("button", {type: "submit", class: "ui-btn ui-btn-primary"}, "发表评论");
    const media = T.media = mediaInput(area, "评论", error);
    const reply = h("span");
    T.replyBar = h("div", {class: "cm-reply-toolbar", hidden: true}, reply,
      h("button", {type: "button", class: "ui-btn ui-btn-sm", onClick: () => T.setReply(null)}, "取消回复"));
    T.setReply = comment => {
      T.reply = comment;
      T.replyBar.hidden = !comment;
      reply.textContent = comment ? "回复 @" + comment.nickname : "";
    };
    const suggestions = h("div", {class: "cm-reply-toolbar", hidden: true, "aria-label": "选择要提及的人"});
    area.addEventListener("input", () => {
      clear(suggestions); suggestions.hidden = true;
      const end = area.selectionStart;
      const match = /(?:^|\s)@([^@\s]*)$/.exec(area.value.slice(0, end));
      if (!T.reply || !match) return;
      const start = end - match[1].length;
      const names = new Set([s.post?.nickname, ...s.comments.map(c => c.nickname)]);
      for (const name of [...names].filter(name => name?.toLowerCase().includes(match[1].toLowerCase())).slice(0, 8)) {
        suggestions.append(h("button", {type: "button", class: "ui-btn ui-btn-sm", onClick: () => {
          area.setRangeText(name + " ", start, end, "end");
          area.dispatchEvent(new Event("input")); area.focus();
        }}, "@" + name));
      }
      suggestions.hidden = !suggestions.childElementCount;
    });
    area.addEventListener("keydown", event => { if ((event.ctrlKey || event.metaKey) && event.key === "Enter") form.requestSubmit(); });
    const form = h("form", {class: "cm-comment-form", novalidate: true, onSubmit: async event => {
      event.preventDefault();
      error.textContent = "";
      const text = area.value.trim();
      if (submit.disabled || media.pending()) return;
      if (!text && !media.hasMedia()) { error.textContent = "请填写评论或添加附件"; area.focus(); return; }
      const postId = s.postId;
      try {
        const sent = await withBusy(submit, () => forumApi.comment(postId, {body: text, reply_to: T.reply?.id, ...media.payload()}), "发送中…");
        if (postId !== s.postId) return;
        media.clear();
        area.value = ""; count.textContent = `0 / ${COMMENT_MAX}`;
        toast("评论已发表", "success");
        await loadThread({silent: true});
        T.setReply(null);
        area.dispatchEvent(new Event("input"));
        if (!T.comments.querySelector("details:not([open]) #" + CSS.escape("forum-comment-" + sent.id))) {
          focusComment(sent.id);
        }
      } catch (failure) { error.textContent = failure.message; toast("评论失败：" + failure.message, "error"); }
    }}, h("div", {class: "ui-label-row"}, h("span", {}, "发表评论"), count), T.replyBar, area, suggestions, media.element, error,
      h("div", {class: "cm-form-actions"}, h("span", {class: "ui-muted"}, "Ctrl + Enter 快速发送"), submit));
    T.form = form;
    T.view = h("section", {class: "cm-thread", "aria-label": "帖子详情"}, T.head, T.commentsHead, T.comments, form);
    T.info = h("section", {class: "cm-panel"});
  }

  const expanded = new Set();
  function rootComment(comment, byId) {
    while (comment.reply_to && byId.has(comment.reply_to)) comment = byId.get(comment.reply_to);
    return comment.id;
  }

  function focusComment(id) {
    const byId = new Map(s.comments.map(comment => [comment.id, comment]));
    const target = byId.get(Number(id));
    if (!target) return false;
    expanded.add(rootComment(target, byId));
    renderThread();
    const row = document.getElementById("forum-comment-" + id);
    row?.scrollIntoView({block: "center"});
    row?.focus({preventScroll: true});
    return Boolean(row);
  }

  function commentRow(comment, parent) {
    const reply = h("button", {type: "button", class: "ui-btn ui-btn-quiet ui-btn-sm", onClick: () => {
      T.setReply(comment);
      T.form.elements.body.focus();
      T.form.elements.body.dispatchEvent(new Event("input"));
    }}, "回复");
    const like = h("button", {type: "button", class: "ui-btn ui-btn-quiet ui-btn-sm", "aria-pressed": String(Boolean(comment.liked)),
      onClick: async () => {
        try {
          await withBusy(like, () => forumApi.comment(s.postId, {action: "like", comment: comment.id, liked: !comment.liked}));
          await loadThread({silent: true});
        } catch (error) { toast(error.message, "error"); }
      }}, "赞 " + (comment.likes || 0));
    const remove = comment.mine ? h("button", {type: "button", class: "ui-btn ui-btn-quiet ui-btn-sm ui-danger-text", onClick: () => removeComment(comment)}, "删除") : null;
    return h("article", {id: "forum-comment-" + comment.id, tabindex: "-1", class: "cm-comment" + (comment.mine ? " is-mine" : "")},
      h("div", {class: "cm-comment-head"}, authorAvatar(comment), h("strong", {}, comment.nickname), comment.mine ? h("span", {class: "ui-badge"}, "我") : null, timeEl(comment.created), remove),
      parent ? h("small", {class: "ui-muted"}, "回复 @" + parent.nickname) : null,
      comment.body ? h("p", {}, comment.body) : null, renderMedia(comment.media),
      h("div", {class: "cm-reply-toolbar"}, reply, like));
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
    const title = h("h2", {tabindex: "-1", id: "cm-thread-title"}, post.title, renderMedia(post.title_media, true));
    append(clear(T.head), [
      h("div", {class: "cm-post-head"}, badgesFor(post)), title,
      h("div", {class: "cm-post-meta"}, authorAvatar(post), h("span", {}, post.nickname), h("span", {class: "ui-dot", "aria-hidden": "true"}, "·"),
        h("span", {title: formatTime(post.created)}, "发布于 ", timeEl(post.created))),
      ref, post.body ? h("div", {class: "cm-post-full"}, post.body) : null, renderMedia(post.media),
      post.tags?.length ? h("div", {class: "cm-tags"}, post.tags.map(tag =>
        h("button", {type: "button", class: "ui-chip ui-chip-sm", "aria-label": "查看标签：" + tag, onClick: () => { s.tag = tag; backToList({reload: true}); }}, tag))) : null]);
    T.commentsHead.textContent = `评论（${s.comments.length}）`;
    clear(T.comments);
    if (!s.comments.length) T.comments.append(stateBlock("empty", "还没有评论", {hint: "来写第一条吧。"}));
    else {
      const byId = new Map(s.comments.map(comment => [comment.id, comment]));
      const branches = new Map();
      for (const comment of s.comments) {
        const rootId = rootComment(comment, byId);
        if (!branches.has(rootId)) branches.set(rootId, []);
        if (comment.id !== rootId) branches.get(rootId).push(comment);
      }
      for (const [rootId, replies] of branches) {
        const open = expanded.has(rootId);
        const branch = h("div", {class: "cm-comment-branch"}, commentRow(byId.get(rootId)));
        if (replies.length) {
          const thread = h("details", {class: "cm-comment-replies", open},
            h("summary", {}, open ? "收起回复" : "查看 " + replies.length + " 条回复"),
            replies.map(comment => commentRow(comment, byId.get(comment.reply_to))));
          thread.addEventListener("toggle", () => {
            if (thread.open) expanded.add(rootId);
            else expanded.delete(rootId);
            thread.firstElementChild.textContent = thread.open ? "收起回复" : "查看 " + replies.length + " 条回复";
          });
          branch.append(thread);
        }
        T.comments.append(branch);
      }
    }
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
      const sig = JSON.stringify([data.post?.id, data.post?.title, comments.map(c => [c.id, c.likes, c.liked]), data.post?.pinned, data.post?.featured]);
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

  async function openPost(id, commentId) {
    s.view = "thread"; s.postId = String(id); s.lastOpened = String(id);
    s.post = null; s.comments = []; threadSig = "";
    if (!T.view) buildThread();
    T.media.clear();
    expanded.clear();
    T.setReply(null);
    T.form.reset();
    T.form.elements.body.dispatchEvent(new Event("input"));
    renderAll();
    ctx.onNavigate?.();
    await loadThread();
    root.querySelector("#cm-thread-title")?.focus({preventScroll: true});
    T.view.scrollIntoView({block: "start"});
    if (commentId && !focusComment(commentId)) throw Error("无法定位这条评论，可能已被删除");
  }

  function backToList({reload = false} = {}) {
    threadCtl?.abort();
    s.view = "list";
    renderAll();
    ctx.onNavigate?.();
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
    openPost(id, commentId) { started = true; return openPost(id, commentId); },
    canGoBack: () => s.view === "thread",
    /** Re-selecting the already-open 论坛 tab returns from a thread to the list. */
    home() { if (s.view === "thread") backToList(); },
    refresh: () => (s.view === "thread" ? loadThread() : loadList()),
  };
}