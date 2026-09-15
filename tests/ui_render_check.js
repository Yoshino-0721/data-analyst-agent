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

/* 登录态 + 产物取回。
 *
 * 产物图（`<img>`）不会自己带 Authorization 头 —— 服务端 `/api/artifact/{name}`
 * 要 Bearer，于是每个图都是 401（2026-09-15 线上实测）。所以前端必须自己
 * fetch 回来再转 blob: URL；这里给出 token、记录 fetch 调用、并吐真 Blob
 * 让 `URL.createObjectURL` 在 Node 里也能真的跑。 */
global.localStorage = {
  getItem: k => (k === "auth_token" ? "T-UNIT" : null),
  setItem() {},
  removeItem() {}
};
const fetchCalls = [];
global.fetch = async (url, opts) => {
  fetchCalls.push({ url: String(url), opts: opts || {} });
  if (String(url).includes("missing")) {
    return { ok: false, status: 404, json: async () => ({ detail: "产物不存在" }) };
  }
  return {
    ok: true,
    status: 200,
    blob: async () => new Blob(["fake-png-bytes"]),
    json: async () => ({})
  };
};
global.FormData = class { append() {} };

let exported;
try {
  // 把脚本包进函数作用域并取出要测的纯函数
  exported = new Function(script + "\n;return { escapeHtml, highlightPython, renderRich, STATUS_LABEL, "
    + "inline, authHeaders, artifactBlobUrl, releaseArtifactUrls, artifactsBlock, "
    + "stderrLooksLikeError, renderStep };")();
} catch (err) {
  console.error("FAIL: 脚本求值失败 —— " + err.message);
  process.exit(1);
}

