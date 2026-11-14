export function mountAgentMode(container, onChange) {
  container.innerHTML = `<section class="profile-settings"><form>
    <h3>Agent 运行模式</h3>
    <label>对话由谁负责<select name="mode"><option value="platform">平台 Agent</option><option value="external">外部 Agent</option></select></label>
    <fieldset data-external hidden><legend>平台后台任务</legend>
      <label><input type="checkbox" name="auto">定时看盘</label>
      <label><input type="checkbox" name="diary">自动日记</label>
      <small>启用前请在后台人格和模型页保存配置。</small>
    </fieldset>
    <button class="primary" type="submit">保存模式</button>
    <p role="status"></p>
  </form></section><section class="panel external-connection" hidden>
    <h3>外部连接</h3><p data-user></p>
    <label>接口地址<input data-endpoint readonly></label>
    <p data-expiry></p>
    <button type="button" data-issue>生成或更换一周令牌</button>
    <button type="button" data-revoke>撤销令牌</button>
    <label data-token-row hidden>令牌（仅显示一次）<input data-token readonly></label>
    <p>发送 POST 请求，使用 Authorization: Bearer &lt;token&gt; 和 JSON 请求体。发送 {} 查看工具清单。</p>
  </section>`;
  const find = selector => container.querySelector(selector);
  const form = find("form"), status = find("[role=status]");
  let config;
  async function request(action, data) {
    const response = await fetch("/api/" + action, data ? {
      method:"POST", headers:{"Content-Type":"application/json"}, body:JSON.stringify(data),
    } : {});
    const result = await response.json();
    if (!response.ok) throw Error(result.error || "请求失败");
    return result;
  }
  function render(data) {
    config = data;
    for (const name of ["auto","diary"]) form.elements[name].checked = data[name];
    form.elements.mode.value = data.mode;
    form.elements.mode.onchange();
    find(".external-connection").hidden = data.mode !== "external";
    find("[data-user]").textContent = "绑定用户 · " + data.username;
    find("[data-endpoint]").value = data.endpoint;
    find("[data-expiry]").textContent = data.expires ? "到期时间 · " + new Date(data.expires * 1000).toLocaleString() : "暂无有效令牌";
    find("[data-revoke]").disabled = !data.expires;
    if (data.mode !== "external") {
      find("[data-token]").value = "";
      find("[data-token-row]").hidden = true;
    }
    onChange(data, form.elements.mode.value);
  }
  form.elements.mode.onchange = () => {
    find("[data-external]").hidden = form.elements.mode.value !== "external";
    onChange(config, form.elements.mode.value);
  };
  form.onsubmit = async event => {
    event.preventDefault();
    const button = event.submitter;
    button.disabled = true;
    try {
      render(await request("agent-mode", {mode:form.elements.mode.value,
        auto:form.elements.auto.checked, diary:form.elements.diary.checked}));
      status.textContent = "已保存";
    } catch(error) { status.textContent = error.message; }
    finally { button.disabled = false; }
  };
  for (const action of ["issue","revoke"]) {
    const button = find("[data-" + action + "]");
    button.onclick = async () => {
      button.disabled = true;
      try {
        const result = await request("external-token", {action});
        find("[data-token-row]").hidden = !result.token;
        find("[data-token]").value = result.token || "";
        render({...config, expires:result.expires});
        status.textContent = action === "issue" ? "令牌已生成，请保存。" : "令牌已撤销";
      } catch(error) { status.textContent = error.message; }
      finally { if (action === "issue") button.disabled = false; }
    };
  }
  const button = document.createElement("button");
  button.type = "button";
  button.textContent = "运行模式";
  button.onclick = () => request("agent-mode").then(render).catch(error => { status.textContent = error.message; });
  return button;
}
