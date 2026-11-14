/* Thin fetch wrapper for the community/AI endpoints. Contract is the backend's; nothing is renamed here. */
export class ApiError extends Error {
  constructor(message, status = 0, kind = "http") { super(message); this.status = status; this.kind = kind; }
}

export async function api(path, {method = "GET", query, body, signal, timeout = 20000, headers = {}} = {}) {
  const params = new URLSearchParams();
  for (const [key, value] of Object.entries(query || {})) {
    if (value !== undefined && value !== null && value !== "") params.set(key, String(value));
  }
  const url = "/api/" + path + (params.size ? "?" + params : "");
  const control = new AbortController();
  const relay = () => control.abort(signal.reason);
  if (signal) signal.aborted ? control.abort(signal.reason) : signal.addEventListener("abort", relay, {once: true});
  const timer = setTimeout(() => control.abort(new DOMException("timeout", "TimeoutError")), timeout);
  let response;
  try {
    response = await fetch(url, {
      method, credentials: "same-origin", cache: "no-store", signal: control.signal,
      headers: body === undefined ? headers : {...headers, "Content-Type": "application/json"},
      body: body === undefined ? undefined : JSON.stringify(body),
    });
  } catch (error) {
    if (signal?.aborted) throw error;
    if (control.signal.reason?.name === "TimeoutError") throw new ApiError("请求超时，请稍后重试", 0, "timeout");
    throw new ApiError("无法连接服务器，请确认服务正在运行后重试", 0, "network");
  } finally {
    clearTimeout(timer);
    signal?.removeEventListener("abort", relay);
  }
  let data = null;
  try { data = await response.json(); } catch { data = null; }
  if (response.status === 401) {
    location.replace("/");
    throw new ApiError("登录已过期，请重新登录", 401);
  }
  if (!response.ok) throw new ApiError(data?.error || `请求失败（${response.status}）`, response.status);
  if (data === null) throw new ApiError("服务器返回了无法识别的数据", response.status);
  return data;
}

export const forumApi = {
  list: (query, signal) => api("forum", {query, signal}),
  thread: (id, signal) => api("forum", {query: {post: id}, signal}),
  create: body => api("forum", {method: "POST", body}),
  comment: (id, body) => api("forum", {method: "POST", query: {post: id}, body}),
  removePost: id => api("forum", {method: "DELETE", query: {post: id}}),
  removeComment: id => api("forum", {method: "DELETE", query: {comment: id}}),
  shareReview: (body, game) => api("forum/share-review", {method: "POST", query: {game}, body, timeout: 60000}),
};

export const groupApi = {
  list: signal => api("groups", {signal}),
  detail: (id, limit = 80, signal) => api("groups", {query: {room: id, limit}, signal}),
  create: (name, privateRoom) => api("groups", {method: "POST", body: {name, private: privateRoom}}),
  act: (id, action, params) => api("groups", {method: "POST", query: {room: id}, body: {action, ...params}}),
  send: (id, body) => api("groups", {method: "POST", query: {room: id}, body: {body}}),
  markRead: id => api("groups", {method: "POST", query: {room: id}, body: {action: "mark-read"}}),
};

export const aiApi = {
  tradeReview: (body, game) => api("ai/trade-review", {method: "POST", query: {game}, body, timeout: 60000}),
  groupSummary: (room, game) => api("ai/group-summary", {method: "POST", query: {game}, body: {room}, timeout: 60000}),
  forumDraft: (body, game) => api("ai/forum-draft", {method: "POST", query: {game}, body, timeout: 60000}),
  dailySummary: signal => api("ai/community-daily-summary", {signal}),
  tradingSummary: (query, signal) => api("trading-summary", {query, signal, timeout: 60000}),
};

export const communityApi = {
  summary: signal => api("community-summary", {signal}),
  profile: () => api("state"),
};