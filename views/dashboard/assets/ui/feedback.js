/* Shared feedback primitives: confirm dialog, toasts, loading/empty/error blocks. */
import { h, clear } from "/assets/ui/dom.js";

let dialog, titleEl, bodyEl, okBtn, cancelBtn, reasonWrap, reasonLabel, reasonField;

function ensureDialog() {
  if (dialog) return;
  titleEl = h("h3", {id: "ui-dialog-title"});
  bodyEl = h("div", {id: "ui-dialog-body", class: "ui-dialog-body"});
  reasonField = h("textarea", {rows: "2", maxlength: "300", class: "ui-dialog-reason"});
  reasonLabel = h("span", {}, "原因");
  reasonWrap = h("label", {class: "ui-dialog-reasonwrap", hidden: true}, reasonLabel, reasonField);
  cancelBtn = h("button", {type: "submit", value: "cancel", class: "ui-btn", formnovalidate: true}, "取消");
  okBtn = h("button", {type: "submit", value: "confirm", class: "ui-btn ui-btn-primary"}, "确认");
  dialog = h("dialog", {class: "ui-dialog", "aria-labelledby": "ui-dialog-title", "aria-describedby": "ui-dialog-body"},
    h("form", {method: "dialog", class: "ui-dialog-form"}, titleEl, bodyEl, reasonWrap,
      h("div", {class: "ui-dialog-actions"}, cancelBtn, okBtn)));
  dialog.addEventListener("click", event => { if (event.target === dialog) dialog.close("cancel"); });
  document.body.append(dialog);
}

/** Resolves {reason} only when the user presses the confirm button; Esc, backdrop and cancel resolve false.
 *  Pass `reason: {label, required}` to collect a short note (used for audited admin actions). */
export function confirmAction({title, message = "", details = [], confirmText = "确认", cancelText = "取消", tone = "primary", reason = null}) {
  ensureDialog();
  if (dialog.open) return Promise.resolve(false);
  titleEl.textContent = title;
  clear(bodyEl).append(...[
    message ? h("p", {}, message) : null,
    details.length ? h("ul", {class: "ui-dialog-details"}, details.map(item => h("li", {}, item))) : null,
  ].filter(Boolean));
  reasonWrap.hidden = !reason;
  reasonField.value = "";
  reasonField.required = Boolean(reason?.required);
  if (reason) reasonLabel.textContent = reason.label || "原因";
  okBtn.textContent = confirmText;
  cancelBtn.textContent = cancelText;
  okBtn.className = "ui-btn " + (tone === "danger" ? "ui-btn-danger" : "ui-btn-primary");
  dialog.returnValue = "";
  return new Promise(resolve => {
    dialog.addEventListener("close", () => resolve(dialog.returnValue === "confirm" ? {reason: reasonField.value.trim()} : false), {once: true});
    dialog.showModal();
    // Destructive actions start on the safe choice.
    (tone === "danger" ? cancelBtn : okBtn).focus();
  });
}

let toasts;
export function toast(message, tone = "info") {
  if (!toasts) {
    toasts = h("div", {class: "ui-toasts"});
    document.body.append(toasts);
  }
  const item = h("p", {class: "ui-toast ui-toast-" + tone, role: tone === "error" ? "alert" : "status"}, message);
  toasts.append(item);
  setTimeout(() => item.remove(), tone === "error" ? 8000 : 4500);
}

export function stateBlock(kind, message, {retry, hint} = {}) {
  if (kind === "loading") {
    return h("div", {class: "ui-state ui-state-loading", role: "status", "aria-busy": "true"},
      h("span", {class: "ui-spinner", "aria-hidden": "true"}), h("span", {}, message || "正在加载…"));
  }
  if (kind === "error") {
    return h("div", {class: "ui-state ui-state-error", role: "alert"},
      h("p", {class: "ui-state-title"}, message || "加载失败"),
      hint ? h("p", {class: "ui-muted"}, hint) : null,
      retry ? h("button", {type: "button", class: "ui-btn", onClick: retry}, "重试") : null);
  }
  return h("div", {class: "ui-state ui-state-empty"},
    h("p", {class: "ui-state-title"}, message || "暂无内容"),
    hint ? h("p", {class: "ui-muted"}, hint) : null);
}

export function skeleton(rows = 3) {
  return h("div", {class: "ui-skeleton", "aria-hidden": "true"},
    Array.from({length: rows}, (_, i) => h("span", {class: "ui-skel", style: `width:${92 - i * 13}%`})));
}

/** Disable a button while an async task runs. Always restores it. */
export async function withBusy(button, task, busyText) {
  const label = button.textContent;
  button.disabled = true;
  button.setAttribute("aria-busy", "true");
  if (busyText) button.textContent = busyText;
  try { return await task(); }
  finally {
    button.disabled = false;
    button.removeAttribute("aria-busy");
    button.textContent = label;
  }
}

export function bindCounter(field, output, max) {
  const update = () => {
    const length = field.value.length;
    output.textContent = `${length} / ${max}`;
    output.classList.toggle("ui-over", length > max * 0.95);
  };
  field.addEventListener("input", update);
  update();
  return update;
}