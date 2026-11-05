import { test } from "node:test";
import assert from "node:assert/strict";
import { renderMarkdown } from "../../src/web_agent/static/markdown.js";

class Element {
  constructor(tagName) { this.tagName = tagName; this.children = []; }
  append(...items) { this.children.push(...items); }
  replaceChildren() { this.children = []; }
  addEventListener() {}
}
const descendants = node => [node, ...node.children.flatMap(descendants)];
const text = node => node.textContent || node.children.map(text).join("");
function render(source, root = new Element("div")) {
  const previous = globalThis.document;
  globalThis.document = {
    createElement: tag => new Element(tag),
    createTextNode: value => Object.assign(new Element("text"), { textContent: value }),
  };
  try {
    renderMarkdown(root, source);
    return root;
  } finally {
    if (previous) globalThis.document = previous; else delete globalThis.document;
  }
}

test("a table immediately after a bold paragraph renders headers and cells", () => {
  for (const gap of ["\n", "\n\n"]) {
    const root = render("**十项安排的落点**" + gap + "| 项 | 本轮处理 |\n|---|---|\n| 预算 | **待确认** |\n\n后续说明");
    assert.deepEqual(root.children.map(node => node.tagName), ["p", "div", "p"]);
    const nodes = descendants(root);
    assert.equal(nodes.filter(node => node.tagName === "table").length, 1);
    assert.deepEqual(nodes.filter(node => node.tagName === "th").map(text), ["项", "本轮处理"]);
    assert.deepEqual(nodes.filter(node => node.tagName === "td").map(text), ["预算", "待确认"]);
  }
});

test("lists immediately after a paragraph render separate list items", () => {
  for (const [marker, tag] of [["-", "ul"], ["*", "ul"], ["+", "ul"], ["1.", "ol"]]) {
    const root = render(`**待确认事项**\n${marker} 地区\n${marker} 周期\n后续说明`);
    assert.deepEqual(root.children.map(node => node.tagName), ["p", tag, "p"]);
    assert.deepEqual(root.children[1].children.map(text), ["地区", "周期"]);
  }
});

test("ordinary multiline text and pipes remain in one paragraph", () => {
  const source = "说明\n选项 A | B\n不是表格分隔行\n1.5 元\n-无空格";
  const root = render(source);
  assert.deepEqual(root.children.map(node => node.tagName), ["p"]);
  assert.equal(text(root), source);
});

test("table and list syntax inside a fenced code block stays literal", () => {
  const source = "| 项 | 值 |\n|---|---|\n- 示例";
  const root = render("说明\n```markdown\n" + source + "\n```");
  assert.equal(descendants(root).filter(node => ["table", "ul", "ol"].includes(node.tagName)).length, 0);
  assert.equal(text(descendants(root).find(node => node.tagName === "code")), source);
});

test("rerendering a streamed table replaces the earlier partial paragraph", () => {
  const root = render("**安排**\n| 项 | 值 |");
  render("**安排**\n| 项 | 值 |\n|---|---|\n| 地区 | 待确认 |", root);
  assert.equal(descendants(root).filter(node => node.tagName === "table").length, 1);
  assert.equal(root.children.length, 2);
});
