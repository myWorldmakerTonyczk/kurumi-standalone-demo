import {prepareImage} from "/assets/community/media.js";

export function insertMention(field, name) {
  const start = field.selectionStart;
  const prefix = start && !/\s$/.test(field.value.slice(0, start)) ? " " : "";
  field.setRangeText(prefix + "@" + name + " ", start, field.selectionEnd, "end");
  field.focus();
}

export async function encodeAgentAvatar(file) {
  if (!file || !file.size) return "";
  if (file.size > 5 * 1024 * 1024 || !["image/png","image/jpeg","image/webp"].includes(file.type))
    throw Error("头像须为不超过 5 MB 的 PNG、JPEG 或 WebP。");
  const image = await createImageBitmap(file);
  try {
    const canvas = document.createElement("canvas");
    canvas.width = canvas.height = 96;
    const side = Math.min(image.width, image.height);
    canvas.getContext("2d").drawImage(image, (image.width-side)/2, (image.height-side)/2, side, side, 0, 0, 96, 96);
    return canvas.toDataURL("image/webp", .68);
  } finally { image.close(); }
}

function markdown(root, content) {
  const create = (tag, parent, value) => {
    const element = document.createElement(tag);
    if (value !== undefined) element.textContent = value;
    parent.append(element);
    return element;
  };
  function inline(parent, value) {
    const pattern = /`([^`]+)`|\*\*(.+?)\*\*|__(.+?)__|\*([^*]+)\*/g;
    let offset = 0;
    for (const match of value.matchAll(pattern)) {
      parent.append(value.slice(offset, match.index));
      create(match[1] ? "code" : match[4] ? "em" : "strong", parent,
        match[1] ?? match[2] ?? match[3] ?? match[4]);
      offset = match.index + match[0].length;
    }
    parent.append(value.slice(offset));
  }
  const lines = content.split(/\r?\n/);
  const cells = line => line.trim().replace(/^\||\|$/g, "").split("|");
  const listPattern = /^\s*(-|\+|\*|\d+[.)])\s+(.+)/;
  for (let i = 0; i < lines.length; i++) {
    const line = lines[i];
    if (!line.trim()) continue;
    if (line.trimStart().startsWith("```")) {
      const code = [];
      while (++i < lines.length && !lines[i].trimStart().startsWith("```")) code.push(lines[i]);
      create("code", create("pre", root), code.join("\n"));
    } else if (line.includes("|") && /^\s*\|?\s*:?-{3,}/.test(lines[i + 1] || "")
      && cells(lines[i + 1]).every(cell => /^\s*:?-{3,}:?\s*$/.test(cell))) {
      const wrapper = create("div", root);
      wrapper.className = "chat-table";
      const table = create("table", wrapper);
      const header = create("tr", create("thead", table));
      cells(line).forEach(value => inline(create("th", header), value.trim()));
      const body = create("tbody", table);
      i++;
      while (lines[i + 1]?.includes("|")) {
        const row = create("tr", body);
        cells(lines[++i]).forEach(value => inline(create("td", row), value.trim()));
      }
    } else if (listPattern.test(line)) {
      const ordered = /^\s*\d/.test(line);
      const list = create(ordered ? "ol" : "ul", root);
      do {
        inline(create("li", list), lines[i].match(listPattern)[2]);
        if (!listPattern.test(lines[i + 1] || "") || /^\s*\d/.test(lines[i + 1]) !== ordered) break;
        i++;
      } while (i < lines.length);
    } else {
      const heading = line.match(/^(#{1,6})\s+(.+)/);
      inline(create(heading ? "h" + Math.min(6, heading[1].length + 2) : "p", root), heading?.[2] ?? line);
    }
  }
}

