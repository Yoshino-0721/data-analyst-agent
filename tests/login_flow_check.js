/* 登录页「强制改密」流程的**行为**断言。
 *
 * 为什么不用浏览器：本机的 Edge 在会话中运行时，CLI 的 --dump-dom 不输出任何东西
 * （根因见 AGENTS.md §4.3 的记录）。而"被强制改密时到底放不放行""改密后有没有换
 * token"恰恰是前端最该被守住的两件事，所以在 Node 里用 DOM 替身**驱动真实脚本
 * 求值** —— 与项目二 tests/ui_render_check.js 同一思路。
 *
 * 用法：node tests/login_flow_check.js web/login.html
 * 输出：每项断言一行；失败时退出码非 0。
 */
"use strict";

const fs = require("fs");

const htmlPath = process.argv[2];
const html = fs.readFileSync(htmlPath, "utf-8");
const match = html.match(/<script>([\s\S]*?)<\/script>/);
if (!match) {
  console.error("FAIL: 页面里没有找到 <script> 块");
  process.exit(1);
}
const script = match[1];

/* ---------------- DOM 替身：只实现脚本真正用到的那几样 ---------------- */

const elements = {};

function makeEl(id) {
  const classes = new Set();
  return {
    id: id,
    value: "",
    textContent: "",
    disabled: false,
    required: false,
    style: {},
    dataset: {},
    _classes: classes,
    classList: {
      add: function () { for (let i = 0; i < arguments.length; i += 1) classes.add(arguments[i]); },
      remove: function () { for (let i = 0; i < arguments.length; i += 1) classes.delete(arguments[i]); },
      toggle: function (name, force) {
        const on = force === undefined ? !classes.has(name) : !!force;
        if (on) classes.add(name); else classes.delete(name);
      },
      contains: function (name) { return classes.has(name); },
    },
    addEventListener: function () {},
  };
}

function getEl(id) {
  if (!elements[id]) elements[id] = makeEl(id);
  return elements[id];
}

global.document = {
  getElementById: getEl,
  createElement: function () { return makeEl("created"); },
  querySelectorAll: function () { return []; },
  addEventListener: function () {},
};
global.window = global;
Object.defineProperty(global, "navigator", {
  value: { clipboard: { writeText: async function () {} } },
  configurable: true,
  writable: true,
});

const storage = {};
global.localStorage = {
  getItem: function (k) { return Object.prototype.hasOwnProperty.call(storage, k) ? storage[k] : null; },
  setItem: function (k, v) { storage[k] = String(v); },
  removeItem: function (k) { delete storage[k]; },
};

const navigations = [];
global.location = { replace: function (url) { navigations.push(url); }, href: "" };

/* fetch 替身：按 URL 派发到预设响应，并把调用（含请求头）记下来 */
let routes = {};
const calls = [];

global.fetch = async function (url, options) {
  calls.push({ url: url, options: options || {} });
  const handler = routes[url];
  if (!handler) throw new Error("未预设的请求：" + url);
  return handler(options || {});
};

function jsonResponse(body, ok) {
  return {
    ok: ok !== false,
    status: ok === false ? 400 : 200,
    json: async function () { return body; },
  };
}

/* ---------------- 求值真实脚本 ---------------- */

let api;
try {
  api = new Function(
    script +
      "\n;return { enter: enter, submitPasswordChange: submitPasswordChange,"
      + " setCurrentPassword: function (v) { knownCurrentPassword = v; } };"
  )();
} catch (err) {
  console.error("FAIL: 脚本求值失败 —— " + err.message);
  process.exit(1);
}

let failures = 0;
function check(name, cond, detail) {
  console.log((cond ? "  ok   " : "  FAIL ") + name + (cond ? "" : "  →  " + detail));
  if (!cond) failures += 1;
}

/* ---------------- 1. 被强制改密：不放行、露出改密屏 ---------------- */

navigations.length = 0;
api.enter({ token: "T1", user: { username: "newbie", role: "user", must_change_password: true } });

check("强制改密：不跳转（不能把人放进去）", navigations.length === 0, JSON.stringify(navigations));
check("强制改密：改密屏可见", !getEl("changePwdForm")._classes.has("hidden"));
check("强制改密：登录 / 注册表单被藏起来",
  getEl("loginForm")._classes.has("hidden") && getEl("registerForm")._classes.has("hidden"));
