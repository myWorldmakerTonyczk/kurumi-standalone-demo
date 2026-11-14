import { encodeAgentAvatar } from "/assets/workspace-chat.js";
export function mountProfile(gameId, {container, api = false, general = false}) {
  const section = document.createElement("section");
  section.className = api ? "profile-settings api-settings" : "profile-settings";
  section.innerHTML = api ? `
    <aside><h3>提供商</h3><strong>OpenAI 兼容接口</strong><p data-endpoint></p></aside>
    <form>
      <h3>接口配置</h3>
      <label>API Base URL<input name="url" type="url" placeholder="https://api.openai.com/v1"></label>
      <label>API Key<input name="key" type="password" autocomplete="new-password"><small data-key></small></label>
      <button type="button" data-clear hidden>清除已保存的 Key</button>
      <label>模型 ID<input name="model" placeholder="填写提供商的模型 ID"></label>
      <button class="primary" type="submit" disabled>保存配置</button>
      <p role="status"></p>
    </form>` : `
    <form class="persona-card">
      <h3 data-persona-title>智能体人格</h3>
      <p data-summary></p>
      <details><summary>编辑人格</summary>
        <label>智能体名称<input name="agent_name" maxlength="40" placeholder="久留美"></label>
        <label>智能体头像<input name="agent_picture" type="file" accept="image/png,image/jpeg,image/webp"></label>
        <label><input name="reset_avatar" type="checkbox">使用默认智能体头像</label>
        <label><input name="split_reply" type="checkbox">按换行、句末标点或连续空格切分短句</label>
        <label>人格设定<textarea name="persona" rows="7" maxlength="4000" placeholder="留空使用默认久留美人格"></textarea></label>
        <button class="primary" type="submit" disabled>保存人格</button>
      </details>
      <p role="status"></p>
    </form>`;
  const split = section.querySelector('[name="split_reply"]')?.closest("label");
  if (general) {
    section.querySelector("form").replaceChildren();
    section.querySelector("form").append(split);
    const rule = document.createElement("code");
    rule.textContent = String.raw`\n+|(?<=[。！？!?])\s*|(?<=[.;])\s+|[ \t]{2,}`;
    const save = document.createElement("button");
    save.type = "submit";
    save.textContent = "保存对话设置";
    const status = document.createElement("p");
    status.setAttribute("role", "status");
    section.querySelector("form").append(rule, save, status);
  } else split?.remove();
  container.append(section);
  const form = section.querySelector("form");
  const status = section.querySelector("[role=status]");
  const save = section.querySelector("button[type=submit]");
  async function request(action, body) {
    const response = await fetch("/api/" + action + "?" + new URLSearchParams({game: gameId()}),
      body === undefined ? {} : {method: "POST", headers: {"Content-Type": "application/json"},
        body: JSON.stringify(body)});
    const result = await response.json();
    if (!response.ok) throw Error(result.error || "请求失败");
    return result;
  }
  let letAvatar;
  async function refresh() {
    const data = await request("state");
    const {profile, has_key: hasKey} = data;
    letAvatar = data.agent_avatar;
    for (const [name, value] of Object.entries(profile)) {
      if (form.elements[name]) form.elements[name].value = value;
    }
    if (general) {
      form.elements.split_reply.checked = data.split_reply !== false;
    } else if (api) {
      section.querySelector("[data-endpoint]").textContent = profile.url || "https://api.openai.com/v1";
      section.querySelector("[data-clear]").hidden = !hasKey;
      section.querySelector("[data-key]").textContent = hasKey ? "已保存，留空保留" : "尚未保存 API Key";
    } else {
      section.querySelector("[data-persona-title]").textContent = data.agent_name + "的人格";
      form.elements.agent_name.value = data.agent_name;
      section.querySelector("[data-summary]").textContent = profile.persona || "你是久留美，使用中文交流。";
    }
    save.disabled = false;
  }
  if (api) {
    const clear = section.querySelector("[data-clear]");
    clear.onclick = async () => {
      if (!window.confirm("清除已保存的 API Key？")) return;
      clear.disabled = true;
      try {
        await request("profile", {clear: true});
        form.elements.key.value = "";
        clear.hidden = true;
        section.querySelector("[data-key]").textContent = "尚未保存 API Key";
        status.textContent = "API Key 已清除";
      } catch (error) { status.textContent = error.message; }
      finally { clear.disabled = false; }
    };
  }
  form.onsubmit = async event => {
    event.preventDefault();
    save.disabled = true;
    try {
      const payload = Object.fromEntries(new FormData(form));
      if (general) {
        await request("reply-settings", {split_reply: form.elements.split_reply.checked});
        window.dispatchEvent(new Event("kurumi-identity"));
        status.textContent = "已保存";
        return;
      }
      if (!api) {
        const agentAvatar = payload.reset_avatar ? "" :
          await encodeAgentAvatar(form.elements.agent_picture.files[0]) || letAvatar;
        await request("agent-identity", {agent_name: payload.agent_name, agent_avatar: agentAvatar});
        form.elements.agent_picture.value = "";
        form.elements.reset_avatar.checked = false;
        window.dispatchEvent(new Event("kurumi-identity"));
      }
      await request("profile", api ? payload : {persona: payload.persona});
      if (api) {
        form.elements.key.value = "";

      }
      await refresh();
      status.textContent = "已保存";
    } catch (error) { status.textContent = error.message; }
    finally { save.disabled = false; }
  };
  const button = document.createElement("button");
  button.type = "button";
  button.textContent = general ? "对话格式" : api ? "模型与 API" : "人格设定";
  button.onclick = () => refresh().catch(error => { status.textContent = error.message; });
  return button;
}
