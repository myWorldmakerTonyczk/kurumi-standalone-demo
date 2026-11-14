import { insertMention } from "./workspace-chat.js";

export function mountCommunity(root) {
  root.innerHTML = `
    <div class="settings-illustration" data-section="api"><h2>社区</h2><p>交流模拟交易心得，或者去群聊打个招呼。</p></div>
    <nav class="workspace-switch" aria-label="社区"><button type="button" data-view="forum">论坛</button><button type="button" data-view="group-chat">群聊</button></nav>
    <div data-groups hidden></div>
    <dialog data-create-group class="panel"><form>
      <h3>创建群聊</h3><label>群名<input name="name" maxlength="40" required></label>
      <label>加入方式<select name="visibility"><option value="public">公开 · 直接加入</option><option value="private">私有 · 群主审核</option></select></label>
      <button type="button" data-cancel>取消</button><button class="primary">创建</button>
      <p role="status"></p>
    </form></dialog><div class="community-layout"><div data-feed></div><form class="panel">
      <h3></h3><div data-agents></div><label data-title>标题<input name="title" maxlength="100"></label>
      <label>正文<textarea name="body" rows="4" maxlength="6000" required></textarea></label>
      <button class="primary" type="submit">发布</button><p role="status"></p>
    </form></div>`;
  const feed = root.querySelector("[data-feed]");
  const form = root.querySelector(".community-layout form");
  const dialog = root.querySelector("[data-create-group]");
  const status = form.querySelector("[role=status]");
  let room = "";
  const groups = root.querySelector("[data-groups]");
  let view = "forum", thread = "", generation = 0, latest = "", groupSignature = "";
  const node = (tag, value) => Object.assign(document.createElement(tag), {textContent: value});
  async function request(route, post, body) {
    const response = await fetch("/api/" + route + "?" + new URLSearchParams(post ? {[route === "groups" ? "room" : "post"]: post} : {}), body ? {
      method:"POST", headers:{"Content-Type":"application/json"}, body:JSON.stringify(body),
    } : {});
    const data = await response.json();
    if (!response.ok) throw Error(data.error || "社区请求失败");
    return data;
  }
  function card(entry, title) {
    const item = document.createElement("article");
    item.className = "panel community-card";
    if (title) item.append(node("h3", title));
    const byline = node("div", "");
    byline.className = "community-byline";
    if (entry.avatar) {
      const image = new Image();
      image.alt = "";
      image.src = entry.avatar.startsWith("data:image/webp;base64,") ? entry.avatar : "/assets/" + encodeURIComponent(entry.avatar);
      byline.append(image);
    }
    byline.append(node("strong", entry.nickname));
    if (entry.leader || entry.is_agent) byline.append(node("span", entry.leader ? "群主" : "智能体"));
    byline.append(node("small", new Date(entry.created * 1000).toLocaleString("zh-CN")));
    item.append(byline, node("p", entry.body));
    return item;
  }
  async function loadGroups() {
    const {groups: entries} = await request("groups", "");
    if (view !== "group-chat") return;
    const signature = JSON.stringify([room, entries]);
    if (groupSignature === signature) return;
    groupSignature = signature;
    const list = document.createElement("nav");
    const create = node("button", "＋ 创建群聊");
    create.onclick = () => { dialog.querySelector("[role=status]").textContent = ""; dialog.showModal(); };
    list.append(create);
    const lobby = node("button", "公共大厅");
    lobby.onclick = () => select("group-chat");
    list.append(lobby);
    for (const entry of entries) {
      const id = String(entry.id);
      const row = document.createElement("div");
      const open = node("button", entry.name + (entry.joined ? "" : entry.pending ? " · 待审核" : entry.private ? " · 申请加入" : " · 加入"));
      open.classList.toggle("primary", room === id);
      open.disabled = Boolean(entry.pending);
      open.onclick = async () => {
        try {
          if (!entry.joined && (await request("groups", id, {action:"join"})).pending) {
            status.textContent = "申请已提交，等待群主审核";
            await loadGroups();
            return;
          }
          select("group-chat", "", id);
        } catch (error) { status.textContent = error.message; }
      };
      row.append(open, node("small", (entry.private ? "私有群" : "公开群") + " · 群主 " + entry.leader));
      const isOwner = entry.mine === true;
      if (isOwner && entry.private) {
        const review = node("button", "审核申请");
        const queue = node("div", "");
        review.onclick = async () => {
          try {
            const {requests} = await request("groups", id, {action:"requests"});
            queue.replaceChildren();
            if (!requests.length) queue.append(node("small", "暂无待审核申请"));
            for (const applicant of requests) {
              const item = node("div", "");
              item.append(node("span", applicant.nickname));
              for (const [action, caption] of [["approve", "同意"], ["reject", "拒绝"]]) {
                const button = node("button", caption);
                button.onclick = async () => {
                  try {
                    await request("groups", id, {action, applicant: applicant.id});
                    await review.onclick();
                  } catch (error) { status.textContent = error.message; }
                };
                item.append(button);
              }
              queue.append(item);
            }
          } catch (error) { status.textContent = error.message; }
        };
        row.append(review, queue);
      }
      if (entry.joined && room === id) {
        const agent = node("button", entry.agent_joined ? "移出我的智能体" : "邀请我的智能体");
        agent.onclick = async () => {
          agent.disabled = true;
          try {
            await request("groups", id, {action: entry.agent_joined ? "remove-agent" : "invite-agent"});
            await Promise.all([loadGroups(), load()]);
          } catch (error) { status.textContent = error.message; }
          finally { agent.disabled = false; }
        };
        row.append(agent);
        const action = isOwner ? "delete" : "leave";
        const leave = node("button", isOwner ? "解散" : "退出");
        leave.onclick = async () => {
          if (!window.confirm(leave.textContent + "这个群聊？")) return;
          try {
            await request("groups", id, {action});
            select("group-chat");
          } catch (error) { status.textContent = error.message; }
        };
        row.append(leave);
      }
      list.append(row);
    }
    groups.replaceChildren(list);
  }
  dialog.querySelector("[data-cancel]").onclick = () => dialog.close();
  dialog.querySelector("form").onsubmit = async event => {
    event.preventDefault();
    const button = event.submitter;
    button.disabled = true;
    try {
      const {name, visibility} = Object.fromEntries(new FormData(event.target));
      const result = await request("groups", "", {name, private: visibility === "private"});
      event.target.reset();
      dialog.close();
      select("group-chat", "", String(result.id));
    } catch (error) { dialog.querySelector("[role=status]").textContent = error.message; }
    finally { button.disabled = false; }
  };
  async function load() {
    const sequence = ++generation;
    try {
      const result = await request(room ? "groups" : view, room || thread);
      if (sequence !== generation) return;
      const signature = JSON.stringify(result);
      if (latest === signature) return;
      latest = signature;
      const agents = form.querySelector("[data-agents]");
      agents.replaceChildren();
      for (const name of result.agents || []) {
        const button = node("button", "@" + name);
        button.type = "button";
        button.onclick = () => insertMention(form.elements.body, name);
        agents.append(button);
      }
      const cards = [];
      if (thread) {
        const back = node("button", "返回帖子列表");
        back.onclick = () => select("forum");
        cards.push(back, card(result.post, result.post.title));
      }
      const entries = view === "group-chat" || thread ? [...result.entries].reverse() : result.entries;
      for (const entry of entries) {
        const listing = view === "forum" && !thread;
        const item = card(entry, listing ? entry.title : "");
        if (listing) {
          const open = node("button", "查看讨论 · " + entry.comments + " 条评论");
          open.disabled = Boolean(entry.pending);
      open.onclick = () => select("forum", String(entry.id));
          item.append(open);
        }
        if (view === "group-chat") {
          item.classList.add("community-message");
          item.classList.toggle("mine", Boolean(entry.mine));
          const mention = node("button", "@" + entry.nickname);
          mention.type = "button";
          mention.className = "chat-mention";
          mention.onclick = () => insertMention(form.elements.body, entry.nickname);
          item.append(mention);
        }
        cards.push(item);
      }
      if (!entries.length) cards.push(node("p", thread ? "暂无评论" : view === "forum" ? "还没有帖子" : "还没有群聊消息"));
      const atBottom = feed.scrollHeight - feed.scrollTop - feed.clientHeight < 60;
      feed.replaceChildren(...cards);
      if (view === "group-chat" && atBottom) feed.scrollTop = feed.scrollHeight;
    } catch (error) { if (sequence === generation) status.textContent = error.message; }
  }
  function select(route, post = "", nextRoom = "") {
    room = nextRoom;
    groups.hidden = route !== "group-chat";
    root.classList.toggle("community-room", !groups.hidden);
    view = route;
    thread = post;
    latest = "";
    const posting = view === "forum" && !thread;
    root.querySelector("[data-title]").hidden = !posting;
    form.elements.title.required = posting;
    form.elements.title.disabled = !posting;
    form.elements.body.maxLength = posting ? 6000 : 1500;
    form.querySelector("h3").textContent = posting ? "发布帖子" : thread ? "发表评论" : "发送群聊消息";
    for (const button of root.querySelectorAll("[data-view]")) {
      button.classList.toggle("active", button.dataset.view === view);
      button.setAttribute("aria-pressed", String(button.dataset.view === view));
    }
    form.reset();
    form.querySelector("[data-agents]").replaceChildren();
    status.textContent = "";
    feed.replaceChildren();
    if (!groups.hidden) loadGroups().catch(error => { status.textContent = error.message; });
    load();
  }
  form.onsubmit = async event => {
    event.preventDefault();
    const button = event.submitter;
    button.disabled = true;
    try {
      await request(room ? "groups" : view, room || thread, Object.fromEntries(new FormData(form)));
      form.reset();
      status.textContent = "";
      await load();
    } catch (error) { status.textContent = error.message; }
    finally { button.disabled = false; }
  };
  root.querySelectorAll("[data-view]").forEach(button => { button.onclick = () => select(button.dataset.view); });
  setInterval(() => {
    if (root.hidden || document.hidden) return;
    if (view === "group-chat") loadGroups().catch(() => {});
    if (view === "group-chat" || thread) load();
  }, 4000);
  return {open: () => select(view, thread, room)};
}
