self.onmessage = ({data: {reply, pattern}}) => {
  try {
    const parts = reply.split(new RegExp(pattern)).filter(part => typeof part === "string" && part.trim());
    self.postMessage(parts.length <= 100 ? parts.map(part => part.trim()) : null);
  } catch { self.postMessage(null); }
};