/** 把一个（stub）DOM 子树里的所有 innerHTML / textContent 拼起来，便于断言渲染结果。 */
function treeText(node) {
  if (!node) { return ""; }
  let out = (node.innerHTML || "") + (node.textContent || "");
  (node.children || []).forEach(child => { out += treeText(child); });
  return out;
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
check("Markdown：图片渲染成产物占位（真实 src 由带 Bearer 的取回填）",
  imgMd.includes('data-artifact="各地区销售额_4.png"') && imgMd.includes("<img")
  && !imgMd.includes("/api/artifact/"), imgMd.slice(0, 160));

const imgEsc = renderRich('![<script>alert(1)</script>](x.png)');
check("Markdown：图片 alt 被转义",
  !imgEsc.includes("<script>") && imgEsc.includes("&lt;script&gt;"));

const imgTraversal = renderRich("![x](../../../.env)");
check("Markdown：图片路径只取文件名（穿越由服务端再拦一道）",
  imgTraversal.includes('data-artifact=".env"') && !imgTraversal.includes("../"),
  imgTraversal.slice(0, 120));

/* ---------------- 状态徽章 ---------------- */
const expected = ["OK", "RUNTIME_ERROR", "TIMEOUT", "OOM", "REJECTED", "SANDBOX_ERROR"];
check("状态映射覆盖六分类",
  expected.every(s => Object.prototype.hasOwnProperty.call(STATUS_LABEL, s)),
  JSON.stringify(Object.keys(STATUS_LABEL)));

/* ---------------- 产物图：必须带 Bearer 取回 ---------------- */
/* 回归 2026-09-15：`<img src="/api/artifact/x.png">` 是浏览器直连，**不带**
 * Authorization 头，而该接口要登录态 → 每个图 401、页面上全是破图。
 * 正确做法：走带 token 的 fetch，转成 blob: URL 再交给 <img>。 */
(async () => {
  const { inline, authHeaders, artifactBlobUrl, releaseArtifactUrls, artifactsBlock } = exported;
  const flush = () => new Promise(resolve => setTimeout(resolve, 0));

  check("鉴权头：有 token 时带 Bearer", authHeaders().Authorization === "Bearer T-UNIT",
    JSON.stringify(authHeaders()));
  check("鉴权头：不覆盖调用方自己的头",
    authHeaders({ "X-Test": "1" }).Authorization === "Bearer T-UNIT"
    && authHeaders({ "X-Test": "1" })["X-Test"] === "1");

  fetchCalls.length = 0;
  const url = await artifactBlobUrl("图表.png");
  check("产物图：取回后给的是 blob: URL", typeof url === "string" && url.startsWith("blob:"), String(url));
  check("产物图：请求打在产物接口上",
    fetchCalls.length === 1
    && fetchCalls[0].url === "/api/artifact/" + encodeURIComponent("图表.png"),
    JSON.stringify(fetchCalls.map(c => c.url)));
  check("产物图：请求带上了 Authorization",
    !!(fetchCalls[0] && fetchCalls[0].opts.headers
       && fetchCalls[0].opts.headers.Authorization === "Bearer T-UNIT"),
    JSON.stringify(fetchCalls[0] && fetchCalls[0].opts.headers));

  const again = await artifactBlobUrl("图表.png");
  check("产物图：同名复用，不重复下载", again === url && fetchCalls.length === 1);

  const missing = await artifactBlobUrl("missing.png");
  check("产物图：404 给 null（图不显示，但绝不塞一个必然 401 的 src）", missing === null, String(missing));

  const callsBeforeRelease = fetchCalls.length;
  releaseArtifactUrls();
  const afterRelease = await artifactBlobUrl("图表.png");
  check("产物图：释放后重新取（清空消息区 revoke，避免 blob 泄漏）",
    fetchCalls.length === callsBeforeRelease + 1
    && typeof afterRelease === "string" && afterRelease.startsWith("blob:")
    && afterRelease !== url,
    String(afterRelease));

  fetchCalls.length = 0;
  const box = artifactsBlock(["图表.png"]);
  const img = box.children[0].children[0];
  await flush();
  check("产物列表：<img> 的 src 是 blob:（不是会 401 的 /api/artifact/…）",
    typeof img.src === "string" && img.src.startsWith("blob:"), String(img.src));
  check("产物列表：仍然保留文件名做 figcaption",
    box.children[0].children[1].textContent === "图表.png");
  check("答案里的内联图：占位标记 + 取回后填 blob",
    inline("![图](图表.png)").includes('data-artifact="图表.png"')
    && !inline("![图](图表.png)").includes("/api/artifact/"));

  console.log("");
  if (failures > 0) {
    console.log("前端渲染断言： " + failures + " 项失败");
    process.exit(1);
  }
  console.log("前端渲染断言全部通过");
})();

/* ---------------- 警告与报错必须分开显示 ---------------- */
/* 回归 2026-09-15：模型画的图只剩一句 matplotlib UserWarning（tight_layout 管不到
 * 手工定位的 Axes），数值与出图都正常，而旧 UI 把它标成「清洗后的 traceback」+
 * 红色错误样式 —— 连续三次被当成"又报错了"来排查。判据用内容 + 状态，不看有没有 stderr。 */
(() => {
  const { stderrLooksLikeError, renderStep } = exported;
  const WARN = "work\\script.py:164: UserWarning: This figure includes Axes that are not "
    + "compatible with tight_layout, so results might be incorrect.";
  const TRACE = "Traceback (most recent call last):\n  File \"script.py\", line 3\nKeyError: 地区";

  check("判据：OK + 只有警告 -> 不算报错", stderrLooksLikeError({ status: "OK", stderr: WARN }) === false);
  check("判据：OK + traceback -> 算报错", stderrLooksLikeError({ status: "OK", stderr: TRACE }) === true);
  check("判据：失败态一律算报错（哪怕 stderr 是空的）",
    stderrLooksLikeError({ status: "TIMEOUT", stderr: "" }) === true
    && stderrLooksLikeError({ status: "RUNTIME_ERROR", stderr: WARN }) === true);
  check("判据：工具调用（status=None、无 stderr）不算报错",
    stderrLooksLikeError({ status: null, stderr: "" }) === false);

  const warnText = treeText(renderStep({
    index: 0,
    calls: [{ name: "run_python", code: "print(1)", file_name: "" }],
    outcomes: [{ status: "OK", stdout: "done", stderr: WARN, hint: "", artifacts: [] }]
  }));
  check("渲染：警告走「警告，不是报错」标签", warnText.includes("警告，不是报错"), warnText.slice(0, 200));
  check("渲染：警告不套 traceback 标签", !warnText.includes("清洗后的 traceback"));
  check("渲染：警告用 warn 配色（不再用 err 红色）",
    warnText.includes("log warn") && !warnText.includes("log err"));

  const errText = treeText(renderStep({
    index: 1,
    calls: [{ name: "run_python", code: "print(1)", file_name: "" }],
    outcomes: [{ status: "RUNTIME_ERROR", stdout: "", stderr: TRACE, hint: "先看列名", artifacts: [] }]
  }));
  check("渲染：真报错仍走 traceback 标签 + err 配色",
    errText.includes("清洗后的 traceback") && errText.includes("log err")
    && !errText.includes("警告，不是报错"), errText.slice(0, 200));
})();
