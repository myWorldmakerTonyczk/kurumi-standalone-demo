/* AI community hub: daily summary, trade review, group summary, forum draft (edit -> explicit confirm -> publish).
   Nothing here publishes on its own: every post is created only from the "确认发布" button after a confirm dialog. */
import {h, clear, formatTime, parseTags, uid, isAbort} from "/assets/ui/dom.js";
import {aiApi, forumApi, groupApi} from "/assets/ui/api.js";
import {confirmAction, toast, stateBlock, skeleton, withBusy, bindCounter} from "/assets/ui/feedback.js";
import {loadProfile} from "/assets/ui/session.js";

const TITLE_MAX = 100, BODY_MAX = 6000;
const pct = value => (Number(value) * 100).toFixed(0) + "%";
const num = value => Number(value).toLocaleString("en-US", {minimumFractionDigits: 2, maximumFractionDigits: 2});

export const aiTag = (text = "AI 生成 · 未发布") => h("span", {class: "ui-badge ui-badge-ai"}, text);

function list(items, cls = "ai-list") {
  return items?.length ? h("ul", {class: cls}, items.map(item => h("li", {}, item))) : null;
}

export function groupSummaryCard(data, {onClose} = {}) {
  return h("div", {class: "ai-box ai-box-draft"},
    h("div", {class: "ai-tagrow"}, aiTag("AI 生成 · 仅供参考"), h("span", {class: "ui-muted"}, "不会自动发送到群聊")),
    h("h4", {}, data.name ? `「${data.name}」群聊摘要` : "群聊摘要"),
    h("p", {class: "ai-text"}, data.summary || "暂无摘要"),
    data.topics?.length ? h("div", {class: "cm-tags"}, h("span", {class: "ui-muted"}, "涉及品种"), data.topics.map(topic => h("span", {class: "ui-chip ui-chip-sm ui-chip-static"}, topic))) : null,
    data.risk_points?.length ? h("div", {}, h("h5", {}, "风险提醒"), list(data.risk_points, "ai-risks")) : null,
    h("p", {class: "ui-muted"}, `依据 ${data.messages ?? 0} 条公开消息生成`),
    h("div", {class: "ai-actions"},
      h("button", {type: "button", class: "ui-btn ui-btn-sm", onClick: async () => {
        try { await navigator.clipboard.writeText(data.summary || ""); toast("摘要已复制", "success"); }
        catch { toast("无法访问剪贴板，请手动选择文字复制", "error"); }
      }}, "复制摘要"),
      onClose ? h("button", {type: "button", class: "ui-btn ui-btn-quiet ui-btn-sm", onClick: onClose}, "关闭") : null));
}

