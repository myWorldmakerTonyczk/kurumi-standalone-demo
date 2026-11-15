import { signalPortalSessionChange } from "./portal-bridge.js";
import { encodeAgentAvatar } from "/assets/workspace-chat.js";
import { ReplayPanel } from "./replay-panel.js";
import { PortalTrading } from "./portal-trading.js";

const byId = id => document.getElementById(id);
const status = byId("portal-status");
const setStatus = (message, error = false) => {
  status.textContent = message;
  status.dataset.error = String(error);
};

async function api(path, body) {
  const response = await fetch("/api/" + path, {
    credentials: "same-origin",
    ...(body === undefined ? {} : {
      method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify(body),
    }),
  });
  const result = await response.json();
  if (!response.ok) {
    const error = Error(result.error || "面板请求失败");
    error.http = response.status < 500;
    throw error;
  }
  return result;
}

const replay = new ReplayPanel(byId("portal-review"), (game, options) =>
  api("review?" + new URLSearchParams({game, ...options})), (game, options) =>
  api("history?" + new URLSearchParams({game, ...options, history_source: "market"})));
const desk = new PortalTrading(byId("portal-trading"), api);
const records = byId("portal-records");
let recordRequest = 0;
const instanceSelect = byId("portal-instance");
let session = null;
let activePage = "review";

function element(tag, text) {
  const node = document.createElement(tag);
  node.textContent = text;
  return node;
}

const actions = {open: "开仓", close: "平仓", protect: "调整保护线", plan: "更新计划",
  observe: "观望", analysis: "看盘", deposit: "追加本金", borrow: "借入资金",
  repay: "偿还借款", update_pairs: "调整货币对", margin_call: "保证金不足",
  end: "本局结束", start: "开始游戏", pause: "暂停", resume: "恢复"};
const actors = {ai: "AI", human: "人类", system: "系统"};
const dates = new Intl.DateTimeFormat("zh-CN", {
  timeZone: "Asia/Shanghai", dateStyle: "short", timeStyle: "medium",
});

async function showRecords(page, game, options = {}) {
  const request = ++recordRequest;
  const journal = page === "journal";
  const title = element("h2", journal ? "交易日志" : "计划与经验");
  const controls = document.createElement("div");
  controls.className = "portal-record-controls";
  if (journal) {
    const filter = document.createElement("select");
    filter.setAttribute("aria-label", "筛选交易日志");
    filter.replaceChildren(...[["all", "全部记录"], ["operations", "实际操作"],
      ["fills", "仅成交"], ["rejected", "未执行"]].map(([value, label]) => new Option(label, value)));
    filter.value = options.filter || "all";
    filter.onchange = () => showRecords(page, game, {filter: filter.value});
    controls.append(filter);
  }
  const content = element("div", "正在读取…");
  content.className = "portal-record-body";
  records.dataset.page = page;
  records.replaceChildren(title, controls, content);
  try {
    const data = await api(page + "?" + new URLSearchParams({game, ...options}));
    if (request !== recordRequest) return;
    content.replaceChildren();
    if (journal) {
      for (const row of data.logs) {
        const item = document.createElement("article");
        item.append(element("small", dates.format(row.time * 1000) + " · " + (actors[row.source] || row.source)),
          element("h3", (actions[row.action] || row.action) + (row.status === "rejected" ? " · 未执行" : "")));
        if (row.reason) item.append(element("p", row.reason));
        content.append(item);
      }
      if (!data.logs.length) content.append(element("p", "没有符合条件的日志。"));
      for (const [label, next] of [["上一页", data.page > 1 ? data.page - 1 : null], ["下一页", data.next_page]]) {
        const button = element("button", label);
        button.disabled = !next;
        button.onclick = () => showRecords(page, game, {...options, before: data.before, page: next});
        controls.append(button);
      }
    } else {
      const plan = document.createElement("article");
      plan.append(element("h3", "当前交易计划"), element("p", data.plan || "她还没有写下交易计划。"));
      const diary = document.createElement("article");
      diary.append(element("h3", "交易日记"));
      for (const [index, entry] of data.diaries.entries()) {
        const details = document.createElement("details");
        details.open = index === 0;
        details.append(element("summary", entry.day), element("p", entry.content));
        diary.append(details);
      }
      if (!data.diaries.length) diary.append(element("p", "她还没有写好日记。"));
      content.append(plan, diary);
    }
  } catch (error) {
    if (request === recordRequest) content.textContent = error.message;
  }
}

