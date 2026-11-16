/* Workspace home: account status, open positions, recent operations, risk hints, trade summary, community snapshot.
   Everything shown is read from existing endpoints/state; nothing is invented. */
import {h, clear, append, formatTime, money, uid, isAbort} from "/assets/ui/dom.js";
import {aiApi, communityApi} from "/assets/ui/api.js";
import {stateBlock, skeleton} from "/assets/ui/feedback.js";
import {renderMarkdown} from "/assets/ui/markdown.js";
import {marketBulletin} from "/assets/home/bulletin.js";
import {loadProfile} from "/assets/ui/session.js";

const SIDE = {buy: "做多", sell: "做空"};
const STATUS = {running: "运行中", paused: "已暂停", ended: "本局已结束"};
const SOURCE = {ai: "AI", human: "人类", system: "系统"};
const LEVEL = {high: ["需处理", 0], warn: ["注意", 1], info: ["提示", 2]};

const num = value => { const n = Number(value); return value === "" || value === null || value === undefined || !Number.isFinite(n) ? null : n; };
const usd = value => num(value) === null ? "—" : "$" + money(value);
const signedUsd = value => { const n = num(value); return n === null ? "—" : (n > 0 ? "+" : n < 0 ? "-" : "") + "$" + money(Math.abs(n)); };
const signedPct = value => { const n = num(value); return n === null ? "—" : (n > 0 ? "+" : "") + n.toFixed(2) + "%"; };
const tone = value => { const n = num(value); return n > 0 ? "positive" : n < 0 ? "negative" : ""; };

/** Risk hints are derived only from fields the API returned. Thresholds (50% / 80% margin use, 10% drawdown) are display heuristics, not engine limits. */
export function computeRisks(state, selected) {
  const out = [];
  const add = (level, title, detail) => out.push({level, title, detail});
  const g = selected.game, v = selected.valuation;
  const positions = v?.positions || g.positions || [];
  if (g.status === "ended") add("high", "本局已结束", "不能再开仓或追加资金，可以去 AI 复盘总结这一局。");
  if (g.status === "paused") add("warn", "智能体操作已暂停", "自动看盘与交易写入暂停，手动发起的看盘仍可读取行情。已有持仓继续检查止盈止损及强平。");
  if (state.market_closed) add("warn", "外汇周末休市", "行情可能停止更新" + (state.automatic_break ? "，自动看盘正在休息。" : "。"));
  else if (state.automatic_break) add("info", "处于休息时段", "已暂停自动看盘，休息结束后会恢复。");
  if (selected.market_error || !v) add("warn", "行情暂不可用", "持仓估值和浮动盈亏暂时无法计算，请等待有效报价。");
  if (v) {
    const equity = num(v.equity), margin = num(v.margin);
    if (equity > 0 && margin !== null) {
      const ratio = margin / equity * 100;
      if (ratio >= 80) add("high", `保证金占用 ${ratio.toFixed(1)}%`, "可用空间很小，价格不利时可能触发保证金不足或强平。考虑降低仓位或追加本金。");
      else if (ratio >= 50) add("warn", `保证金占用 ${ratio.toFixed(1)}%`, "超过净值的一半，继续加仓前先确认能承受的波动。");
    }
    if (num(v.debt) > 0) add("warn", `有待还贷款 ${usd(v.debt)}`, "含利息，利息按真实时间累积，可在交易工作台的「周转小金库」偿还。");
  }
  const noStop = positions.filter(p => !num(p.sl));
  if (noStop.length) add("warn", `${noStop.length} 笔持仓未设置止损`, "价格反向波动时没有自动保护。" + noStop.slice(0, 3).map(p => p.pair).join("、") + (noStop.length > 3 ? " 等" : ""));
  const drawdown = num(g.drawdown);
  if (drawdown >= 10) add("warn", `最大回撤 ${drawdown.toFixed(2)}%`, "本局回撤已较大，建议复盘并检查仓位管理。");
  if (!g.trade && g.status !== "ended") add("info", "当前账号只读", "管理员未开放交易权限，可以查看账户，不能下单。");
  return out.sort((a, b) => LEVEL[a.level][1] - LEVEL[b.level][1]);
}

