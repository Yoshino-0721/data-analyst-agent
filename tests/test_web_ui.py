"""前端约束测试。

前端是一堆内联的 HTML/CSS/JS，没有构建步骤、也没有框架兜底，
所以「它有没有悄悄腐化」只能靠测试守：

- 页面必须**自包含**（不引任何 CDN）—— 这个项目通篇在讲「执行环境要可控」，
  如果前端自己依赖一个外部脚本，故事就讲不圆了；
- 所有不可信文本必须走 escapeHtml —— 模型输出的代码会被贴进页面，
  漏一处转义就是一个 XSS；
- 六种执行状态必须都有对应的视觉呈现，否则「错误分类是地基」这句话
  在界面上就不成立。

纯字符串逻辑（高亮、Markdown 渲染）交给 Node 跑真实断言，
见 `tests/ui_render_check.js`。
"""

from __future__ import annotations

import re
import shutil
import subprocess
from html.parser import HTMLParser
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
WEB_FILE = ROOT / "web" / "index.html"
NODE_SCRIPT = Path(__file__).resolve().parent / "ui_render_check.js"

MANAGED_NODE = Path(
    r"C:\Users\YoshinoCiallo\.workbuddy\binaries\node\versions\22.22.2-3\node.exe"
)


@pytest.fixture(scope="module")
def html() -> str:
    assert WEB_FILE.is_file(), f"前端文件缺失：{WEB_FILE}"
    return WEB_FILE.read_text(encoding="utf-8")


def node_binary() -> str | None:
    found = shutil.which("node")
    if found:
        return found
    if MANAGED_NODE.is_file():
        return str(MANAGED_NODE)
    return None


# ------------------------------------------------------------------ 自包含


class TestSelfContained:
    def test_no_external_scripts(self, html):
        """引一个 CDN 脚本 = 页面不再自包含，也把一个外部依赖塞进了信任链。

        注意断言要落在**标签**上：代码注释里提到某个库的名字是无害的，
        真正危险的是 `<script src=...>`。
        """
        assert "<script src=" not in html.replace("'", '"')
        assert not re.search(r'<link[^>]+href\s*=\s*["\']https?://', html)
        assert not re.search(r'<script[^>]+src\s*=\s*["\']', html)

    def test_no_remote_assets(self, html):
        """不允许 src=/href= 指向外网（xmlns 之类的命名空间不算资源引用）。"""
        remote = re.findall(r'(?:src|href)\s*=\s*["\'](https?:)?//[^"\']+', html)
        assert remote == [], f"发现外部资源引用：{remote}"

    def test_single_file(self):
        """只有 index.html 一个前端文件 —— 没有构建步骤，clone 下来就能跑。"""
        files = sorted(p.name for p in (ROOT / "web").iterdir() if p.is_file())
        assert files == ["index.html"], files


# ------------------------------------------------------------------ 结构


class TestStructure:
    @pytest.mark.parametrize(
        "element_id",
        [
            "chat", "messages", "datasets", "trace", "trace-empty", "trace-count",
            "drop", "file-input", "question", "ask-btn", "ask-form", "reset-btn",
            "env-dot", "env-text",
        ],
    )
    def test_required_elements_exist(self, html, element_id):
        assert f'id="{element_id}"' in html, f"缺少元素 #{element_id}"

    def test_two_pane_layout(self, html):
        """左对话、右执行轨迹的分栏是这一版的核心信息架构。"""
        assert "<main>" in html
        assert "grid-template-columns" in html

    def test_responsive_breakpoint(self, html):
        assert "@media (max-width:960px)" in html

    def test_tags_are_balanced(self, html):
        checker = _BalanceChecker()
        checker.feed(html)
        assert checker.stack == [], f"标签未闭合：{checker.stack}"


class _BalanceChecker(HTMLParser):
    """检查标签闭合（只关心配对关系，不管语义）。"""

    VOID = {
        "area", "base", "br", "col", "embed", "hr", "img", "input",
        "link", "meta", "param", "source", "track", "wbr",
    }

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.stack: list[str] = []

    def handle_starttag(self, tag, attrs):
        if tag not in self.VOID:
            self.stack.append(tag)

    def handle_endtag(self, tag):
        if tag in self.VOID:
            return
        if self.stack and self.stack[-1] == tag:
            self.stack.pop()
        elif tag in self.stack:
            while self.stack and self.stack.pop() != tag:
                pass


# ------------------------------------------------------------------ 状态呈现


class TestStatusPresentation:
    @pytest.mark.parametrize(
        "status",
        ["OK", "RUNTIME_ERROR", "TIMEOUT", "OOM", "REJECTED", "SANDBOX_ERROR"],
    )
    def test_every_status_has_a_style(self, html, status):
        """六分类必须在界面上看得出区别 —— 否则分类的信息就丢在最后一公里了。"""
        assert f".badge.{status}" in html, f"缺少 {status} 的样式"

    def test_labels_are_human_readable(self, html):
        for text in ["执行成功", "代码报错", "执行超时", "内存超限", "预检拒绝"]:
            assert text in html, f"缺少状态说明文案：{text}"

    def test_local_tool_has_a_sane_label(self, html):
        """get_schema 不经过沙箱、没有 status，得有个说得通的标签。"""
        assert "已返回" in html

    def test_hint_is_displayed(self, html):
        """hint 是给模型的修正建议，但用户也该看得到 —— 它解释了「接下来会怎么改」。"""
        assert "hint" in html
        assert "hintbar" in html

    def test_stderr_panel_exists(self, html):
        assert "清洗后的 traceback" in html


# ------------------------------------------------------------------ 转义


class TestEscaping:
    def test_escape_html_is_defined(self, html):
        assert "function escapeHtml" in html

    def test_escape_covers_angle_brackets_and_quotes(self, html):
        for target in ["&amp;", "&lt;", "&gt;", "&quot;", "&#39;"]:
            assert target in html, f"escapeHtml 未覆盖 {target}"

    def test_model_code_goes_through_highlighter(self, html):
        """代码不是拼进 innerHTML 就完事 —— 必须先过高亮（内部会转义）。"""
        assert "highlightPython(call.code)" in html

    def test_answer_goes_through_markdown_renderer(self, html):
        assert "renderRich(" in html

    def test_status_values_are_escaped_in_class(self, html):
        """status 直接进 class 属性，是注入的经典入口。"""
        assert 'escapeHtml(key)' in html or "escapeHtml(status)" in html


# ------------------------------------------------------------------ 代码展示


class TestCodeTransparency:
    def test_code_is_shown_verbatim(self, html):
        """模型写的每一段代码都要原样展示 —— 这是用户信任的来源。"""
        assert "模型生成的 Python 代码" in html

    def test_copy_button_exists(self, html):
        assert "navigator.clipboard.writeText" in html

    def test_artifacts_render_as_images(self, html):
        """先跑通最简单的 img 方案，不被 UI 细节卡住。"""
        assert "/api/artifact/" in html
        assert "createElement(\"img\")" in html or "createElement('img')" in html


# ------------------------------------------------------------------ Node 断言


class TestRenderedBehaviour:
    def test_render_assertions_pass(self):
        """跑真实的 JS 渲染逻辑，验证高亮与 Markdown 的输出与转义。"""
        binary = node_binary()
        if binary is None:
            pytest.skip("本机没有可用的 node，跳过前端渲染断言")

        result = subprocess.run(
            [binary, str(NODE_SCRIPT), str(WEB_FILE)],
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=60,
            check=False,
        )
        assert result.returncode == 0, (
            f"前端渲染断言失败：\n{result.stdout}\n{result.stderr}"
        )
