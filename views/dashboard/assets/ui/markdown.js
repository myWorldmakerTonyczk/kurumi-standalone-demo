// Render a limited Markdown subset using text nodes and safe HTTP(S) links.
export function renderMarkdown(root, content) {
  const create = (tag, parent, value) => {
    const element = document.createElement(tag);
    if (value !== undefined) element.textContent = value;
    parent.append(element);
    return element;
  };
  function inline(parent, value) {
    const pattern = /`([^`]+)`|\*\*(.+?)\*\*|__(.+?)__|\*([^*]+)\*|\[([^\]\n]+)\]\((https?:\/\/[^\s)]+)\)/g;
    let offset = 0;
    for (const match of value.matchAll(pattern)) {
      parent.append(value.slice(offset, match.index));
      if (match[5]) {
        const link = create("a", parent, match[5]);
        link.href = match[6];
        link.target = "_blank";
        link.rel = "noopener noreferrer";
      } else {
        create(match[1] ? "code" : match[4] ? "em" : "strong", parent,
          match[1] ?? match[2] ?? match[3] ?? match[4]);
      }
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