export function createAi(ctx) {
  const root = h("div", {class: "ai-hub"});
  const tabs = [["daily", "每日摘要"], ["review", "交易复盘"], ["group", "群聊摘要"], ["draft", "论坛草稿"]];
  const tabBar = h("div", {class: "ui-seg ai-tabs", role: "tablist", "aria-label": "AI 功能"});
  const panels = {};
  const tabButtons = {};
  let active = "daily", started = false, profile = null;
  const pairSelects = [];
  const syncPairSelects = () => {
    const pairs = ctx.pairs();
    for (const select of pairSelects) {
      const keep = select.value;
      clear(select).append(...pairs.map(p => h("option", {value: p}, p)));
      if (pairs.includes(keep)) select.value = keep;
    }
  };

  for (const [key, label] of tabs) {
    tabButtons[key] = h("button", {type: "button", role: "tab", id: "ai-tab-" + key, "aria-controls": "ai-panel-" + key, class: "ui-seg-btn", onClick: () => show(key)}, label);
    panels[key] = h("div", {role: "tabpanel", id: "ai-panel-" + key, "aria-labelledby": "ai-tab-" + key, class: "ai-panel", hidden: true});
    tabBar.append(tabButtons[key]);
  }
  tabBar.addEventListener("keydown", event => {
    const at = tabs.findIndex(([key]) => key === active);
    const next = event.key === "ArrowRight" ? at + 1 : event.key === "ArrowLeft" ? at - 1 : -1;
    if (next < 0) return;
    event.preventDefault();
    const target = tabs[(next + tabs.length) % tabs.length][0];
    show(target); tabButtons[target].focus();
  });
  const intro = h("p", {class: "ai-intro"}, "AI 只生成摘要和草稿。内容不会被自动发布，发帖始终需要你在「论坛草稿」里检查、编辑并确认。");
  const modelNote = h("p", {class: "ui-muted ai-model-note", hidden: true});
  root.append(intro, modelNote, tabBar, ...Object.values(panels));

  function show(key) {
    active = key;
    for (const [name] of tabs) {
      const on = name === key;
      tabButtons[name].setAttribute("aria-selected", String(on));
      tabButtons[name].setAttribute("aria-pressed", String(on));
      tabButtons[name].tabIndex = on ? 0 : -1;
      panels[name].hidden = !on;
    }
    if (key === "daily") daily.load();
    if (key === "group") group.load();
  }

  const gameGate = (box, what) => {
    if (ctx.game()) return false;
    clear(box).append(stateBlock("empty", `需要先创建模拟交易才能使用${what}`, {hint: "在交易工作台完成首次设置后即可使用。"}));
    return true;
  };

  /* ---------- daily summary ---------- */
  const daily = (() => {
    const box = h("div", {class: "ai-body", "aria-live": "polite"});
    let status = "idle", data = null, error = null, ctl = null;
    const render = () => {
      clear(box);
      if (status === "loading") { box.append(stateBlock("loading", "正在整理今日社区动态…"), skeleton(4)); return; }
      if (status === "error") { box.append(stateBlock("error", error?.message || "每日摘要加载失败", {retry: () => load(true)})); return; }
      if (!data) return;
      const empty = !data.hot_posts?.length && !data.active_groups?.length;
      box.append(h("div", {class: "ai-box"},
        h("div", {class: "ai-tagrow"}, aiTag("自动摘要 · 仅供阅读"), h("span", {class: "ui-muted"}, data.date || "")),
        h("h4", {}, data.headline || "今日社区摘要"),
        empty ? stateBlock("empty", "今天社区还很安静", {hint: "等有人发帖或群聊有新消息后，这里会出现内容。"}) : h("div", {class: "ai-cols"},
          h("div", {}, h("h5", {}, "热门帖子"), list(data.hot_posts) || h("p", {class: "ui-muted"}, "暂无")),
          h("div", {}, h("h5", {}, "活跃群聊"), list(data.active_groups) || h("p", {class: "ui-muted"}, "暂无"))),
        data.market_views?.length ? h("div", {}, h("h5", {}, "社区观点摘录"), data.market_views.map(view => h("blockquote", {class: "ai-quote"}, view))) : null,
        data.risk_points?.length ? h("div", {}, h("h5", {}, "风险提示"), list(data.risk_points, "ai-risks")) : null,
        data.needs_verification?.length ? h("div", {}, h("h5", {}, "发布前需要核对"), list(data.needs_verification)) : null));
    };
    async function load(force = false) {
      if (status === "loading" || (status === "ready" && !force)) return;
      ctl?.abort(); ctl = new AbortController();
      status = "loading"; render();
      try { data = await aiApi.dailySummary(ctl.signal); status = "ready"; }
      catch (failure) { if (isAbort(failure)) return; status = "error"; error = failure; }
      render();
    }
    panels.daily.append(h("div", {class: "ai-bar"}, h("h3", {}, "每日社区摘要"),
      h("button", {type: "button", class: "ui-btn ui-btn-sm", onClick: () => load(true)}, "刷新")), box);
    return {load};
  })();

  /* ---------- shared draft state ---------- */
  const draft = {title: "", body: "", tags: "", origin: "", review: null, original: null, status: "none", postId: "", error: ""};
  const editorBox = h("div", {class: "ai-body", "aria-live": "polite"});
  const hasContent = () => Boolean(draft.title.trim() || draft.body.trim());
  const isEdited = () => Boolean(draft.original) && (draft.title !== draft.original.title || draft.body !== draft.original.body || draft.tags !== draft.original.tags);

  function setDraft(values, origin, review = null) {
    Object.assign(draft, {title: values.title || "", body: values.body || "", tags: (values.tags || []).join(", "), origin, review, status: "draft", postId: "", error: ""});
    draft.original = {title: draft.title, body: draft.body, tags: draft.tags};
    renderEditor();
  }

  async function publish() {
    if (draft.status === "publishing") return;
    const parsed = parseTags(draft.tags);
    const title = draft.title.trim(), body = draft.body.trim();
    const problem = !title ? "请填写标题" : !body ? "请填写正文" : title.length > TITLE_MAX ? `标题不超过 ${TITLE_MAX} 字` : body.length > BODY_MAX ? `正文不超过 ${BODY_MAX} 字` : parsed.error;
    draft.error = problem || "";
    if (problem) { renderEditor(); return; }
    const ok = await confirmAction({title: "确认发布这篇帖子？",
      message: "发布后所有社区成员都能看到。只有你自己可以删除。",
      details: [`标题：${title}`, `正文约 ${body.length} 字`, parsed.tags.length ? `标签：${parsed.tags.join("、")}` : "未设置标签",
        draft.review ? "将作为「交易复盘」引用发布，数据来自你的模拟账本" : "来源：" + (draft.origin === "ai" ? "AI 草稿（已由你检查）" : "手动编辑")],
      confirmText: "确认发布"});
    if (!ok) return;
    draft.status = "publishing"; renderEditor();
    try {
      let id;
      if (draft.review) {
        const result = await forumApi.shareReview({days: draft.review.days, pair: draft.review.pair, end: draft.review.end, title, body, tags: parsed.tags}, ctx.game());
        id = result.post?.id;
      } else id = (await forumApi.create({title, body, tags: parsed.tags})).id;
      draft.status = "published"; draft.postId = String(id ?? "");
      toast("已正式发布到论坛", "success");
      ctx.onPublished?.();
    } catch (failure) {
      draft.status = "draft"; draft.error = failure.message;
      toast("发布失败：" + failure.message, "error");
    }
    renderEditor();
  }

  async function discard() {
    if (hasContent() && draft.status === "draft") {
      const ok = await confirmAction({title: "丢弃这份草稿？", message: "草稿只保存在当前页面，丢弃后无法找回。", confirmText: "丢弃草稿", tone: "danger"});
      if (!ok) return;
    }
    Object.assign(draft, {title: "", body: "", tags: "", origin: "", review: null, original: null, status: "none", postId: "", error: ""});
    renderEditor();
  }

  function renderEditor() {
    clear(editorBox);
    if (draft.status === "none") return;
    const published = draft.status === "published", busy = draft.status === "publishing";
    const idT = uid("draft-title"), idG = uid("draft-tags"), idB = uid("draft-body");
    const title = h("input", {id: idT, maxlength: String(TITLE_MAX), value: draft.title, disabled: published || busy, autocomplete: "off"});
    const tags = h("input", {id: idG, value: draft.tags, placeholder: "用逗号分隔，最多 10 个", disabled: published || busy, autocomplete: "off"});
    const body = h("textarea", {id: idB, rows: "10", maxlength: String(BODY_MAX), disabled: published || busy}, draft.body);
    body.value = draft.body;
    const count = h("span", {class: "ui-counter", "aria-hidden": "true"});
    bindCounter(body, count, BODY_MAX);
    const sync = () => { draft.title = title.value; draft.tags = tags.value; draft.body = body.value; badge.replaceWith(badge = makeBadge()); };
    const makeBadge = () => published ? h("span", {class: "ui-badge ui-badge-published"}, "已正式发布")
      : h("span", {class: "ui-badge ui-badge-ai"}, isEdited() ? "AI 草稿 · 已编辑 · 未发布" : draft.origin === "ai" ? "AI 生成草稿 · 未发布" : "草稿 · 未发布");
    let badge = makeBadge();
    for (const field of [title, tags, body]) field.addEventListener("input", sync);
    editorBox.append(h("div", {class: "ai-box " + (published ? "ai-box-published" : "ai-box-draft")},
      h("div", {class: "ai-tagrow"}, badge,
        published ? h("span", {class: "ui-muted"}, "内容已出现在论坛，不能再从这里修改。")
          : h("span", {class: "ui-muted"}, "检查并按需修改。点击「确认发布」前不会发给任何人。")),
      draft.review ? h("p", {class: "cm-ref"}, h("strong", {}, "引用交易复盘"), ` · ${draft.review.pair} · 最近 ${draft.review.days} 天`) : null,
      h("label", {for: idT}, "标题", title),
      h("label", {for: idG}, "标签", tags),
      h("label", {for: idB}, h("span", {class: "ui-label-row"}, "正文", count), body),
      draft.error ? h("p", {class: "ui-field-error", role: "alert"}, draft.error) : null,
      h("div", {class: "ai-actions"},
        published ? [
          draft.postId ? h("button", {type: "button", class: "ui-btn ui-btn-primary", onClick: () => ctx.openPost(draft.postId)}, "查看已发布的帖子") : null,
          h("button", {type: "button", class: "ui-btn", onClick: () => { Object.assign(draft, {title: "", body: "", tags: "", origin: "", review: null, original: null, status: "none", postId: ""}); renderEditor(); }}, "完成")
        ] : [
          h("button", {type: "button", class: "ui-btn ui-btn-primary", disabled: busy, onClick: publish}, busy ? "发布中…" : "确认发布"),
          h("button", {type: "button", class: "ui-btn ui-btn-quiet", disabled: busy, onClick: discard}, "丢弃草稿")])));
  }

  /* ---------- trade review ---------- */
  const review = (() => {
    const box = h("div", {class: "ai-body", "aria-live": "polite"});
    const days = h("select", {id: uid("rv-days"), "aria-label": "复盘周期"}, [[1, "最近 1 天"], [7, "最近 7 天"], [30, "最近 30 天"]].map(([v, t]) => h("option", {value: String(v)}, t)));
    const pair = h("select", {id: uid("rv-pair"), "aria-label": "复盘品种"});
    pairSelects.push(pair);
    const run = h("button", {type: "button", class: "ui-btn ui-btn-primary", onClick: () => generate()}, "生成复盘");
    let status = "idle", data = null, error = null, used = null;
    const render = () => {
      clear(box);
      if (gameGate(box, "交易复盘")) return;
      run.disabled = status === "loading";
      if (status === "idle") { box.append(stateBlock("empty", "选择周期和品种，生成一份复盘", {hint: "复盘只读取你自己的模拟账本，生成后可转为论坛草稿再决定是否发布。"})); return; }
      if (status === "loading") { box.append(stateBlock("loading", "正在生成复盘，使用模型时可能需要几十秒…"), skeleton(4)); return; }
      if (status === "error") { box.append(stateBlock("error", error?.message || "复盘生成失败", {retry: generate})); return; }
      const st = data.stats || {};
      const noFills = !Number(st.fills);
      box.append(h("div", {class: "ai-box ai-box-draft"},
        h("div", {class: "ai-tagrow"}, aiTag("AI 生成 · 未发布"), h("span", {class: "ui-muted"}, "基于模拟账本，不代表真实收益")),
        h("h4", {}, data.title),
        h("p", {class: "ai-text"}, data.summary),
        h("dl", {class: "ui-dl ui-dl-inline"},
          h("dt", {}, "品种"), h("dd", {}, st.pair || "—"),
          h("dt", {}, "区间"), h("dd", {}, st.start ? `${formatTime(st.start)} 至 ${formatTime(st.end)}` : "—"),
          h("dt", {}, "成交次数"), h("dd", {}, String(st.fills ?? 0)),
          h("dt", {}, "胜率"), h("dd", {}, noFills ? "样本不足" : pct(st.win_rate)),
          h("dt", {}, "净结果"), h("dd", {class: Number(st.net) > 0 ? "positive" : Number(st.net) < 0 ? "negative" : ""}, (Number(st.net) > 0 ? "+" : "") + num(st.net)),
          h("dt", {}, "最大回撤"), h("dd", {}, Number(st.max_drawdown).toFixed(2) + "%")),
        data.risk_points?.length ? h("div", {}, h("h5", {}, "风险提示"), list(data.risk_points, "ai-risks")) : null,
        h("div", {class: "ai-actions"},
          h("button", {type: "button", class: "ui-btn ui-btn-primary", onClick: () => {
            if (draft.status === "draft" && hasContent() && isEdited()) {
              confirmAction({title: "替换当前草稿？", message: "「论坛草稿」里有你编辑过的内容，转入新复盘会覆盖它。", confirmText: "替换并编辑", tone: "danger"}).then(ok => ok && toDraft());
            } else toDraft();
          }}, "转为论坛草稿"),
          h("button", {type: "button", class: "ui-btn", onClick: async () => {
            try { await navigator.clipboard.writeText(data.publishable_body || data.summary); toast("复盘文本已复制", "success"); }
            catch { toast("无法访问剪贴板，请手动选择文字复制", "error"); }
          }}, "复制全文"))));
    };
    function toDraft() {
      setDraft({title: data.title, body: data.publishable_body || data.summary, tags: ["交易复盘", used.pair].filter(Boolean)}, "ai", {days: used.days, pair: used.pair, end: data.stats?.end});
      show("draft");
      toast("已生成草稿，请检查后再决定是否发布", "info");
      editorBox.querySelector("input")?.focus();
    }
    async function generate() {
      if (!ctx.game() || status === "loading") return;
      used = {days: Number(days.value), pair: pair.value};
      status = "loading"; error = null; render();
      try { data = await aiApi.tradeReview({days: used.days, pair: used.pair}, ctx.game()); status = "ready"; }
      catch (failure) { status = "error"; error = failure; }
      render();
    }
    panels.review.append(h("div", {class: "ai-bar"}, h("h3", {}, "交易复盘"),
      h("div", {class: "ai-controls"}, h("label", {class: "ui-inline-label"}, "周期", days), h("label", {class: "ui-inline-label"}, "品种", pair), run)), box);
    return {render};
  })();

  /* ---------- group summary ---------- */
  const group = (() => {
    const box = h("div", {class: "ai-body", "aria-live": "polite"});
    const select = h("select", {id: uid("gs-room"), "aria-label": "选择群聊"});
    const run = h("button", {type: "button", class: "ui-btn ui-btn-primary", onClick: () => generate()}, "生成摘要");
    let groups = [], listStatus = "idle", listError = null, status = "idle", data = null, error = null, ctl = null;
    const render = () => {
      clear(box);
      if (listStatus === "loading") { box.append(stateBlock("loading", "正在读取群聊…")); run.disabled = true; return; }
      if (listStatus === "error") { box.append(stateBlock("error", listError?.message || "群聊列表加载失败", {retry: () => load(true)})); run.disabled = true; return; }
      if (!groups.length) { box.append(stateBlock("empty", "你还没有加入任何群聊", {hint: "加入或创建群聊后，才能生成群聊摘要。"})); run.disabled = true; return; }
      if (gameGate(box, "群聊摘要")) { run.disabled = true; return; }
      run.disabled = status === "loading";
      if (status === "idle") box.append(stateBlock("empty", "选择一个群聊，生成摘要", {hint: "只读取该群公开消息，摘要不会发送到群里。"}));
      else if (status === "loading") box.append(stateBlock("loading", "AI 正在整理群聊内容…"), skeleton(3));
      else if (status === "error") box.append(stateBlock("error", error?.message || "摘要生成失败", {retry: generate}));
      else box.append(groupSummaryCard(data, {onClose: () => { status = "idle"; render(); }}));
    };
    async function load(force = false) {
      if (listStatus === "loading" || (listStatus === "ready" && !force)) { if (listStatus === "ready") render(); return; }
      ctl?.abort(); ctl = new AbortController();
      listStatus = "loading"; render();
      try {
        const result = await groupApi.list(ctl.signal);
        groups = (result.groups || []).filter(g => g.joined);
        const keep = select.value;
        clear(select).append(...groups.map(g => h("option", {value: String(g.id)}, `${g.name}（${g.members} 人）`)));
        if (groups.some(g => String(g.id) === keep)) select.value = keep;
        listStatus = "ready";
      } catch (failure) { if (isAbort(failure)) return; listStatus = "error"; listError = failure; }
      render();
    }
    async function generate() {
      if (!select.value || status === "loading") return;
      status = "loading"; error = null; render();
      try { data = await aiApi.groupSummary(Number(select.value), ctx.game()); status = "ready"; }
      catch (failure) { status = "error"; error = failure; }
      render();
    }
    panels.group.append(h("div", {class: "ai-bar"}, h("h3", {}, "群聊摘要"),
      h("div", {class: "ai-controls"}, h("label", {class: "ui-inline-label"}, "群聊", select), run)), box);
    return {load};
  })();

  /* ---------- forum draft ---------- */
  (() => {
    const topicId = uid("fd-topic"), sourceId = uid("fd-source"), daysId = uid("fd-days"), pairId = uid("fd-pair"), chkId = uid("fd-review");
    const topic = h("input", {id: topicId, maxlength: "200", autocomplete: "off", placeholder: "例如：EUR/USD 本周的仓位管理"});
    const source = h("textarea", {id: sourceId, rows: "4", maxlength: "5000", placeholder: "可粘贴你的笔记或要点，AI 会据此整理（选填）"});
    const useReview = h("input", {id: chkId, type: "checkbox"});
    const days = h("select", {id: daysId, disabled: true}, [[1, "最近 1 天"], [7, "最近 7 天"], [30, "最近 30 天"]].map(([v, t]) => h("option", {value: String(v)}, t)));
    const pair = h("select", {id: pairId, disabled: true});
    pairSelects.push(pair);
    useReview.addEventListener("change", () => { days.disabled = pair.disabled = !useReview.checked; });
    const error = h("p", {class: "ui-field-error", role: "alert"});
    const gen = h("button", {type: "submit", class: "ui-btn ui-btn-primary"}, "生成草稿");
    const form = h("form", {class: "ai-form", novalidate: true, onSubmit: async event => {
      event.preventDefault();
      error.textContent = "";
      if (!ctx.game()) { error.textContent = "需要先创建模拟交易才能使用 AI 草稿"; return; }
      if (!topic.value.trim() && !source.value.trim() && !useReview.checked) { error.textContent = "请填写主题或素材，或勾选「引用我的交易复盘」"; topic.focus(); return; }
      if (draft.status === "draft" && hasContent() && isEdited() && !(await confirmAction({title: "替换当前草稿？", message: "当前草稿里有你编辑过的内容，重新生成会覆盖它。", confirmText: "替换草稿", tone: "danger"}))) return;
      const body = {topic: topic.value.trim(), source: source.value.trim()};
      if (useReview.checked) Object.assign(body, {trade_review: true, days: Number(days.value), pair: pair.value});
      try {
        const result = await withBusy(gen, () => aiApi.forumDraft(body, ctx.game()), "生成中…");
        setDraft(result, "ai", useReview.checked ? result.review : null);
        editorBox.querySelector("input")?.focus();
      } catch (failure) { error.textContent = failure.message; }
    }},
      h("label", {for: topicId}, "主题", topic),
      h("label", {for: sourceId}, "素材（选填）", source),
      h("div", {class: "ai-review-opt"}, h("label", {class: "ui-check", for: chkId}, useReview, "引用我的交易复盘"),
        h("label", {class: "ui-inline-label", for: daysId}, "周期", days), h("label", {class: "ui-inline-label", for: pairId}, "品种", pair)),
      error, h("div", {class: "ai-actions"}, gen,
        h("button", {type: "button", class: "ui-btn ui-btn-quiet", onClick: async () => {
          if (draft.status === "draft" && hasContent() && isEdited() && !(await confirmAction({title: "替换当前草稿？", message: "当前草稿里有你编辑过的内容，开始新草稿会覆盖它。", confirmText: "开始新草稿", tone: "danger"}))) return;
          setDraft({title: "", body: "", tags: []}, "manual");
          editorBox.querySelector("input")?.focus();
        }}, "不用 AI，直接写")));
    panels.draft.append(h("div", {class: "ai-bar"}, h("h3", {}, "论坛草稿")), form, editorBox);
  })();

  return {
    el: root,
    async activate() {
      if (!started) { started = true; show("daily"); renderEditor(); }
      syncPairSelects(); review.render();
      try {
        profile = await loadProfile();
        modelNote.hidden = profile.modelReady;
        modelNote.textContent = profile.modelReady ? "" : "未配置模型：当前生成的是离线模板内容，仅用于演示。在个人设置里填写模型后可获得 AI 改写。";
      } catch { /* the profile is only used for the note */ }
      if (active === "daily") daily.load();
    },
    gotoDraft(values, origin, reviewRef) { setDraft(values, origin, reviewRef); show("draft"); },
  };
}
