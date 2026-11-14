sessionStorage.removeItem("kurumi-admin");
const setupToken = new URLSearchParams(location.hash.slice(1)).get("setup");
if (location.hash) history.replaceState(null, "", location.pathname);

const form = document.getElementById("portal-login");
const status = document.getElementById("status");

try {
  const response = await fetch("/api/login-mode", {cache: "no-store"});
  if (!response.ok) throw Error("无法读取登录状态");
  const {setup} = await response.json();
  document.querySelector("h1").textContent = setup ? "设置管理员账号" : "管理员登录";
  document.getElementById("hint").textContent = setup ? "设置账号和至少 12 位密码。若 Token 未填入，请从终端复制。" : "";
  document.getElementById("setup-token").hidden = !setup;
  form.elements.token.required = setup;
  if (setup && setupToken) form.elements.token.value = setupToken;
  form.elements.password.autocomplete = setup ? "new-password" : "current-password";
  form.elements.password.minLength = setup ? 12 : 1;
  form.querySelector("button").textContent = setup ? "保存并进入管理面板" : "登录管理面板";
  form.hidden = false;
  form.onsubmit = async event => {
    event.preventDefault();
    const button = event.submitter;
    button.disabled = true;
    try {
      const response = await fetch("/api/" + (setup ? "setup" : "login"), {
        method: "POST", headers: {"Content-Type": "application/json"},
        body: JSON.stringify(Object.fromEntries(new FormData(form))),
      });
      const result = await response.json();
      if (!response.ok) throw Error(result.error || "登录失败");
      sessionStorage.setItem("kurumi-admin", result.session);
      location.replace("/dashboard");
    } catch (error) {
      status.textContent = error.message;
    } finally {
      button.disabled = false;
    }
  };
} catch (error) {
  status.textContent = error.message;
}
