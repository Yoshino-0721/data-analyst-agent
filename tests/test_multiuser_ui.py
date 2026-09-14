"""多用户前端护栏：登录页、管理后台页与工作台的新契约。

与 ``test_web_ui.py`` 的分工：
- ``test_web_ui.py`` 守的是 index.html 里**既有的**能力（三栏布局、六种执行状态徽章、
  代码高亮与 Markdown 转义、产物图、Node 真实渲染断言……）—— 那些一条都不能少，
  本文件不去重复它们；
- 本文件守的是**这次多用户改造新加的东西**：三个页面各自自包含、登录页与后台页的
  结构、以及工作台的两处**集成改动**（带 Bearer 的统一请求封装、切会话时从
  ``meta.steps`` 重建执行轨迹）。

两处集成改动是重点，它们都是"后端改了、前端少改一处就静默坏掉"的典型：
1. ``/api/health`` 已经不再返回用户自己的 ``files``（那是公开接口泄露用户数据），
   数据集列表必须改读带登录态的 ``/api/workspace``；
2. 切换会话时要把 assistant 消息 ``meta`` 里的 ``steps`` 重新画成轨迹卡片，
   且必须复用既有的 ``renderTrace`` —— 另写一套渲染会让"历史回放"与"刚跑完"
   长得不一样。
"""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

import pytest

from src.server import PROJECT_ROOT

PAGES = {
    "index": PROJECT_ROOT / "web" / "index.html",
    "login": PROJECT_ROOT / "web" / "login.html",
    "admin": PROJECT_ROOT / "web" / "admin.html",
}


def read_page(name: str) -> str:
    path = PAGES[name]
    assert path.is_file(), f"缺少前端文件：{path}"
    return path.read_text(encoding="utf-8")


# ---------------------------------------------------------------- 三个页面共同约束


@pytest.mark.parametrize("name", sorted(PAGES))
def test_page_exists(name):
    read_page(name)


@pytest.mark.parametrize("name", sorted(PAGES))
def test_page_is_self_contained(name):
    """零 CDN：不引任何外部脚本 / 样式。这个项目通篇在讲「执行环境要可控」，
    前端自己依赖一个外部脚本，故事就讲不圆了。"""
    html = read_page(name)

    assert "<script src=" not in html
    assert not re.search(r"<script[^>]+src\s*=", html)
    assert not re.search(r'<link[^>]+href\s*=\s*["\']https?://', html)
    remote = re.findall(r'(?:src|href)\s*=\s*["\'](?:https?:)?//[^"\']+', html)
    assert remote == [], f"{name}.html 出现外部资源引用：{remote}"


@pytest.mark.parametrize("name", sorted(PAGES))
def test_page_has_mobile_viewport(name):
    html = read_page(name)
    assert 'name="viewport"' in html
    assert "width=device-width" in html


@pytest.mark.parametrize("name", ["index", "admin"])
def test_page_escapes_untrusted_text(name):
    """把不可信文本拼进 HTML 的页面必须有 escapeHtml。

    index 贴的是模型写的代码与结论；admin 贴的是**别人的**数据集名与提问正文 ——
    admin 漏一处转义就是跨用户 XSS。登录页刻意不在名单里：它只用 textContent
    写错误提示，不拼 HTML，给它加一个用不到的 escapeHtml 反而是死代码。
    """
    html = read_page(name)
    assert "function escapeHtml" in html
    for token in ("&amp;", "&lt;", "&gt;", "&quot;", "&#39;"):
        assert token in html, f"{name}.html 的 escapeHtml 未覆盖 {token}"


@pytest.mark.parametrize("name", sorted(PAGES))
def test_boot_is_not_executed_at_top_level(name):
    """启动逻辑必须挂在 DOMContentLoaded 上，顶层不得有副作用。

    这条不是洁癖：``tests/ui_render_check.js`` 会用
    ``new Function(脚本 + ";return { escapeHtml, ... }")`` 求值**整个 <script>**，
    而它只 stub 了 ``document`` / ``window`` / ``navigator`` / ``fetch``，
    **没有 ``localStorage`` / ``location``**。顶层碰它们会抛 ReferenceError，
    把那条真实渲染断言打崩。
    """
    html = read_page(name)
    assert "DOMContentLoaded" in html


def test_render_helpers_stay_top_level_for_node_check():
    """ui_render_check.js 要在函数作用域末尾能取到这四个符号，缺一个就求值失败。"""
    html = read_page("index")
    for symbol in ("escapeHtml", "highlightPython", "renderRich", "STATUS_LABEL"):
        assert re.search(rf"^\s*(?:function|const)\s+{symbol}\b", html, re.M), (
            f"{symbol} 不再是顶层定义，Node 渲染断言会失败"
        )


# ---------------------------------------------------------------- 登录页


def test_login_page_has_login_and_register_tabs():
    html = read_page("login")

    for element_id in ("loginForm", "registerForm", "loginAccount", "loginPassword",
                       "regUsername", "regEmail", "regPassword", "loginBtn", "regBtn"):
        assert f'id="{element_id}"' in html, f"登录页缺少元素 #{element_id}"

    assert "登录" in html and "注册" in html


def test_login_page_uses_the_auth_contract():
    """必须与 /api/auth/* 的真实契约对齐，否则页面根本登录不了。"""
    html = read_page("login")

    assert "/api/auth/login" in html
    assert "/api/auth/register" in html
    assert "auth_token" in html
    assert "Authorization" in html and "Bearer" in html
    assert "'/admin'" in html or '"/admin"' in html
    assert "role" in html


def test_login_page_handles_forced_password_change():
    """被强制改密的账号在登录页就要被按在改密上（模块 6.5 / 9 的前端一半）。

    服务端另有硬闸门（改密前除 /api/auth/me 与改密接口外一律 403），所以前端
    要做的是两件事，这里各断言一件：

    1. **不假装有"跳过"这条路** —— 改密屏有输入框与提交按钮，且标签栏会被隐藏；
    2. **改密后换用响应里新签发的 token** —— 漏了这步，改密成功后的下一个请求
       就是 401，用户会以为"改密把账号弄坏了"。
    """
    html = read_page("login")

    for element_id in ("changePwdForm", "pwdCurrent", "pwdNew", "pwdConfirm",
                       "pwdBtn", "authTabs"):
        assert f'id="{element_id}"' in html, f"登录页缺少元素 #{element_id}"

    assert "must_change_password" in html
    assert "/api/auth/change-password" in html
    assert "data.token" in html

    # 之前那版页面写着"默认管理员 admin / admin123" —— 默认口令早就取消了，
    # 这句话不能再回来（它会把用户引到一个根本不存在的口令上）。
    assert "admin123" not in html


# ---------------------------------------------------------------- 工作台


def test_index_gates_on_token_and_redirects_to_login():
    html = read_page("index")

    assert "auth_token" in html
    assert "'/login'" in html or '"/login"' in html
    assert "/api/auth/me" in html


def test_index_uses_bearer_header_for_all_requests():
    """四个旧调用点（health / upload / ask / reset）都要走统一的带鉴权封装，
    不能再留裸 fetch —— 漏一个就是一处偶发 401。"""
    html = read_page("index")

    assert "Authorization" in html
    assert "Bearer" in html
    assert "apiFetch" in html or "authFetch" in html


def test_index_gets_datasets_from_the_authenticated_endpoint():
    """``/api/health`` 已经不返回用户文件了，数据集列表必须改读 /api/workspace。

    这是本次改造最容易漏的一处：页面不会报错，只是数据集列表永远是空的。
    """
    html = read_page("index")

    assert "/api/workspace" in html
    assert "datasets" in html          # 既有的数据集列表面板 id 不能丢


def test_index_has_session_sidebar():
    html = read_page("index")

    assert "/api/sessions" in html
    for element_id in ("sessionList", "newSessionBtn"):
        assert f'id="{element_id}"' in html, f"工作台缺少元素 #{element_id}"
    assert "/messages" in html


def test_index_sends_session_id_instead_of_client_history():
    html = read_page("index")

    assert "session_id" in html
    assert "history: conversation" not in html


def test_index_restores_trace_from_message_meta():
    """切会话时必须从 assistant 消息的 meta.steps 重建轨迹，并复用既有渲染函数。"""
    html = read_page("index")

    assert "meta" in html
    assert "renderTrace" in html
    assert "renderStep" in html
    assert "steps" in html
    # 产物也要能从历史里恢复出来
    assert "/api/artifact/" in html


def test_index_keeps_its_existing_element_ids():
    """三栏布局与轨迹面板的既有 id 是 JS 的挂载点，删任何一个都会让功能静默失效。"""
    html = read_page("index")

    for element_id in ("chat", "messages", "datasets", "trace", "trace-empty",
                       "trace-count", "drop", "file-input", "question",
                       "ask-btn", "ask-form", "reset-btn", "env-dot", "env-text"):
        assert f'id="{element_id}"' in html, f"工作台丢了既有元素 #{element_id}"


def test_index_has_account_actions():
    html = read_page("index")

    assert "changePwd" in html or "change-password" in html
    assert "logout" in html.lower()


# ---------------------------------------------------------------- 管理后台


def test_admin_page_has_all_five_tabs():
    html = read_page("admin")

    for tab in ("stats", "users", "sessions", "datasets", "system"):
        assert f'data-tab="{tab}"' in html, f"管理后台缺少 {tab} 面板"
        assert f'id="panel-{tab}"' in html, f"管理后台缺少 panel-{tab} 容器"

    for label in ("统计面板", "用户管理", "会话管理", "数据集管理", "系统操作"):
        assert label in html, f"管理后台缺少标签文案：{label}"


def test_admin_page_calls_the_real_admin_endpoints():
    """逐条对齐 /api/admin/* 的真实路由，别写出一个调不到数据的后台。"""
    html = read_page("admin")

    for path in (
        "/api/admin/stats",
        "/api/admin/users",
        "/api/admin/sessions",
        "/api/admin/datasets",
        "/api/admin/system/health",
        "/api/admin/system/reset-caches",
    ):
        assert path in html, f"管理后台没有调用 {path}"

    assert "/reset-password" in html


def test_admin_page_renders_execution_trace_from_meta():
    """管理员看会话记录时，assistant 的 meta.steps 要展开成执行轨迹 ——
    这正是"轨迹存 meta 而不是单独建表"的用户可见价值。"""
    html = read_page("admin")

    assert "steps" in html
    assert "meta" in html


def test_admin_page_shows_denied_state_for_normal_users():
    """普通用户打开 /admin 要看到明确的无权限提示，而不是一个空壳界面。
    注意：这只是体验；真正的闸门是服务端的 require_admin（API 一律 403）。"""
    html = read_page("admin")

    assert "权限" in html
    assert "/api/auth/me" in html


def test_admin_page_has_no_execution_entry():
    """后台只是只读审计入口：能看任意用户的记录与轨迹，但**不提供任何执行入口**。
    管理能力不该顺带变成"以别人身份跑代码"的能力。"""
    html = read_page("admin")

    assert "/api/ask" not in html
    assert "/api/upload" not in html


def test_admin_page_has_forced_password_change_gate():
    """后台同样要认 must_change_password：管理员也会被建号流程闸住。

    行为层面由 `admin_pwd_flow_check.js` 真跑一遍；这里守静态契约。
    """
    html = read_page("admin")

    assert 'id="pwdTitle"' in html
    assert 'id="pwdSub"' in html
    assert 'id="pwdNotice"' in html
    assert "function forcePasswordChange" in html
    assert "if (pwdForced) return;" in html
    assert "openPwdDialog(false)" in html
    assert "await forcePasswordChange()" in html


def test_admin_page_builds_accounts_with_one_time_password():
    """建号入口：表单 -> POST /api/admin/users，且**一次性口令必须落在页面上**。

    服务端只存 bcrypt 哈希，口令一旦没显示出来就再也拿不回（只能再去点重置）——
    所以这条断言盯的是"口令有没有真的出现在界面里 + 有没有说清只出现这一次"，
    而不是"有没有调这个接口"。
    """
    html = read_page("admin")

    assert 'id="newUserName"' in html
    assert 'id="newUserEmail"' in html
    assert 'id="newUserRole"' in html
    assert 'id="newUserBtn"' in html
    assert 'id="newUserResult"' in html
    assert "api('/api/admin/users'" in html
    assert "只显示这一次" in html


def test_index_has_forced_password_change_gate():
    """首页的强制改密闸门：元素、文案位、以及"关不掉"那一行守卫都得在。

    行为层面由 `index_pwd_flow_check.js` 真跑一遍；这里守的是静态契约 ——
    id 被改名 / 守卫那行被顺手删掉时，测试要立刻红，而不是等用户点一下遮罩发现。
    """
    html = read_page("index")

    assert 'id="pwdTitle"' in html
    assert 'id="pwdSub"' in html
    assert 'id="pwdNotice"' in html
    assert "function forcePasswordChange" in html
    assert "if(pwdForced){ return; }" in html    # 取消 / 点遮罩共用的守卫
    assert "openPwd(false)" in html              # 主动改密：不能被点击事件对象污染成强制模式
    assert "await forcePasswordChange()" in html  # 改完才继续加载业务数据


# ---------------------------------------------------------------- 行为断言（Node）

MANAGED_NODE = Path(
    r"C:\Users\YoshinoCiallo\.workbuddy\binaries\node\versions\22.22.2-3\node.exe"
)


def _node_binary():
    found = shutil.which("node")
    if found:
        return found
    return str(MANAGED_NODE) if MANAGED_NODE.is_file() else None


def test_login_flow_behaviour_assertions_pass():
    """用 Node 驱动登录页的**真实脚本**，验证强制改密流程的行为。

    静态断言只能证明"页面里有这些元素"，证明不了"到底放不放行、改密后有没有换
    token" —— 而这两件恰恰是前端最该守住的地方。浏览器在本机不可用
    （Edge 在会话中运行时 CLI 不输出 DOM），所以用 Node + DOM 替身求值真实脚本，
    与项目二 `ui_render_check.js` 同一思路。
    """
    binary = _node_binary()
    if binary is None:
        pytest.skip("本机没有可用的 node，跳过登录页流程断言")

    script = Path(__file__).resolve().parent / "login_flow_check.js"
    result = subprocess.run(
        [binary, str(script), str(PAGES["login"])],
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=60,
        check=False,
    )
    assert result.returncode == 0, f"登录页流程断言失败：\n{result.stdout}\n{result.stderr}"


def test_index_pwd_flow_behaviour_assertions_pass():
    """用 Node 驱动首页的**真实脚本**，验证"被闸住 → 改密 → 才放行"这条闸门。

    `must_change_password=true` 的账号在改密前，服务端除身份与改密接口外一律 403；
    首页必须先把人按在关不掉的改密框上，改完换用新 token 再继续加载业务数据。
    这条链路跨了「按钮绑定 / 关闭守卫 / token 轮换 / boot 放行」四件事，
    静态断言一条都证明不了 —— 守卫那行删掉，页面照样"看起来是对的"。
    """
    binary = _node_binary()
    if binary is None:
        pytest.skip("本机没有可用的 node，跳过首页流程断言")

    script = Path(__file__).resolve().parent / "index_pwd_flow_check.js"
    result = subprocess.run(
        [binary, str(script), str(PAGES["index"])],
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=60,
        check=False,
    )
    assert result.returncode == 0, f"首页流程断言失败：\n{result.stdout}\n{result.stderr}"


def test_admin_pwd_flow_behaviour_assertions_pass():
    """用 Node 驱动后台的**真实脚本**，验证闸门与建号两件事的行为。

    后台是权限最高的页面，而它有两个"看起来对、实际会漏"的地方：被闸住的管理员
    如果照常去拉 /api/admin/*（只会拿到一串 403），界面就成了空壳；建号返回的一次性
    口令如果不落在页面上，它在库里就再也拿不回来。两条都只有真跑一遍才守得住。
    """
    binary = _node_binary()
    if binary is None:
        pytest.skip("本机没有可用的 node，跳过后台流程断言")

    script = Path(__file__).resolve().parent / "admin_pwd_flow_check.js"
    result = subprocess.run(
        [binary, str(script), str(PAGES["admin"])],
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=60,
        check=False,
    )
    assert result.returncode == 0, f"后台流程断言失败：\n{result.stdout}\n{result.stderr}"
