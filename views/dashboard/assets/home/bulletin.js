import {renderMarkdown} from "/assets/ui/markdown.js";
import {api} from "/assets/ui/api.js";
import {h, formatTime} from "/assets/ui/dom.js";

const PERIODS = {day: "日报", week: "周报", month: "月报", event: "指标跟踪"};
const TASKS = {pending: "待唤起", done: "已发布", failed: "失败", cancelled: "已取消"};
const RUNS = {scan: "消息采集", day: "日报", week: "周报", month: "月报", event: "指标唤起"};
const time = stamp => stamp ? formatTime(stamp) : "尚未运行";

function reportBody(content, preview = false) {
  const body = h("div", {class: "bulletin-report-body" + (preview ? " bulletin-preview" : "")});
  const markdown = (content || "").replace(/^(\*\*[^*\r\n]+)\r?\n[ \t]*\*\*[ \t]*(?=\r?$)/gm, "$1**");
  renderMarkdown(body, markdown);
  return body;
}

function reportList(reports) {
  if (!reports.length) return h("p", {class: "bulletin-muted"}, "尚无已发布的播报");
  return h("div", {class: "bulletin-reports"}, reports.map(report => h("details", {},
    h("summary", {}, h("b", {}, PERIODS[report.kind]), " ", report.label,
      h("small", {}, time(report.generated))),
    reportBody(report.body),
    h("small", {}, "汇总 " + report.details.news_count + " 条官方消息"),
    report.details.links.length ? h("ul", {}, report.details.links.map(([title, url]) => {
      if (!url.startsWith("https://")) return h("li", {}, title);
      return h("li", {}, h("a", {href: url, target: "_blank", rel: "noopener noreferrer"}, title));
    })) : null)));
}

export function marketBulletin({compact = false} = {}) {
  const body = h("div", {class: "bulletin-home-body"});
  const state = h("span", {class: "bulletin-signal"});
  const panel = h("section", {class: "hm-panel bulletin-home" + (compact ? " bulletin-compact" : ""), hidden: true},
    h("header", {class: "bulletin-home-head"}, h("h3", {}, "市场播报"), state), body);
  const dialog = h("dialog", {class: "bulletin-archive", "aria-label": "市场播报归档"});
  const archive = h("div");
  dialog.append(h("header", {}, h("h3", {}, "市场播报归档"),
    h("button", {type: "button", onClick: () => dialog.close()}, "关闭")), archive);
  panel.append(dialog);
  let signature = "";
  let busy = false;
  let fetched = 0;
  async function refresh(force = false) {
    if (busy || (!force && Date.now() - fetched < 60000)) return;
    busy = true;
    try {
      const data = await api("market-bulletin");
      fetched = Date.now();
      panel.hidden = !data.enabled;
      if (!data.enabled) return;
      const sig = JSON.stringify(data);
      if (sig === signature) return;
      signature = sig;
      const statuses = {closed: "周末休市", live: "报价已更新", stale: "等待有效行情"};
      state.textContent = data.demo ? "模拟行情" : statuses[data.market];
      state.dataset.state = data.demo ? "demo" : data.market;
      const latest = data.reports[0];
      const preview = latest ? h("div", {class: "bulletin-lead"}, h("small", {}, PERIODS[latest.kind] + " · " + latest.label),
        reportBody(latest.body, true), h("small", {}, "发布于 " + time(latest.generated)))
        : h("p", {class: "bulletin-muted"}, data.model_ready ? "等待发布首份播报" : "等待管理员配置播报模型");
      const quotes = h("div", {class: "bulletin-quotes"}, data.quotes.map(q => h("div", {},
        h("b", {}, q.pair), h("span", {}, q.bid + " / " + q.ask), h("small", {}, time(q.timestamp)))));
      const events = data.agenda.length
        ? h("ul", {class: "bulletin-agenda"}, data.agenda.map(item => h("li", {},
          h("time", {}, time(item.due).slice(-5)), h("span", {}, item.title))))
        : h("p", {class: "bulletin-muted"}, "暂无已同步的待公布指标");
      if (compact) {
        body.replaceChildren(h("button", {type: "button", class: "ui-btn ui-btn-sm",
          onClick: () => dialog.showModal()}, "查看官方播报归档"));
      } else body.replaceChildren(preview, h("h4", {}, "主要品种 · Bid / Ask"),
        data.quotes.length ? quotes : h("p", {class: "bulletin-muted"}, "暂无报价"),
        h("h4", {}, "今日关注 · 北京时间"), events,
        h("button", {type: "button", class: "ui-btn ui-btn-sm", onClick: () => dialog.showModal()}, "查看播报归档"));
      archive.replaceChildren(reportList(data.reports));
    } catch {
      // A failed refresh leaves the most recent bulletin available.
    } finally { busy = false; }
  }
  return {panel, refresh};
}

export function mountBulletinAdmin(root, bridge) {
  const enabled = h("input", {type: "checkbox", name: "enabled", form: "bulletin-settings-form"});
  const interval = h("input", {type: "number", name: "interval", min: 15, max: 1440, required: true});
  const publishAt = h("input", {type: "time", name: "time", required: true});
  const preset = h("select", {name: "preset", "aria-label": "模型预设"});
  let presets = [];
  const model = h("input", {name: "model", placeholder: "模型 ID"});
  const endpoint = h("input", {type: "url", name: "url", placeholder: "https://api.example.com/v1"});
  const key = h("input", {type: "password", name: "key", autocomplete: "new-password", placeholder: "留空保留现有密钥"});
  const periods = ["day", "week", "month"].map(kind => [kind, h("input", {type: "checkbox", value: kind})]);
  const pairs = h("div", {class: "bulletin-checks"});
  const status = h("p", {role: "status", class: "bulletin-feedback"});
  const signal = h("span", {class: "bulletin-signal"});
  const schedule = h("div", {class: "bulletin-task-list"});
  const calendar = h("div", {class: "bulletin-task-list"});
  const reports = h("div");
  const operations = h("div", {class: "bulletin-actions"});
  const runs = h("dl", {class: "bulletin-runs"});
  let initial = false;
  let busy = false;
  let dirty = false;
  const form = h("form", {id: "bulletin-settings-form", class: "bulletin-admin-grid", onSubmit: save});
  const submit = h("button", {type: "submit", class: "primary"}, "保存设置");
  form.addEventListener("input", () => { dirty = true; });
  enabled.addEventListener("change", () => { dirty = true; });
  const label = (caption, control) => h("label", {}, caption, control);
  function syncPreset() {
    const match = presets.findIndex(item => item.url === endpoint.value.trim() && item.model === model.value.trim());
    preset.value = match === -1 ? "" : String(match);
  }
  preset.addEventListener("change", () => {
    if (preset.value === "") return;
    const item = presets[Number(preset.value)];
    if (endpoint.value.trim() !== item.url) key.value = "";
    endpoint.value = item.url;
    model.value = item.model;
    dirty = true;
  });
  endpoint.addEventListener("input", syncPreset);
  model.addEventListener("input", syncPreset);
  form.append(
    h("section", {class: "bulletin-admin-card"}, h("h3", {}, "采集与发布"),
      label("采集间隔（分钟）", interval), label("发布时刻（北京时间）", publishAt),
      h("fieldset", {}, h("legend", {}, "发布周期"), h("div", {class: "bulletin-checks"},
        periods.map(([kind, input]) => label(PERIODS[kind], input)))),
      h("fieldset", {}, h("legend", {}, "首页关注品种"), pairs)),
    h("section", {class: "bulletin-admin-card"}, h("h3", {}, "管理员模型"),
      label("模型预设", preset), label("API Base URL", endpoint), label("模型 ID", model), label("API Key", key),
      h("p", {class: "bulletin-muted"}, "密钥仅供平台播报使用，用户侧不会收到模型配置。")),
    h("div", {class: "bulletin-admin-footer"}, submit));
  root.className = "bulletin-admin";
  root.append(
    h("section", {class: "bulletin-admin-head"}, h("div", {}, h("h3", {}, "平台市场播报"),
      h("p", {}, "共用一套采集计划，向全部用户发布。")),
      h("div", {class: "bulletin-switch"}, signal, label("启用", enabled))),
    status, form,
    h("section", {class: "bulletin-admin-card"}, h("h3", {}, "运行记录"), operations, runs),
    h("div", {class: "bulletin-admin-grid"},
      h("section", {class: "bulletin-admin-card"}, h("h3", {}, "Agent 唤起任务"), schedule),
      h("section", {class: "bulletin-admin-card"}, h("h3", {}, "近期官方日历"), calendar)),
    h("section", {class: "bulletin-admin-card"}, h("h3", {}, "已发布播报"), reports));
  for (const kind of ["scan", "day", "week", "month"]) {
    operations.append(h("button", {type: "button", onClick: () => request({action: kind})},
      kind === "scan" ? "立即采集" : "发布" + PERIODS[kind]));
  }
  operations.append(h("button", {type: "button", onClick: () => request()}, "刷新记录"));

  function render(data, fill) {
    const config = data.config;
    if (fill) {
      enabled.checked = config.enabled;
      interval.value = config.interval;
      publishAt.value = config.time;
      model.value = config.model;
      endpoint.value = config.url;
      key.value = "";
      presets = data.presets;
      preset.replaceChildren(h("option", {value: ""}, "自定义"),
        ...presets.map((item, index) => h("option", {value: String(index)}, item.name)));
      syncPreset();
      periods.forEach(([kind, input]) => { input.checked = config.periods.includes(kind); });
      pairs.replaceChildren(...data.available_pairs.map(pair => {
        const input = h("input", {type: "checkbox", value: pair, checked: config.pairs.includes(pair)});
        return label(pair, input);
      }));
      dirty = false;
    }
    key.placeholder = config.has_key ? "已配置，留空保留" : "请输入管理员 API Key";
    signal.textContent = !config.enabled ? "已停用" : config.model_ready ? "已启用" : "已启用 · 待配置模型";
    signal.dataset.state = config.enabled && config.model_ready ? "live" : "stale";
    runs.replaceChildren(...Object.entries(RUNS).flatMap(([kind, label]) => {
      const row = data.runs.find(run => run.kind === kind);
      return [h("dt", {}, label), h("dd", {}, time(row?.success),
        row?.error ? h("small", {class: "bulletin-error"}, row.error) : null)];
    }));
    schedule.replaceChildren(...(data.tasks.length ? data.tasks.map(task => {
      const state = !task.active && task.state === "pending" ? "日历已变更" : TASKS[task.state];
      const action = task.state === "pending" ? "cancel" : task.state === "failed" ? "retry" : "";
      return h("article", {class: "bulletin-task"}, h("header", {}, h("strong", {}, task.title),
        h("span", {class: "bulletin-signal", dataset: {state: task.state === "done" ? "live" : "stale"}}, state)),
        h("small", {}, time(task.due) + " · 已尝试 " + task.attempts + " 次"), h("p", {}, task.task),
        action && task.active ? h("button", {type: "button", onClick: () => request({action, event: task.event})},
          action === "cancel" ? "取消任务" : "重试") : null);
    }) : [h("p", {class: "bulletin-muted"}, "采集后，Agent 选中的指标会出现在这里。")]));
    calendar.replaceChildren(...(data.agenda.length ? data.agenda.map(item =>
      h("p", {class: "bulletin-calendar-row"}, h("time", {}, time(item.due)), h("span", {}, item.title)))
      : [h("p", {class: "bulletin-muted"}, "暂无已同步的日历事件")]));
    reports.replaceChildren(reportList(data.reports));
  }

  async function request(body, fill = false, silent = false) {
    if (busy) return;
    busy = true;
    if (!silent) status.textContent = body?.action === "save" ? "正在保存…" : "正在处理…";
    submit.disabled = true;
    for (const button of operations.children) button.disabled = true;
    try {
      const data = body ? await bridge.apiPost("market-bulletin", body) : await bridge.apiGet("market-bulletin");
      render(data, fill || !initial);
      initial = true;
      const run = data.runs.find(item => item.kind === body?.action);
      if (!silent) status.textContent = run?.error ||
        (body?.action === "save" ? "已保存设置" : body ? "操作已完成" : "");
    } catch (error) { status.textContent = error.message; }
    finally {
      busy = false;
      submit.disabled = false;
      for (const button of operations.children) button.disabled = false;
    }
  }
  function save(event) {
    event.preventDefault();
    request({action: "save", enabled: enabled.checked, interval: Number(interval.value), time: publishAt.value,
      model: model.value, url: endpoint.value, key: key.value,
      periods: periods.filter(([, input]) => input.checked).map(([kind]) => kind),
      pairs: [...pairs.querySelectorAll(":checked")].map(input => input.value)}, true);
  }
  setInterval(() => { if (initial && !root.hidden && !document.hidden && !dirty) request(undefined, false, true); }, 30000);
  return () => request(undefined, false, initial);
}
