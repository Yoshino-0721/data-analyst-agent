"""工具层测试。

重点验两件事：
1. `run_python` 的入参里**不能有路径参数**（这是安全设计，不是省事）；
2. 回填文本里不能出现宿主路径（净化必须在执行层生效）。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from src.agent.tools import (
    GET_SCHEMA,
    RUN_PYTHON,
    ToolRuntime,
    build_tool_schemas,
    failure_fingerprint,
)
from src.schema.extractor import extract_schema
from src.sandbox.executor import ExecStatus, ExecutionRequest
from tests.conftest_agent import StubExecutor, error_result, ok_result


@pytest.fixture
def sales_csv(tmp_path: Path) -> Path:
    target = tmp_path / "销售.csv"
    target.write_text(
        "地区,销售额\n华东,100\n华南,200\n华东,300\n", encoding="utf-8"
    )
    return target


# ------------------------------------------------------------------ Schema 定义


class TestToolSchemas:
    def test_run_python_has_only_code_parameter(self):
        """入参只有 code —— **没有**文件路径参数。

        给模型一个 data_path 参数，它就会开始猜路径，进而可能构造出
        指向宿主机其它位置的路径。路径该由编排层在系统提示里给死。
        """
        schema = next(
            t for t in build_tool_schemas() if t["function"]["name"] == RUN_PYTHON
        )
        properties = schema["function"]["parameters"]["properties"]
        assert set(properties) == {"code"}
        assert "path" not in properties
        assert "file" not in properties
        assert "data_path" not in properties

    def test_run_python_describes_the_sandbox_rules(self):
        """说明书要写清能做什么、不能做什么 —— 这部分 token 花得值。"""
        schema = next(
            t for t in build_tool_schemas() if t["function"]["name"] == RUN_PYTHON
        )
        description = schema["function"]["description"]
        assert "/out" in description, "必须说明只有 /out 可写"
        assert "照抄" in description, "必须要求照抄路径而不是自行拼接"

    def test_get_schema_file_name_is_optional(self):
        """file_name 可选 —— 不传时返回全部文件概览。"""
        schema = next(
            t for t in build_tool_schemas() if t["function"]["name"] == GET_SCHEMA
        )
        parameters = schema["function"]["parameters"]
        assert "file_name" in parameters["properties"]
        assert parameters.get("required", []) == []

    def test_all_tools_declare_type_function(self):
        for tool in build_tool_schemas():
            assert tool["type"] == "function"
            assert tool["function"]["name"]


# ------------------------------------------------------------------ run_python


class TestRunPython:
    def test_code_is_passed_to_executor(self, tmp_path):
        executor = StubExecutor([ok_result()])
        runtime = ToolRuntime(
            executor=executor, run_dir_factory=lambda: tmp_path / "run"
        )
        runtime.execute(_call(RUN_PYTHON, '{"code": "print(1+1)"}'))
        assert executor.requests[0].code == "print(1+1)"

    def test_data_files_are_attached(self, tmp_path, sales_csv):
        executor = StubExecutor([ok_result()])
        runtime = ToolRuntime(
            executor=executor,
            data_files=[sales_csv],
            run_dir_factory=lambda: tmp_path / "run",
        )
        runtime.execute(_call(RUN_PYTHON, '{"code": "print(1)"}'))
        assert list(executor.requests[0].data_files) == [sales_csv]

    def test_missing_code_is_rejected(self, tmp_path):
        """没有 code 不该崩，也不该拿空代码去跑一次容器。"""
        runtime = ToolRuntime(
            executor=StubExecutor([]), run_dir_factory=lambda: tmp_path / "run"
        )
        outcome = runtime.execute(_call(RUN_PYTHON, "{}"))
        assert outcome.status == "REJECTED"
        assert "code" in outcome.text

    def test_backfill_has_no_host_path(self, tmp_path):
        """回填文本里绝不能出现宿主绝对路径。

        模型知道宿主目录结构没有任何用处，反而给了它构造路径的素材。
        """
        executor = StubExecutor([ok_result("结果：42")])
        runtime = ToolRuntime(
            executor=executor, run_dir_factory=lambda: tmp_path / "run"
        )
        outcome = runtime.execute(_call(RUN_PYTHON, '{"code": "print(42)"}'))
        assert str(tmp_path) not in outcome.text
        assert ":\\" not in outcome.text

    def test_ok_status_is_reported(self, tmp_path):
        executor = StubExecutor([ok_result()])
        runtime = ToolRuntime(
            executor=executor, run_dir_factory=lambda: tmp_path / "run"
        )
        assert runtime.execute(_call(RUN_PYTHON, '{"code": "print(1)"}')).status == "OK"

    def test_artifacts_are_file_names_only(self, tmp_path):
        chart = tmp_path / "deep" / "chart.png"
        executor = StubExecutor([ok_result(artifacts=(chart,))])
        runtime = ToolRuntime(
            executor=executor, run_dir_factory=lambda: tmp_path / "run"
        )
        outcome = runtime.execute(_call(RUN_PYTHON, '{"code": "print(1)"}'))
        assert outcome.artifacts == ("chart.png",)

    def test_runtime_error_status(self, tmp_path):
        executor = StubExecutor([error_result(status=ExecStatus.RUNTIME_ERROR)])
        runtime = ToolRuntime(
            executor=executor, run_dir_factory=lambda: tmp_path / "run"
        )
        assert (
            runtime.execute(_call(RUN_PYTHON, '{"code": "bad"}')).status
            == "RUNTIME_ERROR"
        )

    def test_missing_run_dir_factory_raises(self):
        """这是配置错误，不是代码错误 —— 照抛，别静默。"""
        runtime = ToolRuntime(executor=StubExecutor([]))
        with pytest.raises(RuntimeError, match="run_dir_factory"):
            runtime.execute(_call(RUN_PYTHON, '{"code": "print(1)"}'))


# ------------------------------------------------------------------ get_schema


class TestGetSchema:
    def test_overview_without_file_name(self, tmp_path, sales_csv):
        schema = extract_schema(sales_csv)
        runtime = ToolRuntime(executor=StubExecutor([]), schemas=[schema])
        outcome = runtime.execute(_call(GET_SCHEMA, "{}"))
        assert "销售.csv" in outcome.text
        assert "/data/销售.csv" in outcome.text

    def test_specific_file_returns_columns(self, tmp_path, sales_csv):
        schema = extract_schema(sales_csv)
        runtime = ToolRuntime(executor=StubExecutor([]), schemas=[schema])
        outcome = runtime.execute(_call(GET_SCHEMA, '{"file_name": "销售.csv"}'))
        assert "地区" in outcome.text
        assert "销售额" in outcome.text

    def test_unknown_file_lists_available(self, tmp_path, sales_csv):
        """文件名猜错时，把可用清单回给它 —— 一次纠正，别让它反复试。"""
        schema = extract_schema(sales_csv)
        runtime = ToolRuntime(executor=StubExecutor([]), schemas=[schema])
        outcome = runtime.execute(_call(GET_SCHEMA, '{"file_name": "不存在.csv"}'))
        assert "销售.csv" in outcome.text

    def test_no_schemas_loaded(self):
        runtime = ToolRuntime(executor=StubExecutor([]))
        assert "没有已加载" in runtime.execute(_call(GET_SCHEMA, "{}")).text


# ------------------------------------------------------------------ 未知工具


class TestUnknownTool:
    def test_unknown_tool_is_reported_not_crashed(self):
        runtime = ToolRuntime(executor=StubExecutor([]))
        outcome = runtime.execute(_call("delete_everything", "{}"))
        assert "未知工具" in outcome.text
        assert RUN_PYTHON in outcome.text


# ------------------------------------------------------------------ 失败指纹


class TestFailureFingerprint:
    def test_ok_has_no_fingerprint(self):
        assert failure_fingerprint({"status": "OK"}) is None

    def test_same_error_gives_same_fingerprint(self):
        first = failure_fingerprint(
            {"status": "RUNTIME_ERROR", "stderr": '  File "s.py", line 3\nKeyError: 地区'}
        )
        second = failure_fingerprint(
            {"status": "RUNTIME_ERROR", "stderr": '  File "s.py", line 3\nKeyError: 地区'}
        )
        assert first == second

    def test_different_error_gives_different_fingerprint(self):
        first = failure_fingerprint({"status": "RUNTIME_ERROR", "stderr": "KeyError: 地区"})
        second = failure_fingerprint({"status": "RUNTIME_ERROR", "stderr": "ValueError: x"})
        assert first != second

    def test_rejected_is_also_a_failure(self):
        """预检拒绝也算失败 —— 模型反复写被拒的代码同样是在撞墙。"""
        fingerprint = failure_fingerprint(
            {"status": "REJECTED", "error_summary": "不允许 import socket"}
        )
        assert fingerprint is not None

    def test_timeout_and_oom_differ(self):
        timeout = failure_fingerprint({"status": "TIMEOUT", "stderr": ""})
        oom = failure_fingerprint({"status": "OOM", "stderr": ""})
        assert timeout != oom


# ------------------------------------------------------------------ 小工具


def _call(name: str, arguments: str):
    from src.llm.client import ToolCall

    return ToolCall(id="call_1", name=name, arguments=arguments)


def _unused() -> None:  # pragma: no cover
    assert ExecutionRequest is not None
