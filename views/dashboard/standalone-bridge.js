export function createAdminBridge() {
  async function request(route, params = {}, body) {
    const headers = {Authorization: "Bearer " + (sessionStorage.getItem("kurumi-admin") || "")};
    if (body !== undefined) headers["Content-Type"] = "application/json";
    const query = new URLSearchParams(params);
    const response = await fetch("/api/" + route + "?" + query, {
      headers, method: body === undefined ? "GET" : "POST", body: JSON.stringify(body),
    });
    const result = await response.json();
    if (response.status === 401) { location.replace("/"); throw Error("请登录管理员账号"); }
    if (!response.ok) throw Error(result.error || "操作失败");
    return result;
  }
  return {
    ready: async () => {},
    apiGet: (route, params) => request(route === "state" ? "dashboard" : route, params),
    apiPost: (route, body) => request(route, {}, body),
  };
}