function showPage(page) {
  recordRequest++;
  const game = session?.games?.find(item => item.id === instanceSelect.value);
  if (!game) return;
  if (session.mode === "summary" || (page === "trading" && session.mode !== "account")) page = "review";
  activePage = page;
  byId("portal-title").textContent = game.name;
  for (const button of document.querySelectorAll("[data-portal-page]")) {
    button.hidden = session.mode === "summary" && button.dataset.portalPage !== "review"
      || session.mode !== "account" && button.dataset.portalPage === "trading";
    button.setAttribute("aria-pressed", String(button.dataset.portalPage === page));
  }
  byId("portal-review").hidden = page !== "review";
  records.hidden = page !== "journal" && page !== "notebook";
  if (page === "review") replay.open(game.id, game.pairs);
  else replay.close();
  if (page === "trading") desk.open(game.id);
  else desk.close();
  if (!records.hidden) showRecords(page, game.id);
}

async function start() {
  session = await api("session");
  const games = session.games || [];
  if (games.length && ["account", "read"].includes(session.mode)) {
    location.replace("/workspace/");
    return;
  }
  byId("portal-welcome").hidden = session.authenticated;
  byId("first-game").hidden = !(session.authenticated && session.mode === "account" && !games.length);
  if (!byId("first-game").hidden) {
    byId("portal-logout").hidden = false;
    byId("portal-title").textContent = "我的交易工作区";
    setStatus("");
    return;
  }
  byId("portal-logout").hidden = !session.authenticated;
  byId("portal-tabs").hidden = session.mode !== "account" || !games.length;
  byId("portal-sidebar").hidden = !games.length;
  byId("portal-banner").hidden = !games.length;
  byId("portal-review").hidden = !games.length;
  records.hidden = true;
  instanceSelect.replaceChildren(...games.map(game => new Option(game.name, game.id)));
  if (games.length) {
    const modes = {summary: "复盘摘要", read: "本人只读面板", account: "已登录用户面板"};
    setStatus("账户已同步 · " + modes[session.mode] + " · 所有金额均为虚拟资金");
    showPage("review");
  } else {
    replay.close();
    desk.close();
    byId("portal-title").textContent = "她的交易记录";
    setStatus(session.authenticated ? "此账号没有可用实例。" : "");
  }
}

instanceSelect.onchange = () => showPage(activePage);
for (const button of document.querySelectorAll("[data-portal-page]")) {
  button.onclick = () => showPage(button.dataset.portalPage);
}

byId("portal-login").onsubmit = async event => {
  event.preventDefault();
  const form = event.currentTarget;
  const button = event.submitter;
  button.disabled = true;
  try {
    const credentials = Object.fromEntries(new FormData(form));
    await api("login", credentials);
    signalPortalSessionChange();
    form.reset();
    await start();
  } catch (error) {
    setStatus(error.message, true);
  } finally {
    button.disabled = false;
  }
};

byId("portal-logout").onclick = async () => {
  try { await api("logout", {}); signalPortalSessionChange(); location.reload(); }
  catch (error) { setStatus(error.message, true); }
};