export function createHome(root, ctx) {
  const idOf = uid("hm");
  const nowText = () => new Date().toLocaleTimeString("zh-CN", {hour12: false});

  /* ---------- panel shells (built once so focus survives the 10 s refresh) ---------- */
  const badges = h("div", {class: "hm-badges"});
  const stamp = h("span", {class: "ui-muted hm-stamp", role: "status"});
  const figures = h("dl", {class: "hm-figures"});
  const meter = h("div", {class: "hm-meter"});
  const account = h("section", {class: "hm-panel", "aria-labelledby": idOf + "-acc"},
    h("div", {class: "hm-head"}, h("h3", {id: idOf + "-acc"}, "账户状态"), badges, stamp,
      h("button", {type: "button", class: "ui-btn ui-btn-sm hm-head-btn", onClick: () => ctx.goto("overview")}, "前往交易工作台")),
    figures, meter);

  const posBody = h("div", {class: "hm-body"});
  const positions = h("section", {class: "hm-panel", "aria-labelledby": idOf + "-pos"},
    h("div", {class: "hm-head"}, h("h3", {id: idOf + "-pos"}, "当前持仓"),
      h("button", {type: "button", class: "ui-btn ui-btn-sm hm-head-btn", onClick: () => ctx.goto("overview")}, "管理持仓")), posBody);

  const logBody = h("div", {class: "hm-body"});
  const recent = h("section", {class: "hm-panel", "aria-labelledby": idOf + "-log"},
    h("div", {class: "hm-head"}, h("h3", {id: idOf + "-log"}, "最近操作"),
      h("button", {type: "button", class: "ui-btn ui-btn-sm hm-head-btn", onClick: () => ctx.goto("journal")}, "查看交易日志")), logBody);

  const riskBody = h("div", {class: "hm-body"});
  const risks = h("section", {class: "hm-panel", "aria-labelledby": idOf + "-risk"},
    h("div", {class: "hm-head"}, h("h3", {id: idOf + "-risk"}, "风险提示")), riskBody,
    h("p", {class: "ui-muted hm-foot"}, "仅根据当前账户数据判断，不构成投资建议。所有资金均为虚拟资金。"));

  /* ---------- trade summary ---------- */
  const sum = {days: 1, pair: "", status: "idle", data: null, error: null, auto: false};
  const cache = new Map();
  let sumCtl = null;
  const daysSel = h("select", {id: idOf + "-days", "aria-label": "摘要周期", onChange: () => { sum.days = Number(daysSel.value); sumChanged(); }},
    [[1, "最近 1 天"], [7, "最近 7 天"], [30, "最近 30 天"]].map(([v, t]) => h("option", {value: String(v)}, t)));
  const pairSel = h("select", {id: idOf + "-pair", "aria-label": "摘要品种", onChange: () => { sum.pair = pairSel.value; sumChanged(); }});
  const runBtn = h("button", {type: "button", class: "ui-btn ui-btn-sm", onClick: () => loadSummary(true)}, "生成摘要");
  const sumBody = h("div", {class: "hm-body", "aria-live": "polite"});
  const summary = h("section", {class: "hm-panel", "aria-labelledby": idOf + "-sum"},
    h("div", {class: "hm-head"}, h("h3", {id: idOf + "-sum"}, "交易摘要")),
    h("div", {class: "hm-controls"}, daysSel, pairSel, runBtn), sumBody);

  const sumKey = () => `${ctx.game()}|${sum.days}|${sum.pair}`;
  function sumChanged() {
    const hit = cache.get(sumKey());
    if (hit) { sum.data = hit; sum.status = "ready"; }
    else if (sum.auto) { loadSummary(false); return; }
    else { sum.status = "idle"; sum.data = null; }
    renderSummary();
  }
  async function loadSummary(force) {
    const game = ctx.game();
    if (!game) return;
    const key = sumKey();
    if (!force && cache.has(key)) { sum.data = cache.get(key); sum.status = "ready"; renderSummary(); return; }
    sumCtl?.abort();
    const ctl = sumCtl = new AbortController();
    sum.status = "loading"; sum.error = null; renderSummary();
    try {
      const data = await aiApi.tradingSummary({game, days: sum.days, pair: sum.pair}, ctl.signal);
      if (ctl !== sumCtl) return;
      cache.set(key, data); sum.data = data; sum.status = "ready";
    } catch (error) {
      if (isAbort(error) || ctl !== sumCtl) return;
      sum.status = "error"; sum.error = error;
    }
    renderSummary();
  }
  function renderSummary() {
    clear(sumBody);
    runBtn.disabled = sum.status === "loading";
    runBtn.textContent = sum.data ? "重新生成" : "生成摘要";
    if (!ctx.game()) { sumBody.append(stateBlock("empty", "还没有交易实例", {hint: "创建模拟交易后，这里会显示你的交易摘要。"})); return; }
    if (sum.status === "idle") {
      sumBody.append(stateBlock("empty", "选择周期后生成摘要", {hint: sum.auto ? "" : "已配置模型，生成时会调用你的模型接口，可能需要几十秒。"}));
      return;
    }
    if (sum.status === "loading") { sumBody.append(stateBlock("loading", "正在整理交易摘要…"), skeleton(3)); return; }
    if (sum.status === "error") { sumBody.append(stateBlock("error", sum.error?.message || "摘要加载失败", {retry: () => loadSummary(true)})); return; }
    const data = sum.data, st = data.stats || {}, noFills = !Number(st.fills);
    append(sumBody, [
      h("div", {class: "ai-tagrow"}, h("span", {class: "ui-badge ui-badge-ai"}, "AI 生成 · 未发布"), h("span", {class: "ui-muted"}, "基于模拟账本")),
      h("p", {class: "hm-sum-text"}, data.summary),
      h("dl", {class: "ui-dl hm-sum-stats"},
        h("dt", {}, "品种"), h("dd", {}, st.pair || "—"),
        h("dt", {}, "成交次数"), h("dd", {}, String(st.fills ?? 0)),
        h("dt", {}, "胜率"), h("dd", {}, noFills ? "样本不足" : (Number(st.win_rate) * 100).toFixed(0) + "%"),
        h("dt", {}, "净结果"), h("dd", {class: tone(st.net)}, signedUsd(st.net)),
        h("dt", {}, "最大回撤"), h("dd", {}, Number(st.max_drawdown).toFixed(2) + "%")),
      data.risk_points?.length ? h("ul", {class: "ai-risks"}, data.risk_points.map(point => h("li", {}, point))) : null,
      h("div", {class: "ai-actions"},
        h("button", {type: "button", class: "ui-btn ui-btn-sm", onClick: () => ctx.draftFromReview(data, {days: sum.days, pair: st.pair || sum.pair})}, "转为论坛草稿"),
        h("span", {class: "ui-muted"}, "只会生成草稿，不会自动发布"))]);
  }

  /* ---------- community snapshot ---------- */
  const comBody = h("div", {class: "hm-body", "aria-live": "polite"});
  const community = {status: "idle", data: null, error: null};
  let comCtl = null;
  const comSection = h("section", {class: "hm-panel", "aria-labelledby": idOf + "-com"},
    h("div", {class: "hm-head"}, h("h3", {id: idOf + "-com"}, "社区动态"),
      h("button", {type: "button", class: "ui-btn ui-btn-sm hm-head-btn", onClick: () => loadCommunity()}, "刷新")), comBody);
  async function loadCommunity() {
    comCtl?.abort();
    const ctl = comCtl = new AbortController();
    community.status = "loading"; renderCommunity();
    try { community.data = await communityApi.summary(ctl.signal); community.status = "ready"; }
    catch (error) { if (isAbort(error) || ctl !== comCtl) return; community.status = "error"; community.error = error; }
    renderCommunity();
  }
  function renderCommunity() {
    clear(comBody);
    if (community.status === "loading" && !community.data) { comBody.append(stateBlock("loading", "正在读取社区动态…")); return; }
    if (community.status === "error") { comBody.append(stateBlock("error", community.error?.message || "社区动态加载失败", {retry: loadCommunity})); return; }
    const d = community.data;
    if (!d) return;
    const unread = ctx.unread();
    const empty = !d.posts && !d.groups;
    comBody.append(
      empty ? stateBlock("empty", "社区还没有内容", {hint: "去论坛发第一篇帖子，或创建一个群聊。"}) : h("dl", {class: "ui-dl hm-com-stats"},
        h("dt", {}, "论坛帖子"), h("dd", {}, `${d.posts} 篇，24 小时内新增 ${d.posts_24h}`),
        h("dt", {}, "评论"), h("dd", {}, `${d.comments} 条`),
        h("dt", {}, "群聊"), h("dd", {}, `共 ${d.groups} 个，已加入 ${d.joined_groups} 个`),
        h("dt", {}, "群消息"), h("dd", {}, `24 小时内 ${d.messages_24h} 条`),
        h("dt", {}, "未读"), h("dd", {}, unread > 0 ? h("strong", {class: "hm-unread"}, `${unread} 条未读群消息`) : "没有未读消息")),
      h("div", {class: "hm-links"},
        h("button", {type: "button", class: "ui-btn ui-btn-sm", onClick: () => ctx.openCommunity("forum")}, "论坛"),
        h("button", {type: "button", class: "ui-btn ui-btn-sm", onClick: () => ctx.openCommunity("groups")}, "群聊", unread > 0 ? h("span", {class: "ui-count"}, unread > 99 ? "99+" : String(unread)) : null),
        h("button", {type: "button", class: "ui-btn ui-btn-sm", onClick: () => ctx.openCommunity("ai")}, "AI 助手")));
  }

  /* ---------- live panels driven by app state ---------- */
  function renderAccount(state, selected) {
    const g = selected.game, v = selected.valuation;
    append(clear(badges), [
      h("span", {class: "ui-badge " + (g.status === "running" ? "ui-badge-published" : g.status === "paused" ? "ui-badge-pin" : "ui-badge-muted")}, STATUS[g.status] || g.status),
      h("span", {class: "ui-badge " + (g.trade ? "" : "ui-badge-muted")}, g.trade ? "可交易" : "只读"),
      state.market_closed ? h("span", {class: "ui-badge ui-badge-pin"}, "休市") : null]);
    const row = (label, value, cls = "", lead = false) =>
      h("div", {class: "hm-fig" + (lead ? " hm-fig-lead" : "")}, h("dt", {}, label), h("dd", {class: cls}, value));
    clear(figures).append(
      row("当前净值", v ? usd(v.equity) : "—", "", true),
      row("可用保证金", v ? usd(v.free) : "—"),
      row("已用保证金", v ? usd(v.margin) : "—"),
      row("浮动盈亏", v ? signedUsd(v.floating) : "—", v ? tone(v.floating) : ""),
      row("本局净收益", v ? signedUsd(v.profit) : "—", v ? tone(v.profit) : ""),
      row("收益率", v ? signedPct(v.return_rate) : "—", v ? tone(v.return_rate) : ""),
      row("待还贷款（含息）", v ? usd(v.debt || 0) : "—"),
      row("最大回撤", num(g.drawdown) === null ? "—" : Number(g.drawdown).toFixed(2) + "%"));
    clear(meter);
    const equity = v ? num(v.equity) : null, margin = v ? num(v.margin) : null;
    if (equity > 0 && margin !== null) {
      const ratio = Math.min(100, margin / equity * 100);
      const level = ratio >= 80 ? "high" : ratio >= 50 ? "warn" : "ok";
      meter.append(h("span", {class: "hm-meter-label", id: idOf + "-meter"}, "保证金占用"),
        h("div", {class: "hm-meter-track", role: "meter", "aria-labelledby": idOf + "-meter", "aria-valuemin": "0", "aria-valuemax": "100", "aria-valuenow": ratio.toFixed(1), "aria-valuetext": `${ratio.toFixed(1)}%`},
          h("span", {class: "hm-meter-bar hm-meter-" + level, style: `width:${ratio}%`})),
        h("strong", {class: "hm-meter-value"}, ratio.toFixed(1) + "%"),
        h("span", {class: "ui-muted"}, "已用保证金 ÷ 净值"));
    } else meter.append(h("span", {class: "ui-muted"}, v ? "当前没有占用保证金。" : "暂无有效报价，无法计算保证金占用。"));
    stamp.textContent = "更新于 " + nowText();
  }

  function renderPositions(selected) {
    const g = selected.game, v = selected.valuation;
    const rows = v?.positions || g.positions || [];
    clear(posBody);
    if (!rows.length) { posBody.append(stateBlock("empty", "目前没有持仓", {hint: "在交易工作台选择货币对开仓后，会显示在这里。"})); return; }
    const cell = (text, cls = "") => h("td", {class: cls}, text);
    posBody.append(h("div", {class: "hm-tablewrap"}, h("table", {class: "hm-table"},
      h("caption", {class: "sr-only"}, "当前持仓列表"),
      h("thead", {}, h("tr", {}, ["品种", "方向", "数量", "开仓价", "现价", "浮动盈亏", "止损", "止盈"].map(t => h("th", {scope: "col", class: t === "品种" || t === "方向" ? "" : "num"}, t)))),
      h("tbody", {}, rows.map(p => h("tr", {},
        h("th", {scope: "row"}, p.pair), cell(SIDE[p.side] || p.side),
        cell(num(p.units) === null ? "—" : Number(p.units).toLocaleString("en-US", {maximumFractionDigits: 2}), "num"),
        cell(String(p.entry ?? "—"), "num"), cell(v ? String(p.current ?? "—") : "—", "num"),
        cell(v ? signedUsd(p.pnl) : "—", "num " + (v ? tone(p.pnl) : "")),
        cell(num(p.sl) ? String(p.sl) : "未设置", "num" + (num(p.sl) ? "" : " hm-unset")),
        cell(num(p.tp) ? String(p.tp) : "未设置", "num" + (num(p.tp) ? "" : " hm-unset"))))))));
  }

  function renderLogs(selected) {
    const logs = selected.logs || [];
    clear(logBody);
    if (!logs.length) { logBody.append(stateBlock("empty", "还没有交易记录", {hint: "每次操作和理由都会记录在这里。"})); return; }
    logBody.append(h("ul", {class: "hm-logs"}, logs.slice(0, 4).map(log => {
      const reason = log.reason ? h("div", {class: "hm-log-reason markdown-copy"}) : null;
      if (reason) renderMarkdown(reason, log.reason);
      return h("li", {},
      h("div", {class: "hm-log-top"},
        h("strong", {}, (ctx.labels[log.action] || log.action)),
        log.status === "rejected" ? h("span", {class: "ui-badge ui-badge-muted"}, "未执行") : null,
        h("span", {class: "ui-muted"}, SOURCE[log.source] || log.source),
        h("time", {class: "ui-muted", datetime: new Date(log.created * 1000).toISOString()}, formatTime(log.created))),
      reason);
    })));
  }

  function renderRisks(state, selected) {
    const list = computeRisks(state, selected);
    clear(riskBody);
    if (!list.length) { riskBody.append(stateBlock("empty", "暂时没有需要特别注意的风险项", {hint: "仓位、保证金和行情状态都在正常范围。"})); return; }
    riskBody.append(h("ul", {class: "hm-risks"}, list.map(item => h("li", {class: "hm-risk hm-risk-" + item.level},
      h("span", {class: "ui-badge hm-lv hm-lv-" + item.level}, LEVEL[item.level][0]),
      h("div", {}, h("strong", {}, item.title), h("p", {}, item.detail))))));
  }

  /* ---------- assembly ---------- */
  const main = h("div", {class: "hm-main"}, account, positions, recent);
  const bulletin = marketBulletin();
  const side = h("div", {class: "hm-side"}, risks, bulletin.panel, summary, comSection);
  const empty = h("div", {class: "hm-empty", hidden: true}, stateBlock("loading", "正在读取账户…"));
  const grid = h("div", {class: "hm-grid"}, main, side);
  root.replaceChildren(h("div", {class: "hm"}, empty, grid));
  let lastSig = "", opened = false, pairsKey = "";

  function syncPairs(game) {
    const pairs = game.pairs || [];
    const key = pairs.join();
    if (key === pairsKey) return;
    pairsKey = key;
    clear(pairSel).append(h("option", {value: ""}, pairs.length > 1 ? "默认品种" : (pairs[0] || "默认品种")), ...(pairs.length > 1 ? pairs.map(p => h("option", {value: p}, p)) : []));
    pairSel.disabled = pairs.length <= 1;
    sum.pair = "";
  }

  return {
    /** Called on every app render (every ~10 s). Rebuilds only the read-only bodies; controls keep focus. */
    update(state, selected) {
      const ready = Boolean(selected);
      grid.hidden = !ready; empty.hidden = ready;
      if (!ready) return;
      syncPairs(selected.game);
      if (opened && !root.hidden) bulletin.refresh();
      const sig = JSON.stringify([selected.game, selected.valuation, selected.logs, selected.market_error, state.market_closed, state.automatic_break, ctx.unread()]);
      stamp.textContent = "更新于 " + nowText();
      if (sig === lastSig) return;
      lastSig = sig;
      renderAccount(state, selected);
      renderPositions(selected);
      renderLogs(selected);
      renderRisks(state, selected);
      if (community.data) renderCommunity();
    },
    async open() {
      if (!opened) { opened = true; renderSummary(); renderCommunity(); }
      loadCommunity();
      bulletin.refresh(true);
      try {
        const profile = await loadProfile();
        sum.auto = !profile.modelReady;
      } catch { sum.auto = false; }
      if (sum.auto && sum.status === "idle" && ctx.game()) loadSummary(false);
      else renderSummary();
    },
  };
}