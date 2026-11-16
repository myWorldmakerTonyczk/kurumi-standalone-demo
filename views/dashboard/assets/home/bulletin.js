import {renderMarkdown} from "/assets/ui/markdown.js";
import {api} from "/assets/ui/api.js";
import {h, formatTime} from "/assets/ui/dom.js";

const PERIODS = {live: "实时报告", day: "日报", week: "周报", month: "月报", event: "指标跟踪"};
const TASKS = {pending: "待唤起", done: "已发布", failed: "失败", cancelled: "已取消"};
const RUNS = {scan: "消息采集", day: "日报", week: "周报", month: "月报", event: "指标唤起"};
const time = stamp => stamp ? formatTime(stamp) : "尚未运行";

function reportBody(content) {
  const body = h("div", {class: "bulletin-report-body"});
  const markdown = (content || "").replace(/^(\*\*[^*\r\n]+)\r?\n[ \t]*\*\*[ \t]*(?=\r?$)/gm, "$1**");
  renderMarkdown(body, markdown);
  return body;
}

function reportList(reports) {
  if (!reports.length) return h("p", {class: "bulletin-muted"}, "尚无已发布的播报");
  return h("div", {class: "bulletin-reports"}, reports.map(report => h("details", {dataset: {report: report.id}},
    h("summary", {}, h("b", {}, PERIODS[report.kind]), " ", report.label,
      h("small", {}, time(report.generated))),
    reportBody(report.body),
    h("small", {}, [
      report.details.news_count ? report.details.news_count + " 条消息" : "",
      report.details.media_count ? "其中 " + report.details.media_count + " 条非官方" : "",
      report.details.update_count ? report.details.update_count + " 条实时或指标更新" : "",
      report.details.agenda_count ? report.details.agenda_count + " 条待公布日程" : "",
    ].filter(Boolean).join(" · ")),
    report.details.links?.length ? h("ul", {}, report.details.links.map(([title, url]) => {
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
      const latestUpdates = data.reports.filter(report => ["live", "event"].includes(report.kind)).slice(0, 3);
      const latestDaily = data.reports.find(report => report.kind === "day");
      function openReport(id) {
        for (const item of archive.querySelectorAll("details")) item.open = item.dataset.report === id;
        if (!dialog.open) dialog.showModal();
        if (id) archive.querySelector("details[open]")?.scrollIntoView({block: "nearest"});
      }
      const reportRow = report => h("button", {type: "button",
        class: "bulletin-headline", onClick: () => openReport(report.id)},
        h("b", {}, report.label), h("small", {}, PERIODS[report.kind] + " · " + time(report.generated)));
      const preview = h("div", {class: "bulletin-lead"},
        h("h4", {}, "实时动态"),
        latestUpdates.length ? latestUpdates.map(reportRow)
          : h("p", {class: "bulletin-muted"}, "等待采集到新的市场消息"),
        h("h4", {}, "最新日报"),
        latestDaily ? reportRow(latestDaily)
          : h("p", {class: "bulletin-muted"}, data.model_ready ? "暂无已发布的日报" : "等待管理员配置播报模型"));
      const quotes = h("div", {class: "bulletin-quotes"}, data.quotes.map(q => h("div", {},
        h("b", {}, q.pair), h("span", {}, q.bid + " / " + q.ask), h("small", {}, time(q.timestamp)))));
      const events = data.agenda.length
        ? h("ul", {class: "bulletin-agenda"}, data.agenda.map(item => h("li", {},
          h("time", {}, time(item.due).slice(-5)), h("span", {}, item.title, item.source?.startsWith("非官方") ? h("small", {}, " · " + item.source) : null))))
        : h("p", {class: "bulletin-muted"}, "暂无已同步的待公布指标");
      if (compact) {
        body.replaceChildren(h("button", {type: "button", class: "ui-btn ui-btn-sm",
          onClick: () => dialog.showModal()}, "查看市场播报归档"));
      } else body.replaceChildren(preview, h("h4", {}, "主要品种 · Bid / Ask"),
        data.quotes.length ? quotes : h("p", {class: "bulletin-muted"}, "暂无报价"),
        h("h4", {}, "今日关注 · 北京时间"), events,
        h("button", {type: "button", class: "ui-btn ui-btn-sm", onClick: () => dialog.showModal()}, "查看播报归档"));
      const expanded = [...archive.querySelectorAll("details[open]")].map(item => item.dataset.report);
      archive.replaceChildren(
        h("h4", {}, "实时动态与指标跟踪"),
        reportList(data.reports.filter(report => ["live", "event"].includes(report.kind))),
        h("h4", {}, "日报、周报与月报"),
        reportList(data.reports.filter(report => ["day", "week", "month"].includes(report.kind))));
      for (const item of archive.querySelectorAll("details")) item.open = expanded.includes(item.dataset.report);
    } catch {
      // A failed refresh leaves the most recent bulletin available.
    } finally { busy = false; }
  }
  return {panel, refresh};
}

export function mountBulletinAdmin(root, bridge) {
  const enabled = h("input", {type: "checkbox", name: "enabled", form: "bulletin-settings-form"});
  const interval = h("input", {type: "number", name: "interval", min: 1, max: 1440, required: true});
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
  const sources = h("div", {class: "bulletin-source-list"});
  const searchProviders = {off: "关闭搜索", auto: "自动选择", deepseek: "DeepSeek Search", brave: "Brave News Search"};
  const searchProvider = h("select", {name: "search_provider"}, Object.entries(searchProviders).map(([value, caption]) =>
    h("option", {value}, caption)));
  const searchKey = name => h("input", {type: "password", name, autocomplete: "new-password", maxlength: 1500});
  const deepseekSearchKey = searchKey("deepseek_search_key");
  const braveSearchKey = searchKey("brave_search_key");
  const searchStatus = h("p", {class: "bulletin-muted"});
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
  const deepseekSearchField = label("DeepSeek Search API Key（选填）", deepseekSearchKey);
  const braveSearchField = label("Brave Search API Key（选填）", braveSearchKey);
  function showSearchFields() {
    deepseekSearchField.hidden = !["auto", "deepseek"].includes(searchProvider.value);
    braveSearchField.hidden = !["auto", "brave"].includes(searchProvider.value);
  }
  searchProvider.addEventListener("change", showSearchFields);
  form.append(
    h("section", {class: "bulletin-admin-card"}, h("h3", {}, "采集与发布"),
      label("采集间隔（分钟）", interval), label("日报、周报和月报发布时间（北京时间）", publishAt),
      h("p", {class: "bulletin-muted"}, "采集到新公告时发布实时报告。日报发布前会再检查一次来源。"),
      h("fieldset", {}, h("legend", {}, "发布周期"), h("div", {class: "bulletin-checks"},
        periods.map(([kind, input]) => label(PERIODS[kind], input)))),
      h("fieldset", {}, h("legend", {}, "首页关注品种"), pairs)),
    h("section", {class: "bulletin-admin-card"}, h("h3", {}, "管理员模型"),
      label("模型预设", preset), label("API Base URL", endpoint), label("模型 ID", model), label("API Key", key),
      h("p", {class: "bulletin-muted"}, "密钥仅供平台播报使用，用户侧不会收到模型配置。")),
    h("section", {class: "bulletin-admin-card bulletin-source-card"}, h("h3", {}, "联网新闻搜索（选填）"),
      label("搜索服务", searchProvider), deepseekSearchField, braveSearchField,
      h("p", {class: "bulletin-muted"}, "自动选择优先用 DeepSeek，其次用 Brave。DeepSeek 可复用官方模型密钥，Brave 可读取服务器环境变量。RSS 和日历可独立使用。"),
      searchStatus),
    h("section", {class: "bulletin-admin-card bulletin-source-card"}, h("h3", {}, "订阅来源"),
      h("p", {class: "bulletin-muted"}, "可替换任一来源的公网 HTTPS 地址。已有默认地址的订阅留空会恢复默认地址，补充媒体留空则关闭。"),
      sources),
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
      h("section", {class: "bulletin-admin-card"}, h("h3", {}, "近期发布日历"), calendar)),
    h("section", {class: "bulletin-admin-card"}, h("h3", {}, "已发布播报"), reports));
  operations.append(h("button", {type: "button", onClick: () => request({action: "scan"})}, "立即采集"));
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
      searchProvider.value = config.search_provider;
      deepseekSearchKey.value = braveSearchKey.value = "";
      showSearchFields();
      presets = data.presets;
      preset.replaceChildren(h("option", {value: ""}, "自定义"),
        ...presets.map((item, index) => h("option", {value: String(index)}, item.name)));
      syncPreset();
      periods.forEach(([kind, input]) => { input.checked = config.periods.includes(kind); });
      pairs.replaceChildren(...data.available_pairs.map(pair => {
        const input = h("input", {type: "checkbox", value: pair, checked: config.pairs.includes(pair)});
        return label(pair, input);
      }));
      sources.replaceChildren(...data.sources.map(source => {
        const input = h("input", {type: "url", id: "bulletin-source-" + source.id, value: config.sources[source.id] ?? source.default_url,
          placeholder: source.default_url, dataset: {source: source.id}});
        const feedback = h("small", {role: "status"});
        const test = h("button", {type: "button", onClick: async () => {
          if (!input.reportValidity()) return;
          const url = input.value.trim();
          test.disabled = true;
          feedback.textContent = "正在检查…";
          try {
            const result = await bridge.apiPost("market-bulletin", {action: "check-source", source: source.id, url});
            if (input.value.trim() === url) {
              feedback.textContent = result.message;
              feedback.dataset.state = result.ok ? "ok" : "error";
            }
          } catch (error) {
            if (input.value.trim() === url) feedback.textContent = error.message;
          } finally { test.disabled = false; }
        }}, "检查连通性");
        input.addEventListener("input", () => { feedback.textContent = ""; });
        const alternate = source.alternative ? h("button", {type: "button", onClick: () => {
          input.value = source.alternative[1];
          input.dispatchEvent(new Event("input", {bubbles: true}));
          input.focus();
        }}, "填入备用地址") : null;
        return h("div", {class: "bulletin-source-row"},
          h("label", {for: input.id}, source.label + " · " + source.format),
          h("div", {class: "bulletin-source-controls"}, input,
            h("div", {class: "bulletin-actions"}, test, alternate)),
          source.alternative ? h("small", {class: "bulletin-muted"}, source.alternative[0]) : null, feedback);
      }));
      dirty = false;
    }
    key.placeholder = config.has_key ? "已配置，留空保留" : "请输入管理员 API Key";
    deepseekSearchKey.placeholder = data.search.has_deepseek_search_key ? "已配置，留空保留" : "留空可复用 DeepSeek 官方模型密钥";
    braveSearchKey.placeholder = data.search.has_brave_search_key ? "已配置，留空保留" :
      data.search.brave_env ? "已设置服务器环境变量" : "留空可使用 BRAVE_SEARCH_API_KEY";
    searchStatus.textContent = data.search.active ? "已保存的设置使用 " + searchProviders[data.search.active] :
      config.search_provider === "off" ? "已关闭联网搜索" : "目前缺少所选服务的密钥";
    signal.textContent = !config.enabled ? "已停用" : config.model_ready ? "已启用" : "已启用 · 待配置模型";
    signal.dataset.state = config.enabled && config.model_ready ? "live" : "stale";
    runs.replaceChildren(...Object.entries(RUNS).flatMap(([kind, label]) => {
      const row = data.runs.find(run => run.kind === kind);
      return [h("dt", {}, label), h("dd", {}, time(row?.attempted),
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
      h("p", {class: "bulletin-calendar-row"}, h("time", {}, time(item.due)), h("span", {}, item.title, item.source?.startsWith("非官方") ? h("small", {}, " · " + item.source) : null)))
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
      model: model.value, url: endpoint.value, key: key.value, search_provider: searchProvider.value,
      deepseek_search_key: deepseekSearchKey.value, brave_search_key: braveSearchKey.value,
      sources: Object.fromEntries([...sources.querySelectorAll("input[data-source]")].map(input =>
        [input.dataset.source, input.value.trim()])),
      periods: periods.filter(([, input]) => input.checked).map(([kind]) => kind),
      pairs: [...pairs.querySelectorAll(":checked")].map(input => input.value)}, true);
  }
  setInterval(() => { if (initial && !root.hidden && !document.hidden && !dirty) request(undefined, false, true); }, 30000);
  return () => request(undefined, false, initial);
}
