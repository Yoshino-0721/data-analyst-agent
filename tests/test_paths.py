"""路径安全：归一化与落点校验（C2 复核清单 3.1）。

这是全仓**唯一**的路径防线实现（项目二沙箱层从 `src.paths` 再导出），
所以这里既测纯函数行为，也守住"越界一律拒绝"这条底线。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from src.paths import safe_join, safe_target_name


class TestSafeTargetName:
    @pytest.mark.parametrize(
        "hostile",
        [
            "../../etc/passwd",
            "..\\..\\windows\\system32\\cfg",
            "/etc/shadow",
            "a/b/c.txt",
            "..",
            ".",
            "",
            "   ",
        ],
    )
    def test_result_is_always_a_bare_filename(self, hostile):
        """无论传进来什么，结果都必须是**纯文件名** —— 否则拼接就能跑出目录。"""
        result = safe_target_name(Path(hostile))

        assert "/" not in result and "\\" not in result
        assert result not in {"", ".", ".."}

    def test_normal_name_is_kept(self):
        assert safe_target_name(Path("销售数据.csv")) == "销售数据.csv"

    def test_separators_inside_basename_are_replaced(self):
        """极端输入：basename 里还带分隔符。

        具体结果**依平台而异**：Windows 的 `Path.name` 按 `/` 和 `\\` 一起切分，
        所以这里得到 `me.txt`；POSIX 只按 `/` 切分，会保留反斜杠、再由 `.replace`
        那一层清成 `na_me.txt`。所以**不去断言某个确定值**（那等于假设平台），
        只断言真正重要的事：结果里不可能再出现分隔符。
        """
        result = safe_target_name(Path("weird/na\\me.txt"))

        assert "/" not in result and "\\" not in result
        assert result.endswith(".txt")

    def test_surrounding_whitespace_is_stripped(self):
        assert safe_target_name(Path("  spaced.txt  ")) == "spaced.txt"


class TestSafeJoin:
    def test_normal_file_inside_root(self, tmp_path):
        (tmp_path / "a.txt").write_text("x", encoding="utf-8")

        assert safe_join(tmp_path, "a.txt") == (tmp_path / "a.txt").resolve()

    def test_subdirectory_is_allowed(self, tmp_path):
        """与 safe_target_name 的关键区别：rel_path 可能带子目录，不能压成 basename。"""
        (tmp_path / "nested").mkdir()
        (tmp_path / "nested" / "deep.py").write_text("x", encoding="utf-8")

        assert safe_join(tmp_path, "nested/deep.py") == (
            tmp_path / "nested" / "deep.py"
        ).resolve()

    @pytest.mark.parametrize(
        "hostile",
        ["../outside.txt", "../../etc/passwd", "nested/../../outside.txt", "a/../../../x"],
    )
    def test_traversal_is_refused(self, tmp_path, hostile):
        assert safe_join(tmp_path, hostile) is None

    def test_absolute_path_is_refused(self, tmp_path):
        """绝对路径会把 root 整个替换掉，必须拒绝。"""
        assert safe_join(tmp_path, "/etc/passwd") is None

    def test_missing_target_still_returns_a_path(self, tmp_path):
        """存在性由调用方判断；这里只保证落点合法。"""
        assert safe_join(tmp_path, "not-there.txt") == (tmp_path / "not-there.txt").resolve()


def test_sandbox_layer_reexports_the_same_implementation():
    """项目二沙箱层**不再自带实现**，而是再导出 `src.paths` 里的同一个函数对象。

    用 `is` 而不是"行为一致"来断言：它保证全仓只有**一处**实现 ——
    将来谁在沙箱层重新定义一个同名函数（哪怕只是"优化"了一下），这条立刻红。
    两份归一化规则分叉的后果是"一边拦得住、一边拦不住"，属于最难发现的漏洞。
    """
    from src.paths import safe_target_name as canonical
    from src.sandbox.analysis import safe_target_name as reexported

    assert reexported is canonical
