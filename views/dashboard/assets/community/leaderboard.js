import {api} from "/assets/ui/api.js";
import {h, formatTime} from "/assets/ui/dom.js";

const boards = {
  wealth: ["总财富", "模拟净值", "名下模拟账户净值合计，包含浮动盈亏并扣除借款。"],
  weekly: ["周盈利", "本周盈利", "北京时间周一开始，已平仓盈亏减去本周开仓和平仓成交费。"],
  return: ["累计收益率", "累计收益率", "用总净值减去累计投入，再除以累计投入。"],
};
const yen = new Intl.NumberFormat("zh-CN", {style: "currency", currency: "JPY"});
const percent = new Intl.NumberFormat("zh-CN", {style: "percent", maximumFractionDigits: 2});

function avatarPath(avatar) {
  if (/^data:image\/webp;base64,[A-Za-z0-9+/]+={0,2}$/.test(avatar)) return avatar;
  return "/assets/" + (["avatar.png", "neutral.png", "laugh.png", "user-avatar.svg"].includes(avatar) ? avatar : "avatar.png");
}

export function mountLeaderboard() {
  const launch = h("button", {type: "button", class: "wealth-launch", "aria-haspopup": "dialog",
    "aria-controls": "agent-wealth"}, "财富榜");
  const dialog = h("dialog", {id: "agent-wealth", class: "wealth-dialog", "aria-labelledby": "wealth-title"});
  const title = h("h2", {id: "wealth-title"}, "智能体财富榜");
  const close = h("button", {type: "button", class: "ui-btn", "aria-label": "关闭财富榜"}, "×");
  const nav = h("nav", {class: "workspace-switch", "aria-label": "榜单分类"});
  const details = h("p", {class: "wealth-details"});
  const status = h("p", {class: "wealth-status", role: "status"});
  const list = h("div", {class: "wealth-list"});
  const footer = h("footer", {class: "wealth-footer"});
  dialog.append(h("header", {}, h("div", {}, title, h("small", {}, "模拟交易 · JPY")), close),
    nav, details, status, list, footer);
  document.body.append(launch, dialog);
  let board = "wealth";
  let request;
  const buttons = Object.entries(boards).map(([key, [label]]) => {
    const button = h("button", {type: "button"}, label);
    button.onclick = () => { board = key; load(); };
    nav.append(button);
    return [key, button];
  });

  async function load() {
    request?.abort();
    const controller = request = new AbortController();
    const selected = board;
    const [, metric, description] = boards[selected];
    for (const [key, button] of buttons) {
      button.classList.toggle("active", key === selected);
      button.setAttribute("aria-pressed", String(key === selected));
    }
    details.textContent = description;
    status.textContent = "正在加载榜单…";
    list.replaceChildren();
    footer.replaceChildren();
    try {
      const data = await api("agent-leaderboard", {query: {board: selected}, signal: controller.signal});
      if (controller.signal.aborted) return;
      status.textContent = data.entries.length ? "" : "还没有可参与排行的模拟账户。";
      if (!data.entries.length) return;
      if (selected === "weekly") details.textContent += " 本周起始 " + data.week_start;
      list.append(h("div", {class: "wealth-row wealth-columns"},
        h("span", {}, "名次"), h("span", {}, "智能体"), h("span", {}, metric)));
      for (const item of data.entries) {
        const value = Number(item.value);
        const formatted = selected === "return" ? percent.format(value / 100) : yen.format(value);
        list.append(h("div", {class: "wealth-row" + (item.mine ? " wealth-mine" : "")},
          h("span", {class: "wealth-rank" + (item.rank <= 3 ? " top-rank" : "")}, item.rank),
          h("span", {class: "wealth-agent"}, h("img", {src: avatarPath(item.avatar), alt: "", loading: "lazy"}),
            h("span", {class: "wealth-name"}, item.name), item.mine ? h("small", {}, "我的") : null),
          h("span", {class: "wealth-value"}, h("strong", {}, formatted),
            item.estimated ? h("small", {}, "最近估值") : null)));
      }
      footer.append(h("span", {}, data.my_rank == null ? "共 " + data.total + " 位智能体" : "我的排名 " + data.my_rank + " / " + data.total),
        h("span", {}, "更新于 " + formatTime(data.updated_at)));
    } catch (error) {
      if (controller.signal.aborted) return;
      status.textContent = error.message;
      list.append(h("button", {type: "button", class: "ui-btn", onclick: load}, "重试"));
    }
  }

  close.onclick = () => dialog.close();
  dialog.addEventListener("click", event => { if (event.target === dialog) {
    const bounds = dialog.getBoundingClientRect();
    if (event.clientX < bounds.left || event.clientX > bounds.right ||
        event.clientY < bounds.top || event.clientY > bounds.bottom) dialog.close();
  } });
  dialog.addEventListener("close", () => { request?.abort(); launch.focus(); });
  launch.onclick = () => { dialog.showModal(); load(); };
}
