/* Safe DOM helpers. User content is only ever inserted as text nodes. */
const stamp = new Intl.DateTimeFormat("zh-CN", {
  timeZone: "Asia/Shanghai", year: "numeric", month: "2-digit", day: "2-digit",
  hour: "2-digit", minute: "2-digit", hour12: false,
});
const PROPS = /^(value|disabled|checked|type|hidden|title|rows|min|max|step|placeholder|name|id|required|selected|href)$/;

export function append(parent, kids) {
  for (const kid of kids) {
    if (kid === null || kid === undefined || kid === false) continue;
    if (Array.isArray(kid)) append(parent, kid);
    else if (kid instanceof Node) parent.append(kid);
    else parent.append(document.createTextNode(String(kid)));
  }
  return parent;
}

export function h(tag, props, ...kids) {
  const el = document.createElement(tag);
  let value;
  for (const [key, val] of Object.entries(props || {})) {
    if (val === undefined || val === null || val === false) continue;
    if (key === "class") el.className = val;
    else if (key === "text") el.textContent = val;
    else if (key === "dataset") Object.assign(el.dataset, val);
    else if (key === "for") el.htmlFor = val;
    else if (key === "value") value = val;
    else if (key.startsWith("on") && typeof val === "function") el.addEventListener(key.slice(2).toLowerCase(), val);
    else if (PROPS.test(key)) el[key] = val;
    else el.setAttribute(key, val === true ? "" : String(val));
  }
  append(el, kids);
  if (value !== undefined) el.value = value;
  return el;
}

export const clear = el => { el.replaceChildren(); return el; };

export function formatTime(ts) {
  const n = Number(ts);
  if (!Number.isFinite(n) || n <= 0) return "暂无";
  return stamp.format(new Date(n * 1000)).replace(/\//g, "-");
}

const sameDay = (a, b) => a.getFullYear() === b.getFullYear() && a.getMonth() === b.getMonth() && a.getDate() === b.getDate();
/** Chat-style clock: HH:mm today, MM-DD HH:mm otherwise. */
export function clockTime(ts, now = new Date()) {
  const n = Number(ts);
  if (!Number.isFinite(n) || n <= 0) return "";
  const date = new Date(n * 1000);
  const pad = x => String(x).padStart(2, "0");
  const hm = `${pad(date.getHours())}:${pad(date.getMinutes())}`;
  return sameDay(date, now) ? hm : `${pad(date.getMonth() + 1)}-${pad(date.getDate())} ${hm}`;
}

export function relativeTime(ts, now = Date.now()) {
  const n = Number(ts);
  if (!Number.isFinite(n) || n <= 0) return "暂无";
  const s = Math.max(0, Math.round(now / 1000 - n));
  if (s < 45) return "刚刚";
  if (s < 3600) return Math.round(s / 60) + " 分钟前";
  if (s < 86400) return Math.floor(s / 3600) + " 小时前";
  if (s < 7 * 86400) return Math.floor(s / 86400) + " 天前";
  return formatTime(n).slice(0, 10);
}

export function timeEl(ts) {
  const n = Number(ts);
  if (!Number.isFinite(n) || n <= 0) return h("span", {class: "ui-muted"}, "暂无");
  return h("time", {datetime: new Date(n * 1000).toISOString(), title: formatTime(n)}, relativeTime(n));
}

export function parseTags(raw) {
  const seen = new Set();
  const tags = [];
  for (const part of String(raw || "").split(/[,，、;；\n]+/)) {
    const tag = part.trim();
    if (!tag || seen.has(tag.toLowerCase())) continue;
    seen.add(tag.toLowerCase());
    tags.push(tag);
  }
  if (tags.length > 10) return {tags, error: "标签最多 10 个"};
  if (tags.some(tag => tag.length > 40)) return {tags, error: "单个标签不超过 40 字"};
  return {tags, error: ""};
}

/** Split text around keyword matches; matches become <mark>, everything stays text. */
export function highlight(text, keyword) {
  const value = String(text ?? "");
  const needle = String(keyword ?? "").trim();
  if (!needle) return [value];
  const lower = value.toLowerCase();
  const key = needle.toLowerCase();
  if (lower.length !== value.length) return [value];
  const parts = [];
  let at = 0, hit;
  while ((hit = lower.indexOf(key, at)) !== -1) {
    if (hit > at) parts.push(value.slice(at, hit));
    parts.push(h("mark", {}, value.slice(hit, hit + key.length)));
    at = hit + key.length;
  }
  parts.push(value.slice(at));
  return parts;
}

export function debounce(fn, ms) {
  let timer = 0;
  const wrapped = (...args) => { clearTimeout(timer); timer = setTimeout(() => fn(...args), ms); };
  wrapped.cancel = () => clearTimeout(timer);
  return wrapped;
}

export const money = value => Number(value).toLocaleString("en-US", {minimumFractionDigits: 2, maximumFractionDigits: 2});
export const isAbort = error => error?.name === "AbortError";
let counter = 0;
export const uid = prefix => `${prefix}-${++counter}`;