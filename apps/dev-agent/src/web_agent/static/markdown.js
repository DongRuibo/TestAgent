function appendText(parent, value) {
  parent.append(document.createTextNode(value));
}

function safeHttpUrl(value) {
  if (/^\/api\/conversations\/[0-9a-f-]{36}\/weekly-plan(?:\?version=[1-9]\d*)?$/i.test(value)) return value;
  try {
    const url = new URL(value);
    if (!["http:", "https:"].includes(url.protocol)) return null;
    return url.href;
  } catch (_) {
    return null;
  }
}

function appendInline(parent, source) {
  const pattern = /(`[^`]+`|\*\*[^*]+\*\*|\*[^*]+\*|\[[^\]]+\]\([^)]+\))/g;
  let cursor = 0;
  for (const match of source.matchAll(pattern)) {
    if (match.index > cursor) appendText(parent, source.slice(cursor, match.index));
    const token = match[0];
    if (token.startsWith("`")) {
      const code = document.createElement("code");
      code.textContent = token.slice(1, -1);
      parent.append(code);
    } else if (token.startsWith("**")) {
      const strong = document.createElement("strong");
      appendInline(strong, token.slice(2, -2));
      parent.append(strong);
    } else if (token.startsWith("*")) {
      const emphasis = document.createElement("em");
      appendInline(emphasis, token.slice(1, -1));
      parent.append(emphasis);
    } else {
      const parts = token.match(/^\[([^\]]+)\]\(([^)]+)\)$/);
      const href = parts ? safeHttpUrl(parts[2].trim()) : null;
      if (parts && href) {
        const link = document.createElement("a");
        link.href = href;
        link.target = "_blank";
        link.rel = "noopener noreferrer";
        appendInline(link, parts[1]);
        parent.append(link);
      } else {
        appendText(parent, token);
      }
    }
    cursor = match.index + token.length;
  }
  if (cursor < source.length) appendText(parent, source.slice(cursor));
}

function codeBlock(language, value) {
  const wrapper = document.createElement("div");
  wrapper.className = "markdown-code-block";
  const header = document.createElement("div");
  header.className = "markdown-code-header";
  const label = document.createElement("span");
  label.textContent = language || "代码";
  const copy = document.createElement("button");
  copy.type = "button";
  copy.textContent = "复制";
  copy.addEventListener("click", async () => {
    try {
      await navigator.clipboard.writeText(value);
      copy.textContent = "已复制";
      window.setTimeout(() => { copy.textContent = "复制"; }, 1600);
    } catch (_) {
      copy.textContent = "复制失败";
    }
  });
  const pre = document.createElement("pre");
  const code = document.createElement("code");
  code.textContent = value;
  pre.append(code);
  header.append(label, copy);
  wrapper.append(header, pre);
  return wrapper;
}

function isTableSeparator(line) {
  const cells = line.trim().replace(/^\||\|$/g, "").split("|");
  return cells.length > 1 && cells.every((cell) => /^\s*:?-{3,}:?\s*$/.test(cell));
}

function tableCellValues(line) {
  return line.trim().replace(/^\||\|$/g, "").split("|").map((item) => item.trim());
}

function markdownTable(lines, start) {
  const headers = tableCellValues(lines[start]);
  const rows = [];
  let index = start + 2;
  while (index < lines.length && lines[index].includes("|") && lines[index].trim()) {
    rows.push(tableCellValues(lines[index]));
    index += 1;
  }
  const wrapper = document.createElement("div");
  wrapper.className = "markdown-table-wrap";
  const table = document.createElement("table");
  const head = document.createElement("thead");
  const headRow = document.createElement("tr");
  headers.forEach((value) => {
    const cell = document.createElement("th");
    appendInline(cell, value);
    headRow.append(cell);
  });
  head.append(headRow);
  const body = document.createElement("tbody");
  rows.forEach((values) => {
    const row = document.createElement("tr");
    headers.forEach((_header, cellIndex) => {
      const cell = document.createElement("td");
      appendInline(cell, values[cellIndex] || "");
      row.append(cell);
    });
    body.append(row);
  });
  table.append(head, body);
  wrapper.append(table);
  return { node: wrapper, nextIndex: index };
}

export function renderMarkdown(container, markdown) {
  container.replaceChildren();
  const lines = String(markdown || "").replace(/\r\n?/g, "\n").split("\n");
  let index = 0;
  while (index < lines.length) {
    const line = lines[index];
    if (!line.trim()) {
      index += 1;
      continue;
    }
    const fence = line.match(/^```([\w.+-]*)\s*$/);
    if (fence) {
      const content = [];
      index += 1;
      while (index < lines.length && !/^```\s*$/.test(lines[index])) {
        content.push(lines[index]);
        index += 1;
      }
      if (index < lines.length) index += 1;
      container.append(codeBlock(fence[1], content.join("\n")));
      continue;
    }
    if (
      index + 1 < lines.length
      && line.includes("|")
      && isTableSeparator(lines[index + 1])
    ) {
      const result = markdownTable(lines, index);
      container.append(result.node);
      index = result.nextIndex;
      continue;
    }
    const heading = line.match(/^(#{1,4})\s+(.+)$/);
    if (heading) {
      const element = document.createElement(`h${heading[1].length}`);
      appendInline(element, heading[2]);
      container.append(element);
      index += 1;
      continue;
    }
    const listMatch = line.match(/^\s*(?:([-*+])|(\d+)\.)\s+(.+)$/);
    if (listMatch) {
      const ordered = Boolean(listMatch[2]);
      const list = document.createElement(ordered ? "ol" : "ul");
      while (index < lines.length) {
        const itemMatch = lines[index].match(/^\s*(?:([-*+])|(\d+)\.)\s+(.+)$/);
        if (!itemMatch || Boolean(itemMatch[2]) !== ordered) break;
        const item = document.createElement("li");
        appendInline(item, itemMatch[3]);
        list.append(item);
        index += 1;
      }
      container.append(list);
      continue;
    }
    if (line.startsWith("> ")) {
      const quote = document.createElement("blockquote");
      appendInline(quote, line.slice(2));
      container.append(quote);
      index += 1;
      continue;
    }
    const paragraph = [];
    while (index < lines.length && lines[index].trim()) {
      const current = lines[index];
      if (
        paragraph.length
        && (
          /^```/.test(current)
          || /^(#{1,4})\s+/.test(current)
          || /^\s*(?:[-*+]|\d+\.)\s+.+$/.test(current)
          || (index + 1 < lines.length && current.includes("|") && isTableSeparator(lines[index + 1]))
        )
      ) break;
      paragraph.push(current.trim());
      index += 1;
    }
    const element = document.createElement("p");
    appendInline(element, paragraph.join("\n"));
    container.append(element);
  }
}

export function isSafeResourceHref(value) {
  if (typeof value !== "string") return false;
  if (/^\/api\/resources\/[0-9a-f-]{36}$/i.test(value)) return true;
  return safeHttpUrl(value) !== null;
}
