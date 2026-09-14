/* index 页「强制改密」闸门的**行为**断言（与 login_flow_check.js 同一思路）。
 *
 * 静态断言只能证明"页面里有 pwdTitle / 守卫那行代码"，证明不了"到底放不放行、
 * 改密后有没有换 token、被闸住时有没有偷偷去拉业务数据"。所以这里用 DOM 替身
 * **求值真实脚本**并驱动它跑一遍完整流程。
 *
 * 用法：node tests/index_pwd_flow_check.js web/index.html
 * 输出：每项断言一行；失败时退出码非 0。
 *
 * 注意（AGENTS.md §4.3）：本文件的判据按**本项目**的真实语义写 ——
 * 项目二用 `hidden` 类隐藏按钮、用 `hidden` 类表示弹层关闭（且改密框没有右上角
 * 关闭按钮），与姊妹项目（`element.hidden` / `open` 类）**不同**，不要照抄那边。
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
  const listeners = {};
  return {
    id: id,
    value: "",
    textContent: "",
    innerHTML: "",
    disabled: false,
    hidden: false,
    style: {},
    dataset: {},
    files: [],
    _classes: classes,
    _listeners: listeners,
    classList: {
      add: function () { for (let i = 0; i < arguments.length; i += 1) classes.add(arguments[i]); },
      remove: function () { for (let i = 0; i < arguments.length; i += 1) classes.delete(arguments[i]); },
      toggle: function (name, force) {
        const on = force === undefined ? !classes.has(name) : !!force;
        if (on) classes.add(name); else classes.delete(name);
      },
      contains: function (name) { return classes.has(name); },
    },
    addEventListener: function (type, fn) {
      (listeners[type] = listeners[type] || []).push(fn);
    },
    appendChild: function (child) { return child; },
    removeChild: function () {},
    remove: function () {},
    focus: function () {},
    click: function () {},
    querySelector: function () { return null; },
    querySelectorAll: function () { return []; },
    setAttribute: function () {},
    closest: function () { return null; },
  };
}

function getEl(id) {
  if (!elements[id]) elements[id] = makeEl(id);
  return elements[id];
}

/** 触发某个元素上的监听器 —— 用来验证"绑定到底传了什么进去"。 */
function fire(id, type, event) {
  const list = getEl(id)._listeners[type] || [];
  list.forEach(function (fn) { fn(event || {}); });
}

global.document = {
  getElementById: getEl,
  createElement: function () { return makeEl("created"); },
  querySelectorAll: function () { return []; },
  addEventListener: function () {},
  body: makeEl("body"),
};
global.window = global;
global.addEventListener = function () {};

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
global.FormData = function () { this.append = function () {}; };
global.confirm = function () { return false; };
global.URL = { createObjectURL: function () { return "blob:x"; }, revokeObjectURL: function () {} };

/* fetch 替身：按 URL 派发到预设响应，并把调用记下来 */
let routes = {};
const calls = [];

global.fetch = async function (url, options) {
  calls.push({ url: url, options: options || {} });
  const handler = routes[url];
  if (!handler) throw new Error("未预设的请求：" + url);
  return handler(options || {});
};

function ok(body) {
  return {
    ok: true,
    status: 200,
    json: async function () { return body; },
    text: async function () { return JSON.stringify(body); },
  };
}

function bad(status, detail) {
  const body = { detail: detail };
  return {
    ok: false,
    status: status,
    json: async function () { return body; },
    text: async function () { return JSON.stringify(body); },
  };
}

/* ---------------- 求值真实脚本 ---------------- */

let api;
try {
  api = new Function(
    script +
      "\n;return { boot: boot, bindEvents: bindEvents,"
      + " openPwd: openPwd, closePwd: closePwd,"
      + " forcePasswordChange: forcePasswordChange, savePassword: savePassword };"
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

const tick = function () { return new Promise(function (r) { setTimeout(r, 0); }); };
const pwdOpen = function () { return !getEl("pwdOverlay")._classes.has("hidden"); };
const cancelHidden = function () { return getEl("pwdCancelBtn")._classes.has("hidden"); };

(async function () {
  api.bindEvents();

  /* ------- 0. 主动改密：事件对象不能被当成 forced（真实回归过） ------- */
  fire("changePwdBtn", "click", { type: "click" });
  check("主动改密：取消按钮可见（事件对象没被当成 forced 传进去）",
    cancelHidden() === false, "取消按钮是隐藏的");
  check("主动改密：标题是「修改密码」，不是强制那套文案",
    getEl("pwdTitle").textContent === "修改密码", getEl("pwdTitle").textContent);
  api.closePwd();
  check("主动改密：点关闭能真的关掉", !pwdOpen(), "弹层还开着");

  /* ------- 1. 强制模式：关不掉 ------- */
  api.openPwd(true);
  check("强制模式：标题变成「必须先修改密码」",
    getEl("pwdTitle").textContent === "必须先修改密码", getEl("pwdTitle").textContent);
  check("强制模式：取消按钮被**隐藏**（不是禁用）", cancelHidden() === true, "取消按钮还露着");
  check("强制模式：给出口令强度提示（只提示，判定在服务端）",
    getEl("pwdNotice").textContent.indexOf("服务端要求") >= 0, getEl("pwdNotice").textContent);
  check("强制模式：弹层是打开的", pwdOpen(), "没打开");
  api.closePwd();
  check("强制模式：关闭动作是空操作（取消 / 点遮罩都走这里）", pwdOpen(), "居然关掉了");

  /* ------- 2. 改密失败：不放行、不动 token、保留服务端提示 ------- */
  storage["auth_token"] = "T1";
  let released = false;
  api.forcePasswordChange().then(function () { released = true; });
  routes = {
    "/api/auth/change-password": function () {
      return bad(400, "新密码不满足强度要求（长度不足 12 位）");
    },
  };
  getEl("oldPwdInput").value = "one-time-password";
  getEl("newPwdInput").value = "12345678";
  await api.savePassword();
  await tick();
  check("改密失败：不放行（等待者没有被 resolve）", released === false, "被放行了");
  check("改密失败：原样显示服务端的强度提示",
    getEl("pwdNotice").textContent.indexOf("强度") >= 0, getEl("pwdNotice").textContent);
  check("改密失败：提示是错误样式（不是信息色，别让用户把报错读成提示）",
    getEl("pwdNotice")._classes.has("err"), "提示没有 err 类");
  check("改密失败：本地 token 没有被改掉", storage["auth_token"] === "T1", storage["auth_token"]);
  check("改密失败：弹层还开着（人还留在闸门里）", pwdOpen(), "被关掉了");

  /* ------- 3. 真流程：被闸住时不拉业务数据，改完才放行 ------- */
  routes = {
    "/api/auth/me": function () {
      return ok({ username: "newbie", role: "user", must_change_password: true });
    },
    "/api/auth/change-password": function () { return ok({ ok: true, token: "T-NEW" }); },
    "/api/health": function () { return ok({ status: "ok" }); },
    "/api/workspace": function () { return ok({ files: [] }); },
    "/api/sessions": function () { return ok({ sessions: [] }); },
  };
  storage["auth_token"] = "T1";
  calls.length = 0;
  let bootingSettled = false;
  const booting = api.boot().then(function () { bootingSettled = true; });
  await tick();

  const urlsBefore = calls.map(function (c) { return c.url; });
  // 先确认"脚本真的跑了"：否则下面那条"没拉业务数据"会空过 —— 一个什么都没做的
  // boot 同样满足"没请求过 /api/sessions"，那是假绿。
  check("被闸住时：确实问了身份（证明流程真的跑起来了）",
    urlsBefore.indexOf("/api/auth/me") >= 0, JSON.stringify(urlsBefore));
  check("被闸住时：只校验了身份，没有偷偷去拉业务数据",
    urlsBefore.indexOf("/api/sessions") < 0 && urlsBefore.indexOf("/api/workspace") < 0,
    JSON.stringify(urlsBefore));
  check("被闸住时：boot 还没结束（没有把人放进空壳界面）", bootingSettled === false, "boot 已结束");
  check("被闸住时：改密框自己弹出来了", pwdOpen(), "没弹");

  getEl("oldPwdInput").value = "one-time-password";
  getEl("newPwdInput").value = "Fresh-Newbie-7z!";
  await api.savePassword();
  check("改密成功：本地 token 换成响应里**新签发**的那个（漏了这步下个请求就 401）",
    storage["auth_token"] === "T-NEW", storage["auth_token"]);
  check("改密成功：弹层关掉了", !pwdOpen(), "还开着");

  await booting;
  await tick();
  const urlsAfter = calls.map(function (c) { return c.url; });
  check("改密成功后：boot 继续跑，业务数据这才被加载",
    urlsAfter.indexOf("/api/sessions") >= 0 && bootingSettled === true, JSON.stringify(urlsAfter));

  console.log("");
  if (failures > 0) {
    console.log("index 页改密流程断言：" + failures + " 项失败");
    process.exit(1);
  }
  console.log("index 页改密流程断言全部通过");
  process.exit(0);
})();