export function mountChat(gameId, {onExpand, onCollapse}) {
  const root = document.createElement("div");
  root.className = "kurumi-chat";
  root.innerHTML = `
    <button type="button" class="chat-launch" aria-expanded="false"><img src="/assets/avatar.png" alt="">和久留美聊天</button>
    <section class="chat-window" aria-label="和久留美聊天" hidden>
      <div class="chat-bar"><span class="chat-grip"><img src="/assets/avatar.png" alt="">久留美</span>
        <button type="button" data-expand>全屏</button>
        <button type="button" data-dock aria-pressed="false">固定</button>
        <button type="button" data-close aria-label="收起聊天">×</button>
      </div>
      <div class="chat-log" role="log"></div>
      <form>
        <div class="chat-upload-preview" hidden><img alt="待发送图片"><button type="button" data-remove-image>移除</button></div>
        <textarea aria-label="消息" name="message" rows="2" maxlength="2000" placeholder="输入消息或拖入图片"></textarea>
        <div class="chat-composer-actions"><input type="file" accept="image/*" name="picture" hidden>
          <button type="button" data-pick-image>图片</button><button type="submit" class="primary">发送</button></div>
      </form>
      <p role="status"></p>
    </section>`;
  document.body.append(root);
  const find = selector => root.querySelector(selector);
  const windowBox = find(".chat-window"), launch = find(".chat-launch");
  const log = find(".chat-log"), form = find("form"), status = find("[role=status]");
  const dock = find("[data-dock]");
  const expand = find("[data-expand]");
  let identity = {nickname:"你", agent_name:"久留美"};
  const avatar = (value, fallback) => value?.startsWith("data:image/webp;base64,") ? value : "/assets/" + (value || fallback);
  function updateIdentity(data) {
    identity = data;
    window.dispatchEvent(new CustomEvent("kurumi-chat-name", {detail: data.agent_name}));
    launch.lastChild.textContent = "和" + data.agent_name + "对话";
    find(".chat-grip").lastChild.textContent = data.agent_name;
    windowBox.setAttribute("aria-label", data.agent_name + "对话");
    form.elements.message.placeholder = "给" + data.agent_name + "发消息";
    for (const img of [launch.firstElementChild, find(".chat-grip img")]) img.src = avatar(data.agent_avatar, "avatar.png");
    for (const row of log.querySelectorAll(".chat-message:not(.chat-typing)")) decorate(row, row.classList.contains("chat-user"));
  }
  function decorate(row, user) {
    row.querySelector(".chat-speaker")?.remove();
    const speaker = document.createElement("div");
    speaker.className = "chat-speaker";
    const img = document.createElement("img");
    img.alt = "";
    img.src = user ? avatar(identity.avatar, "user-avatar.svg") : avatar(identity.agent_avatar, "avatar.png");
    const name = user ? identity.nickname : identity.agent_name;
    const mention = document.createElement("button");
    mention.type = "button";
    mention.className = "chat-mention";
    mention.textContent = user || !identity.nickname ? name : name + "（" + identity.nickname + "）";
    mention.title = "提及 " + name;
    mention.onclick = () => insertMention(form.elements.message, name);
    speaker.append(img, mention);
    row.prepend(speaker);
  }
  let floatingPosition, drag, loadedGame, busy = false;
  let lastProactiveId = 0, polling = false;

  async function request(route, gid, body, query = {}) {
    const response = await fetch("/api/" + route + "?" + new URLSearchParams({game: gid, ...query}), body ? {
      method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify(body),
    } : {});
    const result = await response.json();
    if (!response.ok) throw Error(result.error || "请求失败");
    return result;
  }
  function add(role, content, attachments = [], id) {
    const row = document.createElement("div");
    row.className = role === "user" ? "chat-user" : "chat-assistant";
    row.classList.add("chat-message");
    if (id) {
      row.id = "private-message-" + id;
      row.tabIndex = -1;
    }
    if (role === "user") row.textContent = content;
    else if (content) markdown(row, content);
    const images = [];
    for (const attachment of attachments) {
      if (attachment.kind === "image" && /^data:image\/(png|webp);base64,/.test(attachment.src)) {
        const image = document.createElement("img");
        image.className = "chat-attachment";
        image.src = attachment.src;
        image.alt = attachment.label;
        images.push(image);
      } else if (attachment.kind === "sticker" && attachment.src.startsWith("data:image/webp;base64,")) {
        const image = document.createElement("img");
        image.className = "chat-sticker";
        image.src = attachment.src;
        image.alt = attachment.label;
        row.append(image);
      } else if (attachment.kind === "audio" && attachment.src.startsWith("data:audio/mpeg;base64,")) {
        const audio = document.createElement("audio");
        audio.src = attachment.src;
        audio.controls = true;
        audio.preload = "none";
        row.append(audio);
      } else if (attachment.kind === "panel") {
        const link = document.createElement("a");
        link.href = "/workspace/";
        link.textContent = attachment.label;
        row.append(link);
      }
    }
    const user = role === "user";
    const hasBubble = Boolean(content || row.childNodes.length);
    if (hasBubble) decorate(row, user);
    if (hasBubble || !attachments.length) log.append(row);
    for (const [index, image] of images.entries()) {
      const media = document.createElement("div");
      media.className = user ? "chat-image-message from-user" : "chat-image-message";
      if (!hasBubble && index === 0) {
        decorate(media, user);
        if (id) { media.id = row.id; media.tabIndex = -1; }
      }
      media.append(image);
      log.append(media);
    }
    log.scrollTop = log.scrollHeight;
    return row;
  }
  function position(left, top) {
    floatingPosition = {
      left: Math.max(8, Math.min(left, innerWidth - windowBox.offsetWidth - 8)),
      top: Math.max(8, Math.min(top, innerHeight - windowBox.offsetHeight - 8)),
    };
    Object.assign(windowBox.style, {left: floatingPosition.left + "px", top: floatingPosition.top + "px", right: "auto", bottom: "auto"});
  }
  const grip = find(".chat-grip");
  grip.onpointerdown = event => {
    if (event.button !== 0 || (root.classList.contains("docked") || root.classList.contains("full-chat"))) return;
    const rect = windowBox.getBoundingClientRect();
    drag = {x: event.clientX - rect.left, y: event.clientY - rect.top};
    grip.setPointerCapture(event.pointerId);
  };
  grip.onpointermove = event => { if (drag) position(event.clientX - drag.x, event.clientY - drag.y); };
  grip.onlostpointercapture = () => { drag = null; };
  window.addEventListener("resize", () => {
    if (floatingPosition && !windowBox.hidden && !root.classList.contains("docked"))
      position(floatingPosition.left, floatingPosition.top);
  });
  dock.onclick = () => {
    const fixed = root.classList.toggle("docked");
    dock.textContent = fixed ? "解除固定" : "固定";
    dock.setAttribute("aria-pressed", String(fixed));
    windowBox.removeAttribute("style");
    if (!fixed && floatingPosition) position(floatingPosition.left, floatingPosition.top);
  };
  find("[data-close]").onclick = () => {
    windowBox.hidden = true;
    launch.hidden = false;
    launch.setAttribute("aria-expanded", "false");
    launch.focus();
  };
  launch.onclick = () => open();
  async function open(messageId) {
    windowBox.hidden = false;
    launch.hidden = true;
    launch.setAttribute("aria-expanded", "true");
    if (floatingPosition && !root.classList.contains("docked")) position(floatingPosition.left, floatingPosition.top);
    form.elements.message.focus();
    const gid = gameId();
    if (busy && messageId) throw Error("私聊正在加载，请稍后重试");
    if (busy || (gid === loadedGame && !messageId)) return;
    busy = true;
    form.querySelector('[type="submit"]').disabled = true;
    status.textContent = "正在读取聊天记录…";
    try {
      const data = await request("state", gid);
      updateIdentity(data);
      const {chat} = data;
      if (messageId && !chat.some(message => message.id === messageId)) {
        const {entry} = await request("chat-message", gid, undefined, {id: messageId});
        chat.unshift(entry);
      }
      log.replaceChildren();
      for (const message of chat) {
        const parts = message.role === "assistant" ? await splitReply(message.content) : [message.content];
        parts.forEach((part, i) => add(message.role, part, i === 0 ? message.attachments : [], i === 0 ? message.id : undefined));
      }
      lastProactiveId = chat.at(-1)?.id ?? 0;
      loadedGame = gid;
      status.textContent = "";
      if (messageId) {
        const target = document.getElementById("private-message-" + messageId);
        target?.scrollIntoView({block: "center"});
        target?.focus({preventScroll: true});
      }
    } catch (error) {
      status.textContent = error.message;
      if (messageId) throw error;
    } finally { busy = false; form.querySelector('[type="submit"]').disabled = false; }
  }
  setInterval(async () => {
    const gid = gameId();
    if (busy || polling || document.hidden || !loadedGame || gid !== loadedGame ||
        windowBox.hidden || identity.agent_mode === "external") return;
    polling = true;
    try {
      const {entries} = await request("proactive-chat", gid, undefined, {after: lastProactiveId});
      if (busy || gid !== loadedGame || gameId() !== gid) return;
      for (const entry of entries) {
        add("assistant", entry.content, [], entry.id);
        lastProactiveId = entry.id;
      }
    } catch {
      // A later poll retries without interrupting the chat.
    } finally { polling = false; }
  }, 5000);

  async function splitReply(reply) {
    if (identity.split_reply === false || /(^|\n)\s*(?:[#|>]|[-*+]\s|\d+[.)]\s)/.test(reply) || reply.includes("```"))
      return [reply];
    if (identity.split_pattern) {
      return new Promise(resolve => {
        let worker;
        try { worker = new Worker("/assets/split-reply.js"); }
        catch { resolve([reply]); return; }
        const finish = parts => { clearTimeout(timer); worker.terminate(); resolve(parts?.length ? parts : [reply]); };
        const timer = setTimeout(() => finish(null), 500);
        worker.onmessage = event => finish(event.data);
        worker.onerror = () => finish(null);
        worker.postMessage({reply, pattern: identity.split_pattern});
      });
    }
    const parts = reply.split(/\n+|(?<=[。！？!?])\s*|(?<=[.;])\s+|[ \t]{2,}/)
      .map(part => part.trim()).filter(Boolean);
    return parts.length ? parts : [reply];
  }
  function setFull(full) {
    root.classList.toggle("full-chat", full);
    (full ? document.querySelector('[data-page="conversation"]') : document.body).append(root);
    windowBox.removeAttribute("style");
    expand.textContent = full ? "退出全屏" : "全屏";
    dock.hidden = full;
    if (full) {
      onExpand();
      if (windowBox.hidden) launch.click();
    } else if (floatingPosition && !root.classList.contains("docked")) position(floatingPosition.left, floatingPosition.top);
  }
  expand.onclick = () => root.classList.contains("full-chat") ? onCollapse() : setFull(true);
  const close = find("[data-close]").onclick;
  find("[data-close]").onclick = () => {
    if (root.classList.contains("full-chat")) onCollapse();
    close();
  };
  window.addEventListener("kurumi-identity", () => request("state", gameId()).then(updateIdentity).catch(() => {}));
  request("state", gameId()).then(updateIdentity).catch(() => {});
  const imagePreview = find(".chat-upload-preview"), picture = form.elements.picture;
  let imageDraft = null, preparingImage = false;
  function setImage(src) {
    imageDraft = src;
    imagePreview.hidden = !src;
    const thumbnail = imagePreview.querySelector("img");
    if (src) thumbnail.src = src;
    else thumbnail.removeAttribute("src");
  }
  async function addImage(file) {
    if (!file || busy || preparingImage) return;
    preparingImage = true;
    status.textContent = "";
    try { setImage(await prepareImage(file)); }
    catch (error) { status.textContent = error.message || "图片无法读取"; }
    finally { preparingImage = false; }
  }
  find("[data-pick-image]").onclick = () => { if (!busy) picture.click(); };
  find("[data-remove-image]").onclick = () => setImage(null);
  picture.onchange = () => { addImage(picture.files[0]); picture.value = ""; };
  form.addEventListener("dragover", event => {
    if (event.dataTransfer.types.includes("Files")) event.preventDefault();
  });
  form.addEventListener("drop", event => {
    const file = event.dataTransfer.files[0];
    if (!file) return;
    event.preventDefault();
    addImage(file);
  });
  form.onsubmit = async event => {
    event.preventDefault();
    const message = form.elements.message.value.trim();
    const image = imageDraft;
    if ((!message && !image) || busy || preparingImage) return;
    busy = true;
    const button = form.querySelector('[type="submit"]');
    button.disabled = true;
    form.reset();
    setImage(null);
    const sent = add("user", message, image ? [{kind: "image", src: image, label: "用户图片"}] : []);
    const sentImage = image ? log.lastElementChild : null;
    const pending = add("assistant", "");
    pending.classList.add("chat-typing");
    pending.setAttribute("aria-label", identity.agent_name + "正在回复");
    pending.append(...Array.from({length: 3}, () => document.createElement("span")));
    status.textContent = "";
    try {
      const {reply, attachments} = await request("chat", gameId(), {message, image: image || undefined});
      const parts = await splitReply(reply);
      const minimum = identity.split_delay_min ?? 1.5;
      const maximum = identity.split_delay_max ?? 3;
      for (const [index, part] of parts.entries()) {
        if (parts.length > 1 && maximum > 0) {
          const seconds = minimum + Math.random() * (maximum - minimum);
          await new Promise(resolve => setTimeout(resolve, seconds * 1000));
        }
        if (index === 0) pending.remove();
        add("assistant", part, index === 0 ? attachments : []);
      }
      status.textContent = "";
    } catch (error) {
      sent.remove();
      sentImage?.remove();
      pending.remove();
      if (image) setImage(image);
      if (!form.elements.message.value) form.elements.message.value = message;
      status.textContent = error.message;
    } finally { busy = false; button.disabled = false; }
  };
  return {setFull, openMessage: open, launcher: launch};
}
