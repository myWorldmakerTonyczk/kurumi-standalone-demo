import {h} from "/assets/ui/dom.js";

export async function cropAvatar(file) {
  if (file.type && !["image/png", "image/jpeg", "image/webp"].includes(file.type))
    throw Error("请选择 PNG、JPEG 或 WebP 图片。");
  const photo = await createImageBitmap(file);
  return new Promise(resolve => {
    const title = h("h2", {id: "avatar-crop-title"}, "裁剪头像");
    const crop = h("canvas", {tabindex: "0", "aria-label": "头像裁剪选区"});
    const size = h("input", {type: "range", min: 25, max: 100, value: 75});
    const cancel = h("button", {type: "button"}, "取消");
    const apply = h("button", {type: "button", class: "avatar-crop-apply"}, "使用头像");
    const editor = h("dialog", {class: "avatar-crop-dialog", "aria-labelledby": title.id},
      title, h("p", {}, "拖动圆形选区，也可以用方向键微调。"), crop,
      h("label", {}, "圆框大小", size), h("div", {class: "avatar-editor-actions"}, cancel, apply));
    const context = crop.getContext("2d");
    const scale = Math.min(480 / photo.width, 350 / photo.height);
    crop.width = Math.max(1, Math.round(photo.width * scale));
    crop.height = Math.max(1, Math.round(photo.height * scale));
    let x = crop.width / 2, y = crop.height / 2;
    let drag, radius, value = null;
    function renderCrop() {
      radius = Math.min(crop.width, crop.height) * Number(size.value) / 200;
      x = Math.max(radius, Math.min(crop.width - radius, x));
      y = Math.max(radius, Math.min(crop.height - radius, y));
      context.drawImage(photo, 0, 0, crop.width, crop.height);
      context.fillStyle = "#23152999";
      context.beginPath();
      context.rect(0, 0, crop.width, crop.height);
      context.arc(x, y, radius, 0, Math.PI * 2);
      context.fill("evenodd");
      context.beginPath();
      context.arc(x, y, radius, 0, Math.PI * 2);
      context.lineWidth = 2;
      context.strokeStyle = "#fff";
      context.stroke();
    }

    function moveCrop(event) {
      const rect = crop.getBoundingClientRect();
      x = (event.clientX - rect.left) * crop.width / rect.width - drag.x;
      y = (event.clientY - rect.top) * crop.height / rect.height - drag.y;
      renderCrop();
    }

    crop.onpointerdown = event => {
      const rect = crop.getBoundingClientRect();
      const px = (event.clientX - rect.left) * crop.width / rect.width;
      const py = (event.clientY - rect.top) * crop.height / rect.height;
      drag = Math.hypot(px - x, py - y) <= radius ? {x: px - x, y: py - y} : {x: 0, y: 0};
      crop.setPointerCapture(event.pointerId);
      moveCrop(event);
    };
    crop.onpointermove = event => { if (drag) moveCrop(event); };
    crop.onlostpointercapture = () => { drag = null; };
    crop.onkeydown = event => {
      const offset = {ArrowLeft: [-1, 0], ArrowRight: [1, 0], ArrowUp: [0, -1], ArrowDown: [0, 1]}[event.key];
      if (!offset) return;
      event.preventDefault();
      x += offset[0] * 3;
      y += offset[1] * 3;
      renderCrop();
    };
    size.oninput = renderCrop;
    cancel.onclick = () => editor.close();
    apply.onclick = () => {
      const output = document.createElement("canvas");
      output.width = output.height = 96;
      const draw = output.getContext("2d");
      draw.beginPath();
      draw.arc(48, 48, 48, 0, Math.PI * 2);
      draw.clip();
      const scaleX = photo.width / crop.width;
      const scaleY = photo.height / crop.height;
      draw.drawImage(photo, (x-radius)*scaleX, (y-radius)*scaleY, 2*radius*scaleX, 2*radius*scaleY, 0, 0, 96, 96);
      for (const quality of [0.78, 0.62, 0.45, 0.25]) {
        value = output.toDataURL("image/webp", quality);
        if (value.length <= 24000) break;
      }
      editor.close();
    };
    editor.onclose = () => {
      photo.close();
      editor.remove();
      resolve(value);
    };
    document.body.append(editor);
    renderCrop();
    editor.showModal();
    crop.focus();
  });
}
