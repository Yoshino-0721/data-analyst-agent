"""文档守卫：README 里的测试数必须与真实用例数一致。

与项目一 `tests/test_docs.py` 同构（那边还额外守 README 的图片与部署文档路径，
本项目没有那些资产，所以只留这一条）—— 数字徽章是最容易悄悄过期的东西：
每加一条用例它就落后一次，而过期不会让任何测试变红。
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
README = ROOT / "README.md"

BODY_COUNT_PATTERNS = (
    r"运行测试（(\d+) 项",
)


# ---------------------------------------------------------------- 测试数漂移守卫

_DOCKER_MARK = r"pytestmark\s*=\s*pytest\.mark\.docker"


def _docker_only_test_files() -> set[str]:
    """模块级 `pytestmark = pytest.mark.docker` 的文件默认整文件被 deselect。

    这类文件不会出现在 `session.items` 里，做"是否只跑了子集"的判断时必须排除，
    否则每次常规运行都会被误判成子集而跳过守卫 —— 那样守卫等于不存在。
    """
    found = set()
    for path in (ROOT / "tests").glob("test_*.py"):
        if re.search(_DOCKER_MARK, path.read_text(encoding="utf-8")):
            found.add(path.name)
    return found


def test_readme_test_count_matches_reality(request):
    """README 里写的测试数必须与真实执行的用例数一致。

    为什么需要这条：数字徽章是会**漂移**的硬编码 —— 每加一条用例它就过期一次，
    而过期不会让任何测试变红，只会让读到它的人被误导（这个项目一路改下来，
    徽章已经悄悄落后过两次）。
    """
    on_disk = {p.name for p in (ROOT / "tests").glob("test_*.py")} - _docker_only_test_files()
    collected = {item.path.name for item in request.session.items}
    if on_disk - collected:
        pytest.skip(
            f"只跑了部分测试文件，跳过测试数守卫（未收集：{sorted(on_disk - collected)}）"
        )

    text = README.read_text(encoding="utf-8")
    badge = re.search(r"tests-(\d+)%20passed", text)
    assert badge, "README 徽章里找不到 tests-<N>%20passed"
    documented = int(badge.group(1))
    actual = len(request.session.items)

    assert actual == documented, (
        f"README 徽章写的是 {documented} 项，实际执行 {actual} 项 —— "
        "请把 README 里的测试数一并更新（徽章与正文两处都要改）"
    )

    for pattern in BODY_COUNT_PATTERNS:
        match = re.search(pattern, text)
        assert match, f"README 里找不到测试数文案：{pattern}"
        assert int(match.group(1)) == actual, (
            f"README 正文写的是 {match.group(1)} 项，实际执行 {actual} 项"
        )
