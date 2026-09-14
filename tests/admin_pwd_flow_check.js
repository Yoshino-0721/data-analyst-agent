/* 管理后台「强制改密闸门 + 新建账号」的**行为**断言（与 login / index 的两份同思路）。
 *
 * 用法：node tests/admin_pwd_flow_check.js web/admin.html
 * 输出：每项断言一行；失败时退出码非 0。
 *
 * 为什么值得单独一份：后台是权限最高的页面，而它有两个"看起来对、实际会漏"的地方 ——
 * ① 被闸住的管理员如果照常去拉 /api/admin/*，拿到的是一串 403，界面变成空壳；
 * ② 建号返回的一次性口令如果不落在页面上（比如只弹个 toast），它就在库里再也拿不回来
 *    （库里只有 bcrypt 哈希），用户只能去点重置。
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

/* ---------------- DOM 替身 ---------------- */

const elements = {};

function makeEl(id) {
  const classes = new Set();
  const listeners = {};
  const node = {
    id: id,
    value: "",
    textContent: "",
    innerHTML: "",
    disabled: false,
    hidden: false,
    style: {},
    dataset: {},
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
    // 真实 DOM 里 .click() 会派发事件 —— 这里照做，否则"回车等同点按钮"验不到
    click: function () { fire(id, "click", { type: "click", target: node }); },
    appendChild: function (child) { return child; },
    removeChild: function () {},
    remove: function () {},
    focus: function () {},
    querySelector: function () { return null; },
    querySelectorAll: function () { return []; },
    setAttribute: function () {},
    closest: function () { return null; },
  };
  return node;
}

function getEl(id) {
  if (!elements[id]) elements[id] = makeEl(id);
  return elements[id];
}

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
global.confirm = function () { return true; };

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
      "\n;return { boot: boot, bind: bind, openPwdDialog: openPwdDialog,"
      + " closePwdDialog: closePwdDialog, forcePasswordChange: forcePasswordChange,"
      + " savePassword: savePassword, createUser: createUser };"
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
const urls = function () { return calls.map(function (c) { return c.url; }); };

