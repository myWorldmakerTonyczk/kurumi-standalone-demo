const token = new URLSearchParams(location.hash.slice(1)).get("token");
if (token) {
  sessionStorage.setItem("kurumi-admin", token);
  history.replaceState(null, "", location.pathname);
}

export function createAdminBridge() {
  async function request(route, params = {}, body) {
    const headers = {Authorization: "Bearer " + (sessionStorage.getItem("kurumi-admin") || "")};
    if (body !== undefined) headers["Content-Type"] = "application/json";
    const query = new URLSearchParams(params);
    const response = await fetch("/api/" + route + "?" + query, {
      headers, method: body === undefined ? "GET" : "POST", body: JSON.stringify(body),
    });
    const result = await response.json();
    if (response.status === 401) throw Error("请打开启动终端打印的完整管理员链接");
    if (!response.ok) throw Error(result.error || "操作失败");
    return result;
  }
  return {
    ready: async () => {},
    apiGet: (route, params) => request(route === "state" ? "dashboard" : route, params),
    apiPost: (route, body) => request(route, {}, body),
  };
}
