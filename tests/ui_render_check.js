/* 前端纯函数的渲染断言。
 *
 * 为什么要单独跑 Node：`highlightPython` / `renderRich` 是**把不可信文本
 * 拼成 HTML** 的函数，转义漏一处就是一个 XSS。而它们全是纯字符串逻辑，
 * 在 Node 里 stub 掉 DOM 就能直接验真实行为 —— 比在 Python 里用正则
 * 猜它们在干什么可靠得多。
 *
 * 用法：node tests/ui_render_check.js web/index.html
 * 输出：每个断言一行；失败时进程退出码非 0。
 */
"use strict";

const fs = require("fs");
const path = require("path");

const htmlPath = process.argv[2] || path.join(__dirname, "..", "web", "index.html");
const html = fs.readFileSync(htmlPath, "utf-8");

const match = html.match(/<script>([\s\S]*?)<\/script>/);
if (!match) {
  console.error("FAIL: 页面里没有找到 <script> 块");
  process.exit(1);
}
const script = match[1];

/* --- stub 掉 DOM，让脚本能被求值 --- */
function stubEl() {
  const node = {
    style: {},
    classList: { add() {}, remove() {}, toggle() {}, contains() { return false; } },
    dataset: {},
    children: [],
    className: "",
    textContent: "",
    innerHTML: "",
    value: "",
    disabled: false,
    appendChild(child) { this.children.push(child); return child; },
    removeChild() {},
    remove() {},
    querySelector() { return null; },
    querySelectorAll() { return []; },
    addEventListener() {},
    setAttribute() {},
    focus() {},
    click() {},
    closest() { return null }
  };
  return node;
}
global.document = {
  getElementById: () => stubEl(),
  createElement: () => stubEl(),
  querySelectorAll: () => [],
  addEventListener() {}
};
global.window = global;
// Node 22 把 navigator 定义成了只读 getter，直接赋值会 TypeError
Object.defineProperty(global, "navigator", {
  value: { clipboard: { writeText: async () => {} } },
  configurable: true,
  writable: true
});
global.fetch = async () => ({ ok: true, json: async () => ({}) });
global.FormData = class { append() {} };

let exported;
try {
  // 把脚本包进函数作用域并取出要测的纯函数
  exported = new Function(script + "\n;return { escapeHtml, highlightPython, renderRich, STATUS_LABEL };")();
} catch (err) {
  console.error("FAIL: 脚本求值失败 —— " + err.message);
  process.exit(1);
}

const { escapeHtml, highlightPython, renderRich, STATUS_LABEL } = exported;
let failures = 0;

function check(name, condition, detail) {
  if (condition) {
    console.log("  ok   " + name);
  } else {
    failures += 1;
    console.log("  FAIL " + name + (detail ? "  →  " + detail : ""));
  }
}

/* ---------------- escapeHtml ---------------- */
check("escapeHtml 转义尖括号", escapeHtml("<b>") === "&lt;b&gt;", escapeHtml("<b>"));
check("escapeHtml 转义引号", escapeHtml('"x"') === "&quot;x&quot;");
check("escapeHtml 处理 null", escapeHtml(null) === "");

/* ---------------- highlightPython ---------------- */
const code = "import pandas as pd\n# 读取数据\ndf = pd.read_csv('/data/销售.csv')\nprint(df.head(5))";
const hl = highlightPython(code);
check("高亮：关键字被着色", hl.includes('class="tok-kw">import<'));
check("高亮：字符串被着色", hl.includes("tok-str"));
check("高亮：注释被着色", hl.includes('class="tok-com"># 读取数据<'));
check("高亮：数字被着色", hl.includes('class="tok-num"'));

const evilCode = 'x = "<script>alert(1)</script>"\nprint(x)';
const evilHl = highlightPython(evilCode);
check("高亮：代码里的 <script> 被转义，不可执行",
  !evilHl.includes("<script>") && evilHl.includes("&lt;script&gt;"), evilHl.slice(0, 120));

const quoteCode = "s = '闭合引号测试'\nprint(s)";
check("高亮：单引号字符串不吞掉后续内容",
  escapeHtml("print(s)").split("&")[0].length > 0 && highlightPython(quoteCode).includes("tok-kw\">print<"));

check("高亮：空输入不崩", highlightPython("") === "");

/* ---------------- renderRich ---------------- */
const table = "| 地区 | 销售额 |\n|---|---|\n| 华南 | 138000 |";
const tableHtml = renderRich(table);
check("Markdown：表格渲染出 thead/tbody", tableHtml.includes("<table>") && tableHtml.includes("<tbody>"));
check("Markdown：分隔行不显示为内容", !tableHtml.includes("---"));
check("Markdown：表头正确", tableHtml.includes("<th>地区</th>"));

const bold = renderRich("**华南** 最高，代码 `df.head()`");
check("Markdown：粗体", bold.includes("<strong>华南</strong>"));
check("Markdown：行内代码", bold.includes("<code>df.head()</code>"));

const evilMd = "看看这个 <img src=x onerror=alert(1)> 和 <script>bad()</script>";
const evilMdHtml = renderRich(evilMd);
check("Markdown：HTML 被转义，img/script 不可执行",
  !evilMdHtml.includes("<img") && !evilMdHtml.includes("<script>"),
  evilMdHtml.slice(0, 140));

const listMd = renderRich("- 第一项\n- 第二项");
check("Markdown：无序列表", listMd.includes("<ul>") && listMd.includes("<li>第一项</li>"));

/* ---------------- 答案里的图片 ---------------- */
const imgMd = renderRich("柱状图如下：\n![各地区销售额](各地区销售额_4.png)");
check("Markdown：图片渲染成产物链接",
  imgMd.includes('src="/api/artifact/') && imgMd.includes("<img"), imgMd.slice(0, 160));

const imgEsc = renderRich('![<script>alert(1)</script>](x.png)');
check("Markdown：图片 alt 被转义",
  !imgEsc.includes("<script>") && imgEsc.includes("&lt;script&gt;"));

const imgTraversal = renderRich("![x](../../../.env)");
check("Markdown：图片路径只取文件名（穿越由服务端再拦一道）",
  imgTraversal.includes('src="/api/artifact/.env"') && !imgTraversal.includes("../"),
  imgTraversal.slice(0, 120));

/* ---------------- 状态徽章 ---------------- */
const expected = ["OK", "RUNTIME_ERROR", "TIMEOUT", "OOM", "REJECTED", "SANDBOX_ERROR"];
check("状态映射覆盖六分类",
  expected.every(s => Object.prototype.hasOwnProperty.call(STATUS_LABEL, s)),
  JSON.stringify(Object.keys(STATUS_LABEL)));

console.log("");
if (failures > 0) {
  console.log("前端渲染断言： " + failures + " 项失败");
  process.exit(1);
}
console.log("前端渲染断言全部通过");