function handleShare() {
  const share = new URLSearchParams(location.hash.slice(1)).get("share");
  if (location.hash) history.replaceState(null, "", location.pathname);
  if (!share) return false;
  replay.close();
  desk.close();
  recordRequest++;
  records.hidden = true;
  byId("portal-sidebar").hidden = true;
  byId("portal-banner").hidden = true;
  byId("portal-review").hidden = true;
  byId("portal-tabs").hidden = true;
  byId("portal-welcome").hidden = false;
  byId("portal-logout").hidden = true;
  const button = byId("portal-exchange");
  byId("portal-welcome-copy").textContent = "这条链接只能兑换一次，点击后在此浏览器打开。";
  byId("portal-login").hidden = true;
  button.hidden = false;
  button.onclick = async () => {
    button.disabled = true;
    try { await api("exchange", {token: share}); await start(); }
    catch (error) { setStatus(error.message, true); }
    finally { button.disabled = false; }
  };
  return true;
}

const avatarFile = byId("avatar-file");
const editor = byId("avatar-editor");
const crop = byId("avatar-crop");
const size = byId("avatar-size");
const context = crop.getContext("2d");
let photo, drag, savedAvatar = "";
let x, y, radius;

function renderCrop() {
  radius = Math.min(crop.width, crop.height) * Number(size.value) / 200;
  x = Math.max(radius, Math.min(crop.width - radius, x));
  y = Math.max(radius, Math.min(crop.height - radius, y));
  context.drawImage(photo, 0, 0, crop.width, crop.height);
  context.fillStyle = "#23152999";
  context.beginPath();
  context.rect(0, 0, crop.width, crop.height);
  context.arc(x, y, radius, 0, Math.PI * 2);
  context.fill("evenodd");
  context.beginPath();
  context.arc(x, y, radius, 0, Math.PI * 2);
  context.lineWidth = 2;
  context.strokeStyle = "#fff";
  context.stroke();
}

function moveCrop(event) {
  const rect = crop.getBoundingClientRect();
  x = (event.clientX - rect.left) * crop.width / rect.width - drag.x;
  y = (event.clientY - rect.top) * crop.height / rect.height - drag.y;
  renderCrop();
}

crop.onpointerdown = event => {
  const rect = crop.getBoundingClientRect();
  const px = (event.clientX - rect.left) * crop.width / rect.width;
  const py = (event.clientY - rect.top) * crop.height / rect.height;
  drag = Math.hypot(px - x, py - y) <= radius ? {x: px - x, y: py - y} : {x: 0, y: 0};
  crop.setPointerCapture(event.pointerId);
  moveCrop(event);
};
crop.onpointermove = event => { if (drag) moveCrop(event); };
crop.onlostpointercapture = () => { drag = null; };
crop.onkeydown = event => {
  const offset = {ArrowLeft: [-1, 0], ArrowRight: [1, 0], ArrowUp: [0, -1], ArrowDown: [0, 1]}[event.key];
  if (!offset) return;
  event.preventDefault();
  x += offset[0] * 3;
  y += offset[1] * 3;
  renderCrop();
};
size.oninput = renderCrop;
byId("choose-avatar").onclick = () => avatarFile.click();
byId("cancel-avatar").onclick = () => editor.close();
editor.onclose = () => { photo?.close(); photo = null; drag = null; };
function updateAvatar(value) {
  savedAvatar = value;
  byId("avatar-preview").src = value || "/assets/user-avatar.svg";
  byId("reset-avatar").hidden = !value;
}
byId("reset-avatar").onclick = () => updateAvatar("");
byId("apply-avatar").onclick = () => {
  const output = document.createElement("canvas");
  output.width = output.height = 128;
  const draw = output.getContext("2d");
  draw.beginPath();
  draw.arc(64, 64, 64, 0, Math.PI * 2);
  draw.clip();
  const scaleX = photo.width / crop.width;
  const scaleY = photo.height / crop.height;
  draw.drawImage(photo, (x-radius)*scaleX, (y-radius)*scaleY, 2*radius*scaleX, 2*radius*scaleY, 0, 0, 128, 128);
  updateAvatar(output.toDataURL("image/webp", .76));
  editor.close();
};
avatarFile.onchange = async () => {
  const file = avatarFile.files[0];
  avatarFile.value = "";
  if (!file) return;
  try {
    if (file.size > 5 * 1024 * 1024 || !["image/png", "image/jpeg", "image/webp"].includes(file.type))
      throw Error("请选择不超过 5 MB 的 PNG、JPEG 或 WebP 图片。");
    photo = await createImageBitmap(file);
    const scale = Math.min(480 / photo.width, 350 / photo.height);
    crop.width = Math.max(1, Math.round(photo.width * scale));
    crop.height = Math.max(1, Math.round(photo.height * scale));
    x = crop.width / 2;
    y = crop.height / 2;
    size.value = 75;
    renderCrop();
    setStatus("");
    editor.showModal();
  } catch (error) { setStatus(error.message || "无法读取图片", true); }
};

