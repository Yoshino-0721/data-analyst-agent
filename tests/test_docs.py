"""文档守卫：README 与 docs/status.md 里的测试数必须与真实用例数一致。

与项目一 `tests/test_docs.py` 同构（那边还额外守 README 的图片与部署文档路径，
本项目没有那些资产）—— 数字是最容易悄悄过期的东西：每加一条用例它就落后一次，
而过期不会让任何测试变红。

两处守卫**共用同一个真相源**：本次会话真正收集到的用例数（`len(session.items)`）。
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
README = ROOT / "README.md"
STATUS = ROOT / "docs" / "status.md"

BODY_COUNT_PATTERNS = (
    r"运行测试（(\d+) 项",
)

# §0 表格里那一行（两仓库的 status.md 是**同一份文件**，两个数字并排）：
#   | 测试 | **471 通过** | **690 通过**（17 deselected，Docker 集成默认跳过） |
# 本项目是**第二列**，第一列是项目一。取错列就等于拿对家的数字来断言，
# 会造出一个永远"看起来在守"的空转守卫 —— 所以下面有专门的自测钉住列选择。
STATUS_COUNT_ROW = re.compile(r"^\| 测试 \| \*\*(\d+) 通过\*\* \| \*\*(\d+) 通过\*\*", re.M)
STATUS_COLUMN = 2


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


def _require_full_suite(request) -> None:
    """只跑了子集时不判定数字（否则 `-k` 之类的局部运行会被守卫误伤）。"""
    on_disk = {p.name for p in (ROOT / "tests").glob("test_*.py")} - _docker_only_test_files()
    collected = {item.path.name for item in request.session.items}
    if on_disk - collected:
        pytest.skip(
            f"只跑了部分测试文件，跳过测试数守卫（未收集：{sorted(on_disk - collected)}）"
        )


def _documented_count_from_status(text: str, column: int) -> int | None:
    """从 §0 表格取指定列的测试数；行缺失或格式变了返回 None（**不静默跳过**）。"""
    match = STATUS_COUNT_ROW.search(text)
    if not match:
        return None
    return int(match.group(column))


def test_readme_test_count_matches_reality(request):
    """README 里写的测试数必须与真实执行的用例数一致。

    为什么需要这条：数字徽章是会**漂移**的硬编码 —— 每加一条用例它就过期一次，
    而过期不会让任何测试变红，只会让读到它的人被误导（这个项目一路改下来，
    徽章已经悄悄落后过两次）。
    """
    _require_full_suite(request)

    text = README.read_text(encoding="utf-8")
    badge = re.search(r"tests-(\d+)%20passed", text)
    assert badge, "README 徽章里找不到 tests-<N>%20passed"
    documented = int(badge.group(1))
    actual = len(request.session.items)

    assert actual == documented, (
        f"README 徽章写的是 {documented} 项，实际执行 {actual} 项 —— "
        "请把 README 里的测试数一并更新（徽章与正文两处都要改）。"
        "注意**别用裸数字替换**：README 里还有 HTTP 状态码这类同名数字"
        "（例如登录限流的 429、限流配置里的 300），按数字全局替换会把它一起改掉。"
        "用带上下文的片段替换（`tests-<N>%20passed`、`运行测试（<N> 项`）并断言恰好命中 1 处。"
    )

    for pattern in BODY_COUNT_PATTERNS:
        match = re.search(pattern, text)
        assert match, f"README 里找不到测试数文案：{pattern}"
        assert int(match.group(1)) == actual, (
            f"README 正文写的是 {match.group(1)} 项，实际执行 {actual} 项"
        )


def test_status_table_test_count_matches_reality(request):
    """`docs/status.md` §0 表格里的测试数也必须与真实用例数一致。

    为什么这条比 README 那条更要紧：§0 表格是**新会话自检第 2 步**的核心输入 ——
    它漂移了，下一轮会话会把「文档写的数」和「实际跑出来的数」对不上判成
    **环境漂移**，然后花时间去排查一个根本不存在的问题。
    （2026-09-15 真实发生过一次：p2 的用例从 661 涨到 690，README 有守卫所以被抓住，
    这份表没有守卫，就一直停在 661。）
    """
    _require_full_suite(request)

    text = STATUS.read_text(encoding="utf-8")
    documented = _documented_count_from_status(text, STATUS_COLUMN)
    assert documented is not None, (
        "status.md §0 里找不到测试数那一行（格式可能被改过）—— "
        "守卫依赖这一行的形状：`| 测试 | **N 通过** | **N 通过**（…） |`"
    )
    actual = len(request.session.items)
    assert documented == actual, (
        f"status.md §0 表格写的是 {documented} 项，实际执行 {actual} 项 —— "
        "请同步这一行；注意两份 status.md 是**同一个 sha256 的同构文件**，"
        "改完要同步到另一个仓库，且**别用裸数字替换**（表里还有端口、deselected 等数字）。"
    )


def test_status_guard_reads_the_right_column():
    """守卫的自测（防「空转」）：格式变了要认出来，列取错了要当场暴露。

    最容易犯的错是**取错列** —— 读到对家的数字，守卫照样"绿"，但它守的是别人。
    """
    row = "| 测试 | **111 通过** | **222 通过**（17 deselected，Docker 集成默认跳过） |"
    assert _documented_count_from_status(row, 1) == 111
    assert _documented_count_from_status(row, 2) == 222, "本项目取第二列"
    assert _documented_count_from_status("这一行根本不存在", 2) is None
    assert _documented_count_from_status("| 测试 | 111 通过 | 222 通过 |", 1) is None, (
        "格式变了必须返回 None（宁可报错也不要静默认一个错数字）"
    )
