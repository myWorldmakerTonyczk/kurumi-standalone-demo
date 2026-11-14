export function mountPreferences(gameId, {container, group: selectedGroup} = {}) {
  const dialog = document.createElement(container ? "section" : "dialog");
  dialog.className = "standalone-profile user-preferences";
  dialog.innerHTML = '<header><h2>我的设置</h2><button type="button" data-close>关闭</button></header><form></form><p role="status"></p><section data-reports></section>';
  (container || document.body).append(dialog);
  if (container) dialog.classList.add("inline-user-settings");
  const form = dialog.querySelector("form");
  const status = dialog.querySelector("[role=status]");
  const reports = dialog.querySelector("[data-reports]");
  dialog.querySelector("[data-close]").hidden = Boolean(container);
  dialog.querySelector("[data-close]").onclick = () => dialog.close();

  const groups = {
    root: ["自动看盘", {auto:"启用自动看盘", interval:"看盘间隔（分钟）"}],
    rest: ["看盘休息", {weekend_pause:"在指定时段暂停自动看盘", weekend_zone:"休息时区",
      weekend_start_day:"开始日", weekend_start_time:"开始时间", weekend_end_day:"结束日", weekend_end_time:"结束时间"}],
    notices: ["主动汇报", {enabled:"接收站内汇报", analysis:"看盘完成", liquidation:"模拟强平",
      margin_call:"保证金不足", deposit:"追加虚拟资金", end:"本局结束", use_window:"仅在指定时段发送看盘汇报",
      random_delay:"在时段内随机发送", window_start:"汇报开始", window_end:"汇报结束", quiet_start:"免打扰开始",
      quiet_end:"免打扰结束", timezone:"通知时区"}],
    diary: ["每日日记", {enabled:"启用日记", time:"写作时间", timezone:"日记时区", min_logs:"日志数量须大于",
      chat_only:"没有交易但有聊天时也写日记", history_count:"参考聊天条数", send_to_owner:"在站内汇报列表显示已完成的日记"}],
  };
  async function request(action, body) {
    const query = new URLSearchParams({game: gameId()});
    const response = await fetch("/api/" + action + "?" + query, body ? {
      method:"POST", headers:{"Content-Type":"application/json"}, body:JSON.stringify(body),
    } : {});
    const result = await response.json();
    if (!response.ok) throw Error(result.error || "请求失败");
    return result;
  }
  function render(values) {
    form.replaceChildren();
    for (const [group, [title, fields]] of Object.entries(groups)) {
      if (selectedGroup && group !== selectedGroup) continue;
      const section = document.createElement("fieldset");
      const legend = document.createElement("legend");
      legend.textContent = title;
      section.append(legend);
      for (const [name, text] of Object.entries(fields)) {
        const label = document.createElement("label");
        const value = group === "root" ? values[name] : values[group][name];
        const input = document.createElement(name.endsWith("_day") ? "select" : "input");
        input.name = group + "." + name;
        if (input.tagName === "SELECT") {
          "一二三四五六日".split("").forEach((day, index) => input.add(new Option("周" + day, index)));
          input.value = value;
        } else {
          input.type = typeof value === "boolean" ? "checkbox" : typeof value === "number" ? "number" :
            /^(\d\d):(\d\d)$/.test(value) ? "time" : "text";
          input.checked = value === true;
          input.value = name === "interval" ? value / 60 : value;
          if (name === "interval") { input.min = 1; input.max = 1440; }
          input.required = input.type !== "checkbox";
        }
        label.append(text, input);
        section.append(label);
      }
      form.append(section);
    }
    const save = document.createElement("button");
    save.textContent = "保存我的设置";
    form.append(save);
  }
  form.onsubmit = async event => {
    event.preventDefault();
    const save = event.submitter;
    save.disabled = true;
    try {
      const values = await request("preferences");
      for (const element of form.elements) {
        if (!element.name) continue;
        const [group, name] = element.name.split(".");
        const target = group === "root" ? values : values[group];
        target[name] = element.type === "checkbox" ? element.checked :
          typeof target[name] === "number" ? Number(element.value) * (name === "interval" ? 60 : 1) : element.value;
      }
      await request("preferences", values);
      status.textContent = "设置已保存";
    } catch (error) { status.textContent = error.message; }
    finally { save.disabled = false; }
  };
  const button = document.createElement("button");
  button.type = "button";
  button.textContent = groups[selectedGroup]?.[0] || "我的设置";
  if (selectedGroup) dialog.querySelector("h2").textContent = groups[selectedGroup][0];
  button.onclick = async () => {
    if (!container) dialog.showModal();
    form.replaceChildren();
    reports.replaceChildren();
    status.textContent = "正在加载…";
    try {
      render(await request("preferences"));
      const {entries} = selectedGroup === "notices" || !selectedGroup ? await request("reports") : {entries: []};
      reports.hidden = Boolean(selectedGroup && selectedGroup !== "notices");
      const heading = document.createElement("h3");
      heading.textContent = "站内汇报";
      reports.append(heading);
      for (const entry of entries) {
        const item = document.createElement("p");
        item.textContent = entry.text;
        reports.append(item);
      }
      if (!entries.length) reports.append("暂无汇报");
      status.textContent = "";
    } catch (error) { status.textContent = error.message; }
  };
  return button;
}