let setupStep = -1;
const onboarding = byId("first-game-form");
onboarding.elements.mode.onchange = () => {
  byId("setup-background").hidden = onboarding.elements.mode.value !== "external";
};
let presets = [];
const providerChoice = byId("setup-provider");
const modelChoice = byId("setup-model-choice");
const customModel = byId("setup-custom-model");
providerChoice.onchange = () => {
  const preset = presets[Number(providerChoice.value)];
  modelChoice.replaceChildren(new Option(preset.model, preset.model), new Option("自定义", "custom"));
  modelChoice.onchange();
};
modelChoice.onchange = () => { customModel.hidden = modelChoice.value !== "custom"; };
byId("first-game-form").onsubmit = async event => {
  event.preventDefault();
  const button = event.submitter;
  button.disabled = true;
  try {
    const form = byId("first-game-form");
    if (setupStep === -1) {
      byId("setup-agent-mode").hidden = true;
      byId("setup-profile").hidden = false;
      setupStep = 0;
      form.elements.nickname.required = true;
      form.elements.nickname.focus();
      return;
    }
    const external = form.elements.mode.value === "external";
    const background = external && (form.elements.auto.checked || form.elements.diary.checked);
    if (setupStep === 0) {
      const config = await api("model-presets");
      presets = config.presets;
      form.elements.agent_name.value = config.agent_name;
      form.elements.persona.value = config.persona;
      providerChoice.replaceChildren(...presets.map((preset, index) => new Option(preset.name, index)));
      providerChoice.onchange();
    }
    if (setupStep === 0 || (setupStep === 1 && (!external || background))) {
      setupStep++;
      ["setup-profile", "setup-persona", "setup-model"].forEach((id, index) => {
        byId(id).hidden = index !== setupStep;
      });
      byId("first-game").querySelector("h2").textContent = setupStep === 1 ? "设置智能体人格" : "配置模型 API";
      button.textContent = setupStep === 1 && (!external || background) ? "下一步" : "进入交易工作区";
      (setupStep === 1 ? form.elements.agent_name : form.elements.key).focus();
      setStatus("");
      return;
    }
    const payload = Object.fromEntries(new FormData(form));
    const preset = presets[Number(providerChoice.value)];
    payload.auto = external && form.elements.auto.checked;
    payload.diary = external && form.elements.diary.checked;
    if (background && !payload.key.trim()) throw Error("请填写后台 API Key。");
    const model = modelChoice.value === "custom" ? payload.model.trim() : modelChoice.value;
    if (payload.key.trim() && !model) {
      const input = customModel.querySelector("input");
      input.focus();
      throw Error("请输入自定义模型 ID。");
    }
    delete payload.model;
    if (payload.key.trim()) Object.assign(payload, {url: preset.url, model});
    payload.avatar = savedAvatar;
    payload.agent_avatar = await encodeAgentAvatar(form.elements.agent_picture.files[0]);
    delete payload.agent_picture;
    await api("onboarding", payload);
    location.replace(external ? "/workspace/#agent-settings" : "/workspace/");
  } catch (error) { setStatus(error.message, true); }
  finally { button.disabled = false; }
};

window.addEventListener("hashchange", handleShare);
if (!handleShare()) start().catch(error => setStatus(error.message, true));
