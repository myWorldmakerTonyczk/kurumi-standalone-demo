import { encodeAgentAvatar } from "/assets/workspace-chat.js";
export function mountProfile(gameId, {container, general = false}) {
  const section = document.createElement("section");
  section.className = "profile-settings";
  section.innerHTML = `
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
    const rule = document.createElement("input");
    rule.name = "split_pattern";
    rule.maxLength = 240;
    rule.setAttribute("aria-label", "短句切分正则");
    rule.placeholder = String.raw`\n+|(?<=[。！？!?])\s*|(?<=[.;])\s+|[ \t]{2,}`;
    const save = document.createElement("button");
    save.type = "submit";
    save.textContent = "保存对话设置";
    const status = document.createElement("p");
    status.setAttribute("role", "status");
    const reset = document.createElement("button");
    reset.type = "button";
    reset.textContent = "恢复默认正则";
    reset.onclick = () => { rule.value = rule.placeholder; };
    const label = document.createElement("label");
    label.append("短句切分正则", rule);
    const delays = document.createElement("div");
    delays.className = "split-delay-fields";
    for (const [name, caption] of [["split_delay_min", "最短延迟（秒）"], ["split_delay_max", "最长延迟（秒）"]]) {
      const field = document.createElement("label");
      const input = document.createElement("input");
      Object.assign(input, {name, type: "number", min: "0", max: "60", step: "any", required: true});
      field.append(caption, input);
      delays.append(field);
    }
    section.querySelector("form").append(label, reset, delays, save, status);
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
    const {profile} = data;
    letAvatar = data.agent_avatar;
    for (const [name, value] of Object.entries(profile)) {
      if (form.elements[name]) form.elements[name].value = value;
    }
    if (general) {
      form.elements.split_delay_min.value = data.split_delay_min ?? 1;
      form.elements.split_delay_max.value = data.split_delay_max ?? 1.5;
      form.elements.split_reply.checked = data.split_reply !== false;
      form.elements.split_pattern.value = data.split_pattern || form.elements.split_pattern.placeholder;
    } else {
      section.querySelector("[data-persona-title]").textContent = data.agent_name + "的人格";
      form.elements.agent_name.value = data.agent_name;
      section.querySelector("[data-summary]").textContent = profile.persona || "你是久留美，使用中文交流。";
    }
    save.disabled = false;
  }
  form.onsubmit = async event => {
    event.preventDefault();
    save.disabled = true;
    try {
      const payload = Object.fromEntries(new FormData(form));
      if (general) {
        const pattern = form.elements.split_pattern.value.trim();
        new RegExp(pattern || form.elements.split_pattern.placeholder);
        const minimum = form.elements.split_delay_min.valueAsNumber;
        const maximum = form.elements.split_delay_max.valueAsNumber;
        if (minimum > maximum) throw Error("最短延迟不能超过最长延迟");
        await request("reply-settings", {split_reply: form.elements.split_reply.checked, split_pattern: pattern,
          split_delay_min: minimum, split_delay_max: maximum});
        window.dispatchEvent(new Event("kurumi-identity"));
        status.textContent = "已保存";
        return;
      }
      {
        const agentAvatar = payload.reset_avatar ? "" :
          await encodeAgentAvatar(form.elements.agent_picture.files[0]) || letAvatar;
        await request("agent-identity", {agent_name: payload.agent_name, agent_avatar: agentAvatar});
        form.elements.agent_picture.value = "";
        form.elements.reset_avatar.checked = false;
        window.dispatchEvent(new Event("kurumi-identity"));
      }
      await request("profile", {persona: payload.persona});
      await refresh();
      status.textContent = "已保存";
    } catch (error) { status.textContent = error.message; }
    finally { save.disabled = false; }
  };
  const button = document.createElement("button");
  button.type = "button";
  button.textContent = general ? "对话格式" : "人格设定";
  button.onclick = () => refresh().catch(error => { status.textContent = error.message; });
  return button;
}

export function mountModels(usagePanel, providerPanel) {
  const section = document.createElement("section");
  section.className = "profile-settings api-settings model-providers";
  section.innerHTML = `
    <header><h3>已配置的模型</h3><button class="primary" type="button" data-new disabled>＋ 新增模型</button></header>
    <div data-list></div>
    <p role="status"></p>
    <dialog class="model-provider-dialog" aria-label="模型连接">
      <form data-editor>
        <h3 data-editor-title></h3>
        <label>模型类型<select name="kind"><option value="chat">对话模型</option><option value="speech">语音合成（OpenAI 兼容）</option></select></label>
        <label>供应商预设<select name="preset"></select></label>
        <label>显示名称<input name="label" maxlength="80"></label>
        <label>API Base URL<input name="url" type="url"></label>
        <label>模型 ID<input name="model" required maxlength="200"></label>
        <label>API Key<input name="key" type="password" autocomplete="new-password"></label>
        <p data-editor-status role="status"></p>
        <div class="model-provider-actions">
          <button type="button" data-clear>清除 Key</button>
          <button type="button" data-delete>删除模型</button>
          <button type="button" data-cancel>取消</button>
          <button class="primary">保存连接</button>
        </div>
      </form>
    </dialog>`;
  const usage = document.createElement("section");
  usage.className = "profile-settings api-settings model-usage";
  usage.innerHTML = `
      <form data-roles>
        <h3>模型用途与失败重试</h3>
        <label>对话模型<select name="chat"></select></label>
        <label>回退对话模型<select name="fallback"></select></label>
        <label>异常重试次数<input type="number" name="retries" min="0" max="5" required></label>
        <h3>上下文管理</h3>
        <label>保留对话轮数（−1 不限）<input type="number" name="rounds" min="-1" max="100" required></label>
        <label>超限时丢弃轮数<input type="number" name="discard" min="1" max="100" required></label>
        <label>超限处理<select name="strategy"><option value="trim">截断旧对话</option><option value="summary">由模型压缩</option></select></label>
        <label>上下文压缩模型<select name="summary"></select></label>
        <label>压缩保留最近轮次比例<input type="number" name="keep" min="0" max=".3" step=".01" required></label>
        <label>压缩提示词<textarea name="prompt" maxlength="4000" rows="3"></textarea></label>
        <h3>语音回复</h3>
        <label>启用语音回复<input type="checkbox" name="voice_enabled"></label>
        <label>语音模型<select name="speech"></select></label>
        <label>语气分析模型<select name="emotion"></select></label>
        <label>语音触发概率<input type="number" name="voice_probability" min="0" max="1" step=".01" required></label>
        <label>音色 ID<input name="voice" maxlength="80"></label>
        <button class="primary">保存模型用途</button>
      </form>
      <p role="status"></p>`;
  providerPanel.append(section);
  usagePanel.append(usage);
  const editor = section.querySelector("[data-editor]");
  const roles = usage.querySelector("[data-roles]");
  const dialog = section.querySelector("dialog");
  const editorStatus = editor.querySelector("[role=status]");
  const add = section.querySelector("[data-new]");
  const usageStatus = usage.querySelector("[role=status]");
  const status = section.querySelector("[role=status]");
  const clear = section.querySelector("[data-clear]");
  const remove = section.querySelector("[data-delete]");
  let data, id = "";
  function edit(entry) {
    editor.reset();
    editor.elements.kind.value = entry?.kind || "chat";
    id = entry?.id || "";
    for (const field of ["label", "url", "model"]) editor.elements[field].value = entry?.[field] || "";
    editor.elements.key.placeholder = entry?.has_key ? "********" : "";
    editor.querySelector("[data-editor-title]").textContent = entry ? "编辑模型" : "新增模型";
    editorStatus.textContent = "";
    clear.hidden = !entry?.has_key;
    remove.hidden = !entry;
    dialog.showModal();
  }
  function render() {
    const list = section.querySelector("[data-list]");
    list.replaceChildren(...data.models.map(entry => {
      const button = document.createElement("button");
      button.type = "button";
      const label = document.createElement("strong");
      label.textContent = entry.label;
      const detail = document.createElement("small");
      detail.textContent = (entry.kind === "speech" ? "语音合成" : "对话模型") + " · " + entry.model;
      const action = document.createElement("span");
      action.textContent = "编辑";
      button.append(label, detail, action);
      button.onclick = () => edit(entry);
      return button;
    }));
    if (!data.models.length) {
      const empty = document.createElement("p");
      empty.textContent = "还没有模型。添加后即可在「模型与 API」中选用。";
      list.append(empty);
    }
    editor.elements.preset.replaceChildren(new Option("自定义兼容接口", ""), ...data.presets.map(
      (item, index) => new Option(item.name, String(index))));
    add.disabled = false;
    for (const name of ["chat", "fallback", "summary", "speech", "emotion"]) {
      const select = roles.elements[name];
      select.replaceChildren(new Option(["chat", "summary"].includes(name) ? "使用第一个模型" : "不使用", ""),
        ...data.models.filter(entry => entry.kind === (name === "speech" ? "speech" : "chat"))
          .map(entry => new Option(entry.label, entry.id)));
      select.value = data.roles[name] || "";
    }
    const defaults = {retries: 0, rounds: 10, discard: 1, keep: .15, strategy: "trim", prompt: "", voice_probability: .2, voice: "alloy"};
    for (const [name, value] of Object.entries({...defaults, ...data.policy})) {
      if (name !== "voice_enabled") roles.elements[name].value = value;
    }
    roles.elements.voice_enabled.checked = data.policy.voice_enabled === true;
  }
  async function request(body) {
    const response = await fetch("/api/models", body ? {
      method: "POST", headers: {"Content-Type":"application/json"}, body: JSON.stringify(body),
    } : {});
    const result = await response.json();
    if (!response.ok) throw Error(result.error || "模型配置请求失败");
    data = result;
    render();
  }
  async function save(body, feedback, controls) {
    controls.inert = true;
    feedback.textContent = "";
    try {
      await request(body);
      if (feedback === editorStatus) {
        dialog.close();
        status.textContent = body.action === "delete" ? "已删除模型" :
          body.action === "clear" ? "已清除 API Key" : "已保存模型";
        add.focus();
      } else feedback.textContent = "已保存";
    } catch (error) { feedback.textContent = error.message; }
    finally { controls.inert = false; }
  }
  editor.elements.preset.onchange = () => {
    const value = editor.elements.preset.value;
    if (!value) return;
    const preset = data.presets[Number(value)];
    editor.elements.label.value = preset.name;
    editor.elements.url.value = preset.url;
    editor.elements.model.value = preset.model;
  };
  add.onclick = () => edit();
  editor.querySelector("[data-cancel]").onclick = () => dialog.close();
  dialog.addEventListener("cancel", event => { if (editor.inert) event.preventDefault(); });
  dialog.addEventListener("close", () => { editor.elements.key.value = ""; });
  clear.onclick = () => { if (window.confirm("清除该模型的 API Key？")) save({action:"clear", id}, editorStatus, editor); };
  remove.onclick = () => { if (window.confirm("删除该模型？")) save({action:"delete", id}, editorStatus, editor); };
  editor.onsubmit = event => {
    event.preventDefault();
    save({...Object.fromEntries(new FormData(editor)), action:"save", id}, editorStatus, editor);
  };
  roles.onsubmit = event => {
    event.preventDefault();
    save({...Object.fromEntries(new FormData(roles)), action:"roles", voice_enabled: roles.elements.voice_enabled.checked}, usageStatus, roles);
  };
  function tab(label, feedback) {
    const button = document.createElement("button");
    button.type = "button";
    button.textContent = label;
    button.onclick = () => {
      if (!data) request().catch(error => { feedback.textContent = error.message; });
    };
    return button;
  }
  return {usageTab: tab("模型与 API", usageStatus), providerTab: tab("模型供应商", status)};
}


function advancedSections(section, pages) {
  const nav = document.createElement("nav");
  nav.className = "advanced-subnav";
  nav.setAttribute("aria-label", "模块页面");
  section.prepend(nav);
  const buttons = pages.map(([label]) => {
    const button = document.createElement("button");
    button.type = "button";
    button.textContent = label;
    nav.append(button);
    return button;
  });
  function select(index) {
    pages.forEach(([, panel], i) => {
      panel.hidden = i !== index;
      buttons[i].setAttribute("aria-pressed", String(i === index));
    });
  }
  buttons.forEach((button, index) => { button.onclick = () => select(index); });
  select(0);
  return select;
}

export function mountStickers(container) {
  const section = document.createElement("section");
  section.className = "profile-settings sticker-manager";
  section.innerHTML = `
    <div data-library><header><h3>表情库</h3><div class="model-provider-actions"><button type="button" data-new-category>新建分类</button><button type="button" class="primary" data-add>上传表情包</button></div></header>

    <nav class="sticker-filters" aria-label="表情分类"></nav>
    <div class="sticker-category" hidden><p></p><button type="button" data-edit-category>编辑分类</button><button type="button" data-delete-category>删除分类</button></div>
    <div class="sticker-grid"></div></div>
    <div data-sticker-settings><header><h3>表情发送设置</h3></header>
    <form class="sticker-probability">
      <label>发送表情包概率（%）<input name="probability" type="number" min="0" max="100" step="any" required></label>
      <button class="primary">保存概率</button><small>0% 关闭。命中概率后，由智能体判断是否适合发表情。</small>
    </form></div>
    <p class="sticker-status" role="status"></p>
    <dialog class="model-provider-dialog" aria-label="表情包">
      <form>
        <h3 data-title></h3>
        <label>名称<input name="label" maxlength="40" required></label>
        <label>分类<select name="category"></select></label>
        <label>图片<input type="file" name="picture" accept="image/png,image/jpeg,image/webp"></label>
        <p role="status"></p>
        <div class="model-provider-actions"><button type="button" data-cancel>取消</button><button class="primary">保存</button></div>
      </form>
    </dialog>
    <dialog data-category-dialog class="model-provider-dialog" aria-label="编辑表情分类">
      <form>
        <h3 data-title></h3>
        <label>分类名称<input name="label" maxlength="40" required placeholder="例如：得意"></label>
        <label>使用说明<textarea name="description" maxlength="160" rows="3" placeholder="例如：猜对走势或被夸奖时"></textarea></label>
        <p role="status"></p>
        <div class="model-provider-actions"><button type="button" data-cancel>取消</button><button class="primary">保存分类</button></div>
      </form>
    </dialog>`;
  container.append(section);
  advancedSections(section, [
    ["表情库", section.querySelector("[data-library]")],
    ["设置", section.querySelector("[data-sticker-settings]")],
  ]);
  const probability = section.querySelector(".sticker-probability");
  const grid = section.querySelector(".sticker-grid");
  const status = section.querySelector(".sticker-status");
  const dialog = section.querySelector("dialog");
  const form = dialog.querySelector("form");
  const feedback = form.querySelector("[role=status]");
  const filters = section.querySelector(".sticker-filters");
  const categoryDetails = section.querySelector(".sticker-category");
  const categoryDialog = section.querySelector("[data-category-dialog]");
  const categoryForm = categoryDialog.querySelector("form");
  const categoryFeedback = categoryForm.querySelector("[role=status]");
  let editing, editingCategory, filter = "all";
  let categories = [], stickers = [];

  async function request(body) {
    const response = await fetch("/api/stickers", body ? {
      method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify(body),
    } : {});
    const result = await response.json();
    if (!response.ok) throw Error(result.error || "表情包请求失败");
    if (!body || body.action === "settings") probability.elements.probability.value = result.probability;
    categories = result.categories;
    stickers = result.stickers;
    if (filter && filter !== "all" && !categories.some(item => item.id === filter)) filter = "all";
    render();
  }

  function open(entry) {
    editing = entry;
    form.reset();
    feedback.textContent = "";
    form.elements.label.value = entry?.label || "";
    form.elements.category.replaceChildren(new Option("未分类", ""),
      ...categories.filter(item => !item.builtin).map(item => new Option(item.label, item.id)));
    form.elements.category.value = entry ? entry.category || "" : categories.some(item => !item.builtin && item.id === filter) ? filter : "";
    form.elements.picture.required = !entry;
    form.elements.picture.closest("label").hidden = Boolean(entry);
    form.querySelector("[data-title]").textContent = entry ? "编辑表情包" : "上传表情包";
    dialog.showModal();
  }

  function openCategory(entry) {
    editingCategory = entry;
    categoryForm.reset();
    categoryFeedback.textContent = "";
    categoryForm.querySelector("[data-title]").textContent = entry ? "编辑分类" : "新建分类";
    categoryForm.elements.label.value = entry?.label || "";
    categoryForm.elements.description.value = entry?.description || "";
    categoryDialog.showModal();
  }

  function render() {
    const selected = categories.find(item => item.id === filter);
    const entries = filter === "all" ? stickers : stickers.filter(entry => (entry.category || "") === filter);
    filters.replaceChildren();
    for (const item of [
      {id: "all", label: "全部", count: stickers.length},
      ...categories,
      {id: "", label: "未分类", count: stickers.filter(entry => !entry.category).length},
    ]) {
      const button = document.createElement("button");
      button.type = "button";
      button.textContent = item.label + " (" + item.count + ")";
      button.setAttribute("aria-pressed", String(item.id === filter));
      button.onclick = () => {
        filter = item.id;
        render();
        filters.querySelector('[aria-pressed="true"]').focus();
      };
      filters.append(button);
    }
    categoryDetails.hidden = !selected;
    categoryDetails.querySelector("p").textContent = selected?.description || "";
    for (const button of categoryDetails.querySelectorAll("button")) button.hidden = Boolean(selected?.builtin);
    categoryDetails.querySelector("[data-edit-category]").onclick = () => openCategory(selected);
    grid.replaceChildren();
    if (!entries.length) {
      const empty = document.createElement("p");
      empty.textContent = filter === "all" ? "还没有表情包。上传后，智能体可以在对话里发送。" : "这个分类里还没有表情。";
      grid.append(empty);
    }
    for (const entry of entries) {
      const card = document.createElement("article");
      const image = new Image();
      image.src = entry.src;
      image.alt = entry.label;
      const caption = document.createElement("strong");
      caption.textContent = entry.label;
      if (entry.builtin) {
        const badge = document.createElement("small");
        badge.textContent = "系统默认";
        card.append(image, caption, badge);
        grid.append(card);
        continue;
      }
      const actions = document.createElement("div");
      const rename = document.createElement("button");
      rename.textContent = "编辑";
      rename.onclick = () => open(entry);
      const remove = document.createElement("button");
      remove.textContent = "删除";
      remove.onclick = async () => {
        if (!window.confirm("删除「" + entry.label + "」？")) return;
        remove.disabled = true;
        try { await request({action: "delete", id: entry.id}); status.textContent = "已删除"; }
        catch (error) { status.textContent = error.message; }
        finally { remove.disabled = false; }
      };
      actions.append(rename, remove);
      card.append(image, caption, actions);
      grid.append(card);
    }
  }

  probability.onsubmit = async event => {
    event.preventDefault();
    probability.inert = true;
    try {
      await request({action: "settings", probability: probability.elements.probability.valueAsNumber});
      status.textContent = "已保存发送概率";
    } catch (error) { status.textContent = error.message; }
    finally { probability.inert = false; }
  };

  section.querySelector("[data-new-category]").onclick = () => openCategory();
  categoryForm.querySelector("[data-cancel]").onclick = () => categoryDialog.close();
  categoryDialog.addEventListener("cancel", event => { if (categoryForm.inert) event.preventDefault(); });
  categoryForm.onsubmit = async event => {
    event.preventDefault();
    const fields = Object.fromEntries(new FormData(categoryForm));
    categoryForm.inert = true;
    categoryFeedback.textContent = "";
    try {
      await request({action: "category-save", category: editingCategory?.id, ...fields});
      filter = categories.find(item => item.label === fields.label.trim()).id;
      render();
      categoryDialog.close();
      status.textContent = "已保存分类";
    } catch (error) { categoryFeedback.textContent = error.message; }
    finally { categoryForm.inert = false; }
  };
  section.querySelector("[data-delete-category]").onclick = async event => {
    if (!window.confirm("删除此分类？里面的表情会移到「未分类」。")) return;
    const button = event.currentTarget;
    button.disabled = true;
    try {
      await request({action: "category-delete", category: filter});
      filter = "";
      render();
      status.textContent = "已删除分类";
    } catch (error) { status.textContent = error.message; }
    finally { button.disabled = false; }
  };

  section.querySelector("[data-add]").onclick = () => open();
  form.querySelector("[data-cancel]").onclick = () => dialog.close();
  dialog.addEventListener("cancel", event => { if (form.inert) event.preventDefault(); });
  form.onsubmit = async event => {
    event.preventDefault();
    form.inert = true;
    feedback.textContent = "";
    try {
      const label = form.elements.label.value;
      const category = form.elements.category.value;
      const body = editing ? {action: "rename", id: editing.id, label, category}
        : {action: "add", label, category, src: await encodeSticker(form.elements.picture.files[0])};
      await request(body);
      dialog.close();
      status.textContent = "已保存";
    } catch (error) {
      feedback.textContent = error.message;
    } finally { form.inert = false; }
  };

  const tab = document.createElement("button");
  tab.type = "button";
  tab.textContent = "高级设置";
  tab.onclick = () => request().catch(error => { status.textContent = error.message; });
  return tab;
}

async function encodeSticker(file) {
  if (!file || file.size > 5 * 1024 * 1024 || !["image/png", "image/jpeg", "image/webp"].includes(file.type))
    throw Error("请选择不超过 5 MB 的 PNG、JPEG 或 WebP。");
  const image = await createImageBitmap(file);
  try {
    const scale = Math.min(1, 256 / Math.max(image.width, image.height));
    const canvas = document.createElement("canvas");
    canvas.width = Math.max(1, Math.round(image.width * scale));
    canvas.height = Math.max(1, Math.round(image.height * scale));
    canvas.getContext("2d").drawImage(image, 0, 0, canvas.width, canvas.height);
    for (const quality of [.7, .5, .3]) {
      const src = canvas.toDataURL("image/webp", quality);
      if (src.startsWith("data:image/webp;base64,") && src.length <= 48000) return src;
    }
    throw Error("图片压缩后仍过大，请换一张较小的图片。");
  } finally { image.close(); }
}


function mountMemory(container) {
  const section = document.createElement("section");
  section.className = "profile-settings sticker-manager";
  section.innerHTML = `
    <div data-overview>
      <header><h3 data-agent>长期记忆</h3></header>
      <div class="memory-overview">
        <dl class="memory-stats">
          <div><dt>记忆总数</dt><dd data-total>—</dd></div>
          <div><dt>可检索</dt><dd data-active>—</dd></div>
          <div><dt>已屏蔽</dt><dd data-expired>—</dd></div>
        </dl>
        <figure class="memory-curve"><figcaption>记忆强度随时间下降</figcaption>
          <svg viewBox="0 0 240 60" role="img" aria-label="记忆强度线性下降曲线">
            <path d="M8 6V52H232" fill="none" stroke="#dbc9da"/>
            <path d="M8 6L232 52" fill="none" stroke="#984674" stroke-width="3"/>
          </svg>
          <div><span>当天 · 100%</span><span data-expiry></span></div>
        </figure>
      </div>
    </div>
    <div data-browse>
      <header><h3>记忆浏览</h3><button type="button" class="primary" data-new>添加记忆</button></header>
      <div class="memory-filters">
        <label>记忆对象<select data-subject></select></label>
        <label>关键词<input type="search" data-search placeholder="搜索记忆内容"></label>
      </div>
      <div class="memory-list"></div>
    </div>
    <div data-profiles><header><h3>对象画像</h3></header><div class="memory-profiles"></div></div>
    <div data-memory-options><header><h3>记忆设置</h3></header>
    <form data-memory-settings>
      <label class="check"><input type="checkbox" name="enabled"> 在私聊中使用长期记忆</label>
      <label class="check"><input type="checkbox" name="automatic"> 回复后自动整理</label>
      <label>整理模型<select name="model"></select></label>
      <small>开启自动整理后，本轮文字会发送给选定的模型，用来提取长期偏好和约定。</small>
      <button class="primary">保存记忆设置</button>
    </form>
    </div>
    <p class="sticker-status" role="status"></p>
    <dialog class="model-provider-dialog" aria-label="编辑长期记忆"><form>
      <h3 data-title></h3>
      <label>记忆对象<select name="subject"></select></label>
      <label>记忆内容<textarea name="text" rows="4" maxlength="300" required></textarea></label>
      <p role="status"></p>
      <div class="model-provider-actions"><button type="button" data-cancel>取消</button><button class="primary">保存</button></div>
    </form></dialog>`;
  container.append(section);
  const settings = section.querySelector("[data-memory-settings]");
  const list = section.querySelector(".memory-list");
  const status = section.querySelector(".sticker-status");
  const dialog = section.querySelector("dialog");
  const editor = dialog.querySelector("form");
  const feedback = editor.querySelector("[role=status]");
  const subject = section.querySelector("[data-subject]");
  const add = section.querySelector("[data-new]");
  add.disabled = true;
  const search = section.querySelector("[data-search]");
  const profiles = section.querySelector(".memory-profiles");
  const show = advancedSections(section, [
    ["总览", section.querySelector("[data-overview]")],
    ["记忆浏览", section.querySelector("[data-browse]")],
    ["对象画像", section.querySelector("[data-profiles]")],
    ["设置", section.querySelector("[data-memory-options]")],
  ]);
  let editing, entries = [], subjects = {};
  subject.onchange = renderEntries;
  search.oninput = renderEntries;

  async function fetchMemory(body) {
    const response = await fetch("/api/chat-memory", body ? {
      method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify(body),
    } : {});
    const result = await response.json();
    if (!response.ok) throw Error(result.error || "无法更新记忆");
    return result;
  }

  function renderData(data) {
    const selected = subject.value || "all";
    subjects = {...data.subjects};
    entries = data.entries;
    for (const entry of entries) subjects[entry.subject] ??= "已移除的对象";
    subject.replaceChildren(new Option("全部对象", "all"),
      ...Object.entries(subjects).map(([id, label]) => new Option(id === "self" ? label + "（我）" : label, id)));
    subject.value = selected === "all" || Object.hasOwn(subjects, selected) ? selected : "self";
    section.querySelector("[data-agent]").textContent = data.agent + " 的长期记忆";
    section.querySelector("[data-expiry]").textContent = data.days + " 天 · 0%（屏蔽）";
    const active = entries.filter(entry => entry.strength > 0).length;
    section.querySelector("[data-total]").textContent = entries.length;
    section.querySelector("[data-active]").textContent = active;
    section.querySelector("[data-expired]").textContent = entries.length - active;
    add.disabled = false;
    renderEntries();
    renderProfiles();
  }

  function renderSettings({config, models}) {
    settings.elements.enabled.checked = Boolean(config.enabled);
    settings.elements.automatic.checked = Boolean(config.automatic);
    settings.elements.model.replaceChildren(new Option("使用当前对话模型", ""),
      ...models.map(item => new Option(item.label, item.id)));
    settings.elements.model.value = models.some(item => item.id === config.model) ? config.model : "";
  }

  function open(entry) {
    editing = entry;
    editor.querySelector("[data-title]").textContent = entry ? "编辑记忆" : "添加记忆";
    editor.elements.subject.replaceChildren(...Object.entries(subjects)
      .map(([id, label]) => new Option(label, id)));
    editor.elements.subject.value = entry?.subject || (subject.value === "all" ? "self" : subject.value);
    editor.elements.text.value = entry?.text || "";
    feedback.textContent = "";
    dialog.showModal();
  }

  function renderProfiles() {
    profiles.replaceChildren();
    for (const [id, label] of Object.entries(subjects)) {
      const matching = entries.filter(entry => entry.subject === id);
      if (!matching.length && id !== "self") continue;
      const card = document.createElement("button");
      card.type = "button";
      const initial = document.createElement("span");
      initial.className = "memory-initial";
      initial.textContent = Array.from(label)[0] || "？";
      initial.setAttribute("aria-hidden", "true");
      const name = document.createElement("strong");
      name.textContent = id === "self" ? label + "（我）" : label;
      const count = document.createElement("small");
      count.textContent = matching.length + " 条记忆 · " + matching.filter(entry => entry.strength > 0).length + " 条可检索";
      card.append(initial, name, count);
      card.onclick = () => {
        subject.value = id;
        search.value = "";
        renderEntries();
        show(1);
        subject.focus();
      };
      profiles.append(card);
    }
  }

  function renderEntries() {
    list.replaceChildren();
    const keyword = search.value.trim().toLocaleLowerCase();
    const visible = entries.filter(entry => (subject.value === "all" || entry.subject === subject.value)
      && entry.text.toLocaleLowerCase().includes(keyword));
    if (!visible.length) {
      const empty = document.createElement("p");
      empty.className = "memory-empty";
      empty.textContent = entries.length ? "没有符合筛选条件的记忆。" : "还没有长期记忆，可以先添加一条。";
      list.append(empty);
      return;
    }
    const table = document.createElement("table");
    table.createTHead().insertRow().append(...["对象", "记忆内容", "强度", "更新时间", "操作"].map(label => {
      const cell = document.createElement("th");
      cell.scope = "col";
      cell.textContent = label;
      return cell;
    }));
    const body = table.createTBody();
    const dateFormat = new Intl.DateTimeFormat("zh-CN", {
      dateStyle: "short", timeStyle: "short", timeZone: "Asia/Shanghai",
    });
    for (const entry of visible) {
      const row = body.insertRow();
      const badge = document.createElement("span");
      badge.className = "memory-subject-badge";
      badge.textContent = subjects[entry.subject];
      row.insertCell().append(badge);
      row.insertCell().textContent = entry.text;
      const strength = row.insertCell();
      const percent = Math.ceil(entry.strength * 100);
      strength.textContent = percent ? percent + "%" : "0% · 已屏蔽";
      const meter = document.createElement("progress");
      meter.max = 1;
      meter.value = entry.strength;
      meter.setAttribute("aria-label", "记忆强度");
      strength.append(meter);
      row.insertCell().textContent = dateFormat.format(entry.updated * 1000);
      const actions = row.insertCell();
      const edit = document.createElement("button");
      edit.textContent = "查看 / 编辑";
      edit.onclick = () => open(entry);
      const remove = document.createElement("button");
      remove.textContent = "删除";
      remove.onclick = () => {
        if (window.confirm("删除这条记忆？")) save({action: "delete", id: entry.id}, row, status);
      };
      actions.append(edit, remove);
    }
    list.append(table);
  }

  async function refresh() {
    const data = await fetchMemory();
    renderSettings(data);
    renderData(data);
  }

  async function save(body, controls, target) {
    controls.inert = true;
    target.textContent = "";
    try {
      const data = await fetchMemory(body);
      if (body.action === "settings") renderSettings(data);
      renderData(data);
      if (controls === editor) dialog.close();
      status.textContent = body.action === "delete" ? "已删除记忆" : "已保存";
    } catch (error) { target.textContent = error.message; }
    finally { controls.inert = false; }
  }

  settings.onsubmit = event => {
    event.preventDefault();
    save({action: "settings", enabled: settings.elements.enabled.checked,
      automatic: settings.elements.automatic.checked, model: settings.elements.model.value}, settings, status);
  };
  editor.onsubmit = event => {
    event.preventDefault();
    save({action: editing ? "edit" : "add", id: editing?.id, text: editor.elements.text.value, subject: editor.elements.subject.value}, editor, feedback);
  };
  section.querySelector("[data-new]").onclick = () => open();
  editor.querySelector("[data-cancel]").onclick = () => dialog.close();
  dialog.addEventListener("cancel", event => { if (editor.inert) event.preventDefault(); });
  return refresh;
}

export function mountAdvanced(container) {
  const navigation = document.createElement("nav");
  navigation.className = "workspace-switch advanced-settings-tabs";
  navigation.setAttribute("aria-label", "高级设置");
  const stickers = document.createElement("div");
  const memory = document.createElement("div");
  container.append(navigation, stickers, memory);
  const stickerTab = mountStickers(stickers);
  stickerTab.textContent = "表情包管理器";
  const memoryTab = document.createElement("button");
  memoryTab.type = "button";
  memoryTab.textContent = "记忆模组";
  const loadStickers = stickerTab.onclick;
  const loadMemory = mountMemory(memory);
  let showMemory = false;
  function select() {
    stickers.hidden = showMemory;
    memory.hidden = !showMemory;
    stickerTab.classList.toggle("active", !showMemory);
    memoryTab.classList.toggle("active", showMemory);
    stickerTab.setAttribute("aria-pressed", String(!showMemory));
    memoryTab.setAttribute("aria-pressed", String(showMemory));
    if (showMemory) loadMemory().catch(error => { memory.querySelector(".sticker-status").textContent = error.message; });
    else loadStickers();
  }
  stickerTab.onclick = () => { showMemory = false; select(); };
  memoryTab.onclick = () => { showMemory = true; select(); };
  navigation.append(stickerTab, memoryTab);
  const tab = document.createElement("button");
  tab.type = "button";
  tab.textContent = "高级设置";
  tab.onclick = select;
  return tab;
}


function accountSettings(container, title, endpoint, fields, defaults, convert) {
  const section = document.createElement("section");
  section.className = "profile-settings api-settings model-usage";
  section.innerHTML = `<form><h3>${title}</h3>${fields}<button class="primary" disabled>保存设置</button><p role="status"></p></form>`;
  container.append(section);
  const form = section.querySelector("form");
  const button = form.querySelector("button");
  const status = form.querySelector("[role=status]");
  async function request(body) {
    const res = await fetch("/api/" + endpoint, body ? {
      method: "POST", headers: {"Content-Type":"application/json"}, body: JSON.stringify(body),
    } : {cache:"no-store"});
    const data = await res.json();
    if (!res.ok) throw Error(data.error || "请求失败");
    form.reset();
    for (const [key, value] of Object.entries({...defaults, ...data})) {
      if (form.elements[key]) form.elements[key].value = value;
    }
    const avatar = form.querySelector("img");
    if (avatar) avatar.src = data.avatar.startsWith("data:") ? data.avatar : "/assets/" + data.avatar;
    button.disabled = false;
  }
  form.onsubmit = async event => {
    event.preventDefault();
    button.disabled = true;
    try {
      await request(await convert(form, Object.fromEntries(new FormData(form))));
      status.textContent = "已保存";
      if (endpoint === "user-settings") window.dispatchEvent(new Event("kurumi-identity"));
    } catch (error) { status.textContent = error.message; }
    finally { button.disabled = false; }
  };
  const tab = document.createElement("button");
  tab.type = "button";
  tab.textContent = title;
  tab.onclick = () => request().catch(error => { status.textContent = error.message; });
  return tab;
}

export function mountUserSettings(container) {
  return accountSettings(container, "个人资料与账号", "user-settings", `
    <img class="account-avatar" width="60" height="60" alt="当前头像">
    <label>昵称<input name="nickname" maxlength="40" required></label>
    <label>登录用户名<input name="username" autocomplete="username" required></label>
    <label>更换头像<input name="picture" type="file" accept="image/png,image/jpeg,image/webp"></label>
    <label>当前密码<input name="password" type="password" autocomplete="current-password"></label>
    <label>新密码<input name="new_password" type="password" autocomplete="new-password" minlength="12" maxlength="128"></label>
    <small>修改用户名或密码时需验证当前密码。</small>`, {}, async (form, values) => {
      delete values.picture;
      return {...values, avatar: await encodeAgentAvatar(form.elements.picture.files[0])};
    });
}

export function mountSocialSettings(container) {
  return accountSettings(container, "群聊与巡视", "social-settings", `
    <label>群聊主动发言概率（%）<input name="probability" type="number" min="0" max="100" step="any" required></label>
    <small>被 @ 时直接唤醒，由智能体决定是否回复。</small>
    <label>定时巡视<select name="patrol"><option value="off">关闭</option><option value="fixed">每天固定时间</option><option value="random">每天在时段内随机选时</option></select></label>
    <label>固定时间 / 时段开始（北京时间）<input name="start" type="time" required></label>
    <label>时段结束（北京时间）<input name="end" type="time" required></label>`,
    {probability:0, patrol:"off", start:"10:00", end:"22:00"},
    (form, values) => ({...values, probability: form.elements.probability.valueAsNumber}));
}
