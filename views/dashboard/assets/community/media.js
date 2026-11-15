export async function prepareImage(file) {
  if (!file?.type.startsWith("image/") || file.size > 10 * 1024 * 1024)
    throw Error("请拖入一张不超过 10 MB 的图片");
  const bitmap = await createImageBitmap(file);
  try {
    const canvas = document.createElement("canvas");
    const context = canvas.getContext("2d");
    for (const dimension of [960, 720, 480, 320]) {
      const scale = Math.min(1, dimension / Math.max(bitmap.width, bitmap.height));
      canvas.width = Math.max(1, Math.round(bitmap.width * scale));
      canvas.height = Math.max(1, Math.round(bitmap.height * scale));
      context.drawImage(bitmap, 0, 0, canvas.width, canvas.height);
      const image = canvas.toDataURL("image/webp", .76);
      if (image.startsWith("data:image/webp;base64,") && image.length <= 56000) return image;
    }
    throw Error("图片压缩后仍过大");
  } finally { bitmap.close(); }
}