(async function () {
  api.bind();

  /* ------- 0. 主动改密：事件对象不能被当成 forced（真实回归过） ------- */
  fire("changePwdBtn", "click", { type: "click" });
  check("主动改密：标题是「修改密码」，不是强制那套文案",
    getEl("pwdTitle").textContent === "修改密码", getEl("pwdTitle").textContent);
  check("主动改密：关闭按钮可见（事件对象没被当成 forced 传进去）",
    getEl("pwdClose").hidden === false, "关闭按钮是隐藏的");
  api.closePwdDialog();
  check("主动改密：点关闭能真的关掉", !pwdOpen(), "弹层还开着");

  /* ------- 1. 强制模式：关不掉 ------- */
  api.openPwdDialog(true);
  check("强制模式：标题变成「必须先修改密码」",
    getEl("pwdTitle").textContent === "必须先修改密码", getEl("pwdTitle").textContent);
  check("强制模式：关闭按钮被**隐藏**（不是禁用）", getEl("pwdClose").hidden === true, "关闭按钮还露着");
  check("强制模式：给出口令强度提示（只提示，判定在服务端）",
    getEl("pwdNotice").textContent.indexOf("服务端要求") >= 0, getEl("pwdNotice").textContent);
  check("强制模式：弹层是打开的", pwdOpen(), "没打开");
  api.closePwdDialog();
  check("强制模式：关闭动作是空操作（服务端不放行，前端也不假装能跳过）", pwdOpen(), "居然关掉了");

  /* ------- 2. 建号：本地先拦空输入，不发请求 ------- */
  calls.length = 0;
  getEl("newUserName").value = "";
  getEl("newUserEmail").value = "";
  await api.createUser();
  check("建号：用户名 / 邮箱为空时本地就拦下，不发请求", calls.length === 0, JSON.stringify(urls()));
  check("建号：给出可读提示",
    getEl("toast").textContent.indexOf("都要填") >= 0, getEl("toast").textContent);

  /* ------- 3. 建号失败：口令区块不能被露出来 ------- */
  routes["/api/admin/users"] = function () { return bad(400, "用户名已被占用"); };
  getEl("newUserName").value = "alice";
  getEl("newUserEmail").value = "alice@example.com";
  getEl("newUserRole").value = "user";
  getEl("newUserResult").classList.add("hidden");
  await api.createUser();
  check("建号失败：提示是错误样式",
    getEl("toast")._classes.has("error"), "toast 没有 error 类");
  check("建号失败：一次性口令区块保持隐藏（没有口令可给）",
    getEl("newUserResult")._classes.has("hidden"), "区块被露出来了");

  /* ------- 4. 建号成功：口令必须落在页面上，并说清只出现一次 ------- */
  const ONE_TIME = "One-Time-Pw-123";
  let createBody = null;
  let createMethod = null;
  routes["/api/admin/users"] = function (options) {
    createBody = JSON.parse(options.body);
    createMethod = options.method;
    return ok({ user: { id: 7, username: "bob", email: "bob@example.com", role: "admin" }, password: ONE_TIME });
  };
  getEl("newUserName").value = "bob";
  getEl("newUserEmail").value = "bob@example.com";
  getEl("newUserRole").value = "admin";
  calls.length = 0;
  await api.createUser();

  const reveal = getEl("newUserResult");
  check("建号：请求打到 POST /api/admin/users",
    calls.length > 0 && calls[0].url === "/api/admin/users" && createMethod === "POST",
    JSON.stringify(calls.length ? [calls[0].url, createMethod] : []));
  check("建号：把用户名 / 邮箱 / 角色一并提交",
    !!createBody && createBody.username === "bob" && createBody.email === "bob@example.com"
      && createBody.role === "admin", JSON.stringify(createBody));
  check("建号成功：一次性口令**显示在页面上**（只弹 toast 就再也拿不回来了）",
    reveal.innerHTML.indexOf(ONE_TIME) >= 0, reveal.innerHTML);
  check("建号成功：口令区块是露出来的", !reveal._classes.has("hidden"), "还是隐藏的");
  check("建号成功：明说「只显示这一次」",
    reveal.innerHTML.indexOf("只显示这一次") >= 0, reveal.innerHTML);
  check("建号成功：提醒新账号首登必须改密（服务端也会 403 兜住）",
    reveal.innerHTML.indexOf("首次登录") >= 0, reveal.innerHTML);
  check("建号成功：表单被清空（免得手滑再建一个同名账号）",
    getEl("newUserName").value === "" && getEl("newUserEmail").value === ""
      && getEl("newUserRole").value === "user",
    JSON.stringify([getEl("newUserName").value, getEl("newUserEmail").value, getEl("newUserRole").value]));
  check("建号成功：顺手刷新用户列表（新账号要看得见）",
    urls().indexOf("/api/admin/users") >= 0, JSON.stringify(urls()));

  /* ------- 5. 闸门真流程：改密前不拉后台数据，改完才放行 ------- */
  routes = {
    "/api/auth/me": function () {
      return ok({ id: 1, username: "boss", role: "admin", must_change_password: true });
    },
    "/api/auth/change-password": function () { return ok({ ok: true, token: "T-NEW" }); },
    "/api/admin/users": function () { return ok({ users: [] }); },
    "/api/admin/stats": function () { return ok({ users: 0, sessions: 0, recent: [] }); },
  };
  storage["auth_token"] = "T-OLD";
  calls.length = 0;
  navigations.length = 0;
  let bootingSettled = false;
  const booting = api.boot().then(function () { bootingSettled = true; });
  await tick();

  const before = urls();
  check("被闸住时：确实校验了身份（证明流程真的跑起来了）",
    before.indexOf("/api/auth/me") >= 0, JSON.stringify(before));
  check("被闸住时：没有去拉任何后台数据（否则只会拿到一串 403）",
    before.filter(function (u) { return u.indexOf("/api/admin/") === 0; }).length === 0,
    JSON.stringify(before));
  check("被闸住时：没有把人赶回登录页（他是登录着的管理员）",
    navigations.length === 0, JSON.stringify(navigations));
  check("被闸住时：boot 还没结束（没把他放进空壳后台）", bootingSettled === false, "boot 已结束");
  check("被闸住时：改密框自己弹出来了", pwdOpen(), "没弹");
  check("被闸住时：改密框关不掉", getEl("pwdClose").hidden === true, "关闭按钮还露着");

  /* ------- 5b. 闸门里改密失败：留在框里、给出原因、不放行 ------- */
  routes["/api/auth/change-password"] = function () {
    return bad(400, "新密码不满足强度要求（长度不足 12 位）");
  };
  getEl("oldPwdInput").value = "one-time-password";
  getEl("newPwdInput").value = "12345678";
  await api.savePassword();
  await tick();
  check("闸门里改密失败：不放行（boot 还在等）", bootingSettled === false, "被放行了");
  check("闸门里改密失败：弹层还开着（人还留在闸门里）", pwdOpen(), "被关掉了");
  check("闸门里改密失败：把服务端的原因显示在框里（只弹 toast 会被弹层挡住）",
    getEl("pwdNotice").textContent.indexOf("强度") >= 0, getEl("pwdNotice").textContent);
  check("闸门里改密失败：提示是错误样式", getEl("pwdNotice")._classes.has("err"), "没有 err 类");
  check("闸门里改密失败：本地 token 没有被改掉", storage["auth_token"] === "T-OLD", storage["auth_token"]);

  /* ------- 5c. 改密成功：换 token、放行 boot ------- */
  routes["/api/auth/change-password"] = function () { return ok({ ok: true, token: "T-NEW" }); };

  getEl("oldPwdInput").value = "one-time-password";
  getEl("newPwdInput").value = "Fresh-Boss-9x!";
  await api.savePassword();
  check("改密成功：本地 token 换成响应里**新签发**的那个（漏了这步下个请求就 401）",
    storage["auth_token"] === "T-NEW", storage["auth_token"]);
  check("改密成功：弹层关掉了", !pwdOpen(), "还开着");
  check("改密成功：角标按新身份刷新", getEl("userBadge").innerHTML.indexOf("boss") >= 0,
    getEl("userBadge").innerHTML);

  await booting;
  await tick();
  const after = urls();
  check("改密成功后：boot 继续跑，后台数据这才被加载",
    after.indexOf("/api/admin/users") >= 0 && after.indexOf("/api/admin/stats") >= 0
      && bootingSettled === true, JSON.stringify(after));
  check("改密成功后：后台外壳露出来了", !getEl("app")._classes.has("hidden"), "app 还藏着");

  /* ------- 6. 非管理员：给明确的无权限页，不弹改密框 ------- */
  routes["/api/auth/me"] = function () {
    return ok({ id: 2, username: "alice", role: "user", must_change_password: false });
  };
  storage["auth_token"] = "T-OLD";
  calls.length = 0;
  navigations.length = 0;
  api.closePwdDialog(); // 先关掉上一段留下的弹层，免得起点的状态混淆结论
  getEl("pwdOverlay")._classes.add("hidden");
  await api.boot();
  check("非管理员：看到无权限提示", getEl("denied").style.display === "block",
    String(getEl("denied").style.display));
  check("非管理员：后台外壳藏起来", getEl("app")._classes.has("hidden"), "app 还露着");
  check("非管理员：不弹改密框（本页不是他的工位，改密由工作台接管）", !pwdOpen(), "弹了");
  check("非管理员：不去拉后台数据",
    urls().filter(function (u) { return u.indexOf("/api/admin/") === 0; }).length === 0,
    JSON.stringify(urls()));
  check("非管理员：不把人赶回登录页（他是登录着的普通用户）",
    navigations.length === 0, JSON.stringify(navigations));

  console.log("");
  if (failures > 0) {
    console.log("管理后台改密 / 建号断言：" + failures + " 项失败");
    process.exit(1);
  }
  console.log("管理后台改密 / 建号断言全部通过");
  process.exit(0);
})();
