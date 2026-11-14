export function mountProfile(gameId) {
  const dialog = document.createElement("dialog");
  dialog.className = "standalone-profile";
  dialog.innerHTML = `
    <header><h2>人格、模型与聊天</h2><button type="button" data-close>关闭</button></header>
    <form data-profile>
      <label>人格设定<textarea name="persona" rows="4" maxlength="4000"></textarea></label>
      <label>OpenAI 兼容接口地址<input name="url" placeholder="默认使用 OpenAI"></label>
      <label>模型名称<input name="model"></label>
      <label>API Key<input type="password" name="key" autocomplete="new-password"></label>
      <small data-key></small>
      <label><input type="checkbox" name="clear"> 清除已保存的 Key</label>
      <button type="submit">保存设置</button>
    </form>
    <form data-chat>
      <h3>和久留美聊天</h3>
      <div class="standalone-messages" data-messages aria-live="polite"></div>
      <label>消息<textarea name="message" rows="2" maxlength="2000" required></textarea></label>
      <button type="submit">发送</button>
    </form>
    <p role="status" data-status></p>`;
  document.body.append(dialog);
  const profile = dialog.querySelector("[data-profile]");
  const chat = dialog.querySelector("[data-chat]");
  const status = dialog.querySelector("[data-status]");
  const messages = dialog.querySelector("[data-messages]");
  dialog.querySelector("[data-close]").onclick = () => dialog.close();
  async function request(action, gid, body) {
    const response = await fetch("/api/" + action + "?" + new URLSearchParams({game: gid}),
      body === undefined ? {} : {method: "POST", headers: {"Content-Type": "application/json"},
        body: JSON.stringify(body)});
    const result = await response.json();
    if (!response.ok) throw Error(result.error || "请求失败");
    return result;
  }
  async function refresh(gid, settings) {
    const result = await request("state", gid);
    if (settings) {
      for (const [name, value] of Object.entries(result.profile)) profile.elements[name].value = value;
      dialog.querySelector("[data-key]").textContent = result.has_key ? "已保存 Key，留空保留" : "尚未保存 Key";
    }
    messages.replaceChildren(...result.chat.map(message => {
      const row = document.createElement("p");
      row.textContent = (message.role === "user" ? "你：" : "久留美：") + message.content;
      return row;
    }));
    messages.scrollTop = messages.scrollHeight;
  }
  let busy = false;
  function submit(form, action, update) {
    form.onsubmit = async event => {
      event.preventDefault();
      if (busy) return;
      busy = true;
      const button = event.submitter;
      button.disabled = true;
      const gid = gameId();
      status.textContent = action === "chat" ? "久留美正在回复…" : "正在保存…";
      try {
        await request(action, gid, Object.fromEntries(new FormData(form)));
        if (action === "profile") {
          profile.elements.key.value = "";
          profile.elements.clear.checked = false;
        } else chat.reset();
        await refresh(gid, update);
        status.textContent = action === "profile" ? "设置已保存" : "";
      } catch (error) { status.textContent = error.message; }
      finally { button.disabled = false; busy = false; }
    };
  }
  submit(profile, "profile", true);
  submit(chat, "chat", false);
  const button = document.createElement("button");
  button.textContent = "聊天与人格";
  button.type = "button";
  button.onclick = async () => {
    if (busy) return;
    status.textContent = "";
    dialog.showModal();
    try { await refresh(gameId(), true); } catch (error) { status.textContent = error.message; }
  };
  return button;
}
