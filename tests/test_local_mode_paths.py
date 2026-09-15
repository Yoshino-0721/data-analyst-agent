"""「按执行器模式给说法」的契约（2026-09-15 真实事故的回归）。

事故回放（会话「根据月的数据进行分析画图」，8 步预算全烧光、`terminated_by=max_steps`）：

    step2  pd.read_excel('/data/Inhouse Lab Test Summary June 2024.xlsx')  → FileNotFoundError
    step3  os.listdir('/data')                                            → FileNotFoundError
    step4  os.walk('/')  满盘找文件                                        → TIMEOUT（30s，
           stdout 里刷出一堆 $RECYCLE.BIN 的路径）
    step5  step6  试 C:/D:/E: 盘根、列 cwd                                 → 才发现文件就在工作目录
    step7  step8  终于读上、画完图                                        → 步数已耗尽，没给收尾回答

根因不是模型笨：`EXECUTOR=local` 时系统提示仍写着「数据在 `/data` 下…**代码里必须用
这个路径**」，而本地执行器根本没有 `/data` —— 它把数据复制进工作目录，让代码用
相对路径读（`src/sandbox/local_executor.py` 的 `_prepare`）。提示与实现对不上，
于是每一步第一次读文件必失败。

两个项目同构的是**架构与写法**，不是**每个模式的语义**：容器里 `/data` 是挂载点、
matplotlibrc 也配好了中文字体，本地模式下两样都没有。这个文件钉住两类说法：

- **路径**：模型侧路径根跟随执行器模式（`mount_root_for_executor`）；本地模式给出的是
  可以直接读取的**文件名**，且不出现 `/data`、`/out`；工具说明书显式警告「别满盘找文件」；
- **画图字体**：本地模式把 `Microsoft YaHei` 排最前（有粗体、覆盖 Ö 这类字符），
  容器模式则明令**不要**自己设 `font.sans-serif`（会顶掉镜像配好的 Noto Sans CJK）。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from src.agent.prompt import build_system_prompt
from src.agent.tools import RUN_PYTHON, ToolRuntime, build_tool_schemas, font_glyph_hint
from src.config import settings
from src.schema.extractor import extract_schema, mount_root_for_executor
from tests.conftest import upload_csv
from tests.conftest_agent import StubExecutor, error_result, ok_result

CSV = "地区,销售额\n华东,100\n华南,200\n"


def _call(name: str, arguments: str):
    from src.llm.client import ToolCall

    return ToolCall(id="call_1", name=name, arguments=arguments)


@pytest.fixture
def sales_csv(tmp_path: Path) -> Path:
    target = tmp_path / "销售.csv"
    target.write_text(CSV, encoding="utf-8")
    return target


def _run_python_description(*, local: bool) -> str:
    for schema in build_tool_schemas(local=local):
        if schema["function"]["name"] == RUN_PYTHON:
            return schema["function"]["description"]
    raise AssertionError("工具清单里没有 run_python")


# --------------------------------------------------------------- 路径根


class TestMountRootFollowsExecutorMode:
    def test_local_mode_has_no_prefix(self):
        """本地模式：文件就在工作目录，模型看到的必须是相对名。"""
        assert mount_root_for_executor("local") == ""

    def test_docker_mode_is_container_mount(self):
        assert mount_root_for_executor("docker") == "/data"

    @pytest.mark.parametrize("value", ["", "nonsense", "Docker", "LOCAL "])
    def test_unknown_falls_back_to_container(self, value):
        """认不出来的一律按容器算 —— 宁可多一个 /data 前缀，也不能把生产模式
        悄悄降级成「相对当前工作目录」。"""
        assert mount_root_for_executor(value) == "/data"

    def test_executors_and_helper_agree(self):
        """两个执行器的自述必须与路径根函数一致（防止两处真相漂移）。"""
        from src.sandbox.docker_executor import DockerExecutor
        from src.sandbox.local_executor import LocalSubprocessExecutor

        assert DockerExecutor.is_local is False
        assert LocalSubprocessExecutor.is_local is True
        assert (mount_root_for_executor("local") == "") is LocalSubprocessExecutor.is_local
        assert (mount_root_for_executor("docker") == "/data") is not DockerExecutor.is_local


# --------------------------------------------------------------- 接口层


class TestWorkspaceReportsModeAwarePath:
    def test_local_mode_reports_bare_name(self, api, monkeypatch):
        monkeypatch.setattr(settings, "executor", "local")
        upload_csv(api, "销售.csv", CSV)
        data = api.get("/api/workspace").json()
        assert data["files"][0]["path"] == "销售.csv"

    def test_docker_mode_reports_container_path(self, api, monkeypatch):
        monkeypatch.setattr(settings, "executor", "docker")
        upload_csv(api, "销售.csv", CSV)
        data = api.get("/api/workspace").json()
        assert data["files"][0]["path"] == "/data/销售.csv"


# --------------------------------------------------------------- 系统提示


class TestSystemPromptPaths:
    def test_local_prompt_has_no_container_paths(self, sales_csv):
        schema = extract_schema(sales_csv, mount_root=mount_root_for_executor("local"))
        prompt = build_system_prompt([schema], local=True)
        # 文件清单那一行必须正好是裸文件名 —— 模型是**照抄**这一行的
        assert "\n- 销售.csv\n" in prompt
        assert "/data/销售.csv" not in prompt, "本地模式不能把容器形式的数据路径摆给模型"
        assert "只有 /out 可写" not in prompt, "本地模式没有 /out"
        assert "本机调试模式" in prompt, "必须显式告诉模型它跑在本机、路径是相对的"
        # 提示里可以**否定地**提到 /data（"没有 /data"），但绝不能把它写成可用路径
        assert "`/data/`" not in prompt and "`/data`：" not in prompt

    def test_docker_prompt_keeps_container_paths(self, sales_csv):
        schema = extract_schema(sales_csv)  # 默认就是 /data
        prompt = build_system_prompt([schema], local=False)
        assert "/data/销售.csv" in prompt
        assert "本机调试模式" not in prompt

    def test_no_data_hint_unchanged_in_both_modes(self):
        assert "还没有加载数据文件" in build_system_prompt([], local=True)
        assert "还没有加载数据文件" in build_system_prompt([], local=False)


# --------------------------------------------------------------- 工具说明书


class TestToolDescriptionPaths:
    def test_local_description_warns_about_missing_data_dir(self):
        desc = _run_python_description(local=True)
        assert "本机调试模式" in desc
        assert "没有" in desc and "`/data`" in desc, "必须点明 /data 不存在"
        assert "os.walk" in desc, "必须点名满盘找文件这条错路"

    def test_docker_description_does_not_warn(self):
        desc = _run_python_description(local=False)
        assert "本机调试模式" not in desc
        assert "os.walk" not in desc

    def test_both_modes_keep_the_core_rules(self):
        """换模式只该换路径说法，安全规则一条都不能少。"""
        for local in (True, False):
            desc = _run_python_description(local=local)
            assert "subprocess" in desc and "socket" in desc
            assert "print()" in desc
            assert "照抄" in desc


# --------------------------------------------------------------- 画图字体


class TestChartFontHint:
    """字体也是「两个实现的语义差异」：镜像改好了 matplotlibrc，本机什么都没有。

    2026-09-15 实测：模型硬编码 `['SimHei', 'Microsoft YaHei', 'DejaVu Sans']`，
    本机因此报 `findfont: Failed to find font weight bold for SimHei`，
    且 `HUNKEMÖLLER` 的 Ö 渲染成豆腐块。容器里更糟 —— SimHei 根本不存在，
    这一行会把镜像配好的 Noto Sans CJK 顶掉，中文全变方块。
    """

    def test_local_hint_puts_yahei_first(self):
        desc = _run_python_description(local=True)
        assert "Microsoft YaHei" in desc
        assert desc.index("Microsoft YaHei") < desc.index("SimHei"), \
            "YaHei 必须排在 SimHei 前面（SimHei 无粗体字重、缺拉丁扩展字符）"

    def test_docker_hint_forbids_overriding_fonts(self):
        desc = _run_python_description(local=False)
        assert "matplotlibrc" in desc and "不要" in desc
        assert "Microsoft YaHei" not in desc, "容器里没有这些 Windows 字体，别让模型去写"

    def test_both_modes_still_mention_matplotlib(self):
        for local in (True, False):
            assert "matplotlib" in _run_python_description(local=local)

    def test_font_rule_is_a_numbered_rule_not_a_footnote(self):
        """升格成规则 6：之前挂在末尾的「图表提示」被模型整段跳过过。"""
        for local in (True, False):
            assert "\n6. " in _run_python_description(local=local)


# ------------------------------------------------- 字体：运行时兜底（真缺字形才提示）


MPL_WARNING = (
    "work\\script.py:157: UserWarning: Glyph 214 (\\N{LATIN CAPITAL LETTER O WITH DIAERESIS}) "
    "missing from font(s) SimHei.\n"
    "findfont: Failed to find font weight bold for SimHei, now using 400.\n"
)


class TestFontGlyphHint:
    """说明书里写一条不够 —— 模型会照抄自己上一轮的写法（实测）。

    所以再从**执行结果的 stderr** 里取证据：matplotlib 真报了缺字形/缺粗体字重，
    就往回填消息末尾挂一条提示。用结果当判据，不猜模型的写法。
    """

    def test_no_warning_no_hint(self):
        assert font_glyph_hint("", local=True) == ""
        assert font_glyph_hint("普通输出，无字体问题\n", local=True) == ""

    def test_local_warning_gets_yahei_first_hint(self):
        hint = font_glyph_hint(MPL_WARNING, local=True)
        assert "Microsoft YaHei" in hint
        assert hint.index("Microsoft YaHei") < hint.index("SimHei")

    def test_container_warning_gets_dont_override_hint(self):
        hint = font_glyph_hint(MPL_WARNING, local=False)
        assert "matplotlibrc" in hint
        assert "Microsoft YaHei" not in hint

    def test_runtime_appends_hint_when_glyphs_missing(self, tmp_path, sales_csv):
        """端到端：执行结果带 matplotlib 告警 -> 回填文本**以这条提示结尾**。"""
        schema = extract_schema(sales_csv, mount_root="")
        executor = StubExecutor([ok_result(stdout="chart saved",
                                           artifacts=(tmp_path / "chart.png",),
                                           stderr=MPL_WARNING)])
        executor.is_local = True
        runtime = ToolRuntime(
            executor=executor,
            schemas=[schema],
            data_files=[sales_csv],
            run_dir_factory=lambda: tmp_path / "run_x",
        )
        outcome = runtime.execute(_call(RUN_PYTHON, '{"code": "print(1)"}'))
        assert "Microsoft YaHei" in outcome.hint
        assert outcome.text.rstrip().endswith(outcome.hint.strip()), \
            "hint 必须在回填消息的最后（§2.7 第 4 条：最后读到的最影响下一段代码）"

    def test_runtime_keeps_engine_hint_and_puts_font_hint_last(self, tmp_path, sales_csv):
        """执行层自己的 hint（列名/超时那类）不能被顶掉，字体提示追在其后。"""
        schema = extract_schema(sales_csv, mount_root="")
        executor = StubExecutor([error_result(stderr=MPL_WARNING, hint="先用 get_schema 确认列名")])
        executor.is_local = True
        runtime = ToolRuntime(
            executor=executor,
            schemas=[schema],
            data_files=[sales_csv],
            run_dir_factory=lambda: tmp_path / "run_y",
        )
        outcome = runtime.execute(_call(RUN_PYTHON, '{"code": "print(1)"}'))
        assert "先用 get_schema 确认列名" in outcome.hint
        assert outcome.hint.index("先用 get_schema") < outcome.hint.index("Microsoft YaHei")

    def test_docker_runtime_gets_container_hint(self, tmp_path, sales_csv):
        schema = extract_schema(sales_csv)  # 容器模式：/data/销售.csv
        executor = StubExecutor([ok_result(stdout="x", stderr=MPL_WARNING)])
        runtime = ToolRuntime(
            executor=executor,
            schemas=[schema],
            data_files=[sales_csv],
            run_dir_factory=lambda: tmp_path / "run_z",
        )
        outcome = runtime.execute(_call(RUN_PYTHON, '{"code": "print(1)"}'))
        assert "matplotlibrc" in outcome.hint
        assert "Microsoft YaHei" not in outcome.hint


# --------------------------------------------------------------- 会话提示串


class TestSchemaPromptString:
    def test_local_reader_hint_is_relative(self, sales_csv):
        text = extract_schema(sales_csv, mount_root="").to_prompt_string()
        assert "pd.read_csv('销售.csv')" in text
        assert "/data/" not in text

    def test_docker_reader_hint_is_container(self, sales_csv):
        text = extract_schema(sales_csv).to_prompt_string()
        assert "pd.read_csv('/data/销售.csv')" in text


# --------------------------------------------------------------- 运行时自述


class TestToolRuntimeReportsMode:
    def test_stub_executor_defaults_to_container(self):
        """测试里常见的桩执行器没有 is_local —— 默认按容器算，既有断言不受影响。"""
        runtime = ToolRuntime(executor=StubExecutor([]))
        assert runtime.local_executor is False

    def test_local_executor_is_reported(self):
        from src.sandbox.local_executor import LocalSubprocessExecutor

        executor = LocalSubprocessExecutor.__new__(LocalSubprocessExecutor)
        runtime = ToolRuntime(executor=executor)
        assert runtime.local_executor is True