check("强制改密：标签栏被藏起来（界面上没有'跳过'这条路）", getEl("authTabs")._classes.has("hidden"));
check("强制改密：token 先存下来（改密要用它）", storage["auth_token"] === "T1", storage["auth_token"]);

/* ---------------- 2. 普通登录：照常按角色跳转 ---------------- */

navigations.length = 0;
api.enter({ token: "T2", user: { username: "alice", role: "user", must_change_password: false } });
check("普通用户登录：跳到 /", navigations[navigations.length - 1] === "/", JSON.stringify(navigations));

navigations.length = 0;
api.enter({ token: "T2b", user: { username: "boss", role: "admin", must_change_password: false } });
check("管理员登录：跳到 /admin", navigations[navigations.length - 1] === "/admin", JSON.stringify(navigations));

/* ---------------- 3/4. 改密成功与失败 ---------------- */

(async function () {
  /* 3. 成功：必须换用响应里新签发的 token */
  navigations.length = 0;
  calls.length = 0;
  storage["auth_token"] = "T1";
  // 真实流程里是 bindLogin 先把用户刚输入的口令记下来、再调 enter ——
  // 所以这里保持同样的顺序，否则会验错"要不要再问一遍当前口令"。
  api.setCurrentPassword("one-time-password");
  api.enter({ token: "T1", user: { username: "newbie", role: "user", must_change_password: true } });
  check("已知当前口令时：不再要求重填（字段被藏起来）",
    getEl("pwdCurrentLabel")._classes.has("hidden"));

  let changeBody = null;
  routes = {
    "/api/auth/change-password": function (options) {
      changeBody = JSON.parse(options.body);
      return jsonResponse({ ok: true, token: "T3" });
    },
    "/api/auth/me": function () {
      return jsonResponse({ username: "newbie", role: "user", must_change_password: false });
    },
  };
  getEl("pwdNew").value = "Fresh-Newbie-7z!";
  getEl("pwdConfirm").value = "Fresh-Newbie-7z!";

  await api.submitPasswordChange();

  check("改密：用的是登录时那串口令，没让用户再填一遍",
    !!changeBody && changeBody.old_password === "one-time-password", JSON.stringify(changeBody));
  check("改密：请求带上了旧的 Bearer token",
    calls.length > 0 && calls[0].options.headers.Authorization === "Bearer T1",
    JSON.stringify(calls.length ? calls[0].options.headers : null));
  check("改密：本地 token 换成响应里的**新** token（漏了这步下一个请求就 401）",
    storage["auth_token"] === "T3", storage["auth_token"]);
  check("改密成功：按角色跳转", navigations[navigations.length - 1] === "/", JSON.stringify(navigations));

  /* 4. 失败：不放行、把服务端的强度提示显示出来、不动 token */
  navigations.length = 0;
  storage["auth_token"] = "T1";
  routes["/api/auth/change-password"] = function () {
    return jsonResponse({ detail: "新密码不满足强度要求（长度不足 12 位）" }, false);
  };
  getEl("pwdNew").value = "12345678";
  getEl("pwdConfirm").value = "12345678";

  await api.submitPasswordChange();

  check("改密失败：不跳转", navigations.length === 0, JSON.stringify(navigations));
  check("改密失败：原样显示服务端的强度提示",
    getEl("error").textContent.indexOf("强度") >= 0, getEl("error").textContent);
  check("改密失败：token 没有被改掉", storage["auth_token"] === "T1", storage["auth_token"]);

  /* 5. 两次输入不一致：本地就该拦住，且不发请求 */
  calls.length = 0;
  getEl("pwdNew").value = "Fresh-Newbie-7z!";
  getEl("pwdConfirm").value = "Fresh-Newbie-8z!";
  await api.submitPasswordChange();
  check("两次新口令不一致：本地拦下且不发请求", calls.length === 0, JSON.stringify(calls.map(function (c) { return c.url; })));
  check("两次新口令不一致：给出可读提示",
    getEl("error").textContent.indexOf("不一致") >= 0, getEl("error").textContent);

  console.log("");
  if (failures > 0) {
    console.log("登录页流程断言：" + failures + " 项失败");
    process.exit(1);
  }
  console.log("登录页流程断言全部通过");
})();
