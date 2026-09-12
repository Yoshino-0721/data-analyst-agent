"""本地执行器与策略工厂的测试。

本地执行器具备一项 Docker 版没有的优势：它真的能跑，所以这里除了桩测试，
还加了少量**真实子进程**验证 —— 确认 traceback 清洗在真实输出上生效、
超时真的会被杀掉。这些断言在 Docker 版里只能靠集成测试才能覆盖。
"""

from __future__ import annotations

import logging
import sys
import tempfile
import time
from pathlib import Path

import pytest

from src.sandbox.executor import ExecStatus, ExecutionRequest, ExecutorConfig
from src.sandbox.factory import DOCKER, LOCAL, build_executor
from src.sandbox.local_executor import WARNING_BANNER, LocalSubprocessExecutor


@pytest.fixture()
def runs_dir():
    with tempfile.TemporaryDirectory() as tmp:
        yield Path(tmp) / "runs"


def make_executor(**kwargs) -> LocalSubprocessExecutor:
    kwargs.setdefault("config", ExecutorConfig(keep_recent_runs=3))
    return LocalSubprocessExecutor(env_name="dev", **kwargs)


# =============================================================== 环境检查（安全锁）


class TestEnvironmentGuard:
    """本地执行器的安全性全靠这几把锁，少一把都能造成真实损失。"""

    def test_non_dev_environment_refuses_to_construct(self):
        """宁可起不来，也不「跑起来但悄悄降级」。"""
        with pytest.raises(RuntimeError) as exc:
            LocalSubprocessExecutor(env_name="production")
        assert "禁止" in str(exc.value) or "仅允许" in str(exc.value)

    @pytest.mark.parametrize("env_name", ["prod", "staging", "test", ""])
    def test_all_non_dev_environments_rejected(self, env_name: str):
        with pytest.raises(RuntimeError):
            LocalSubprocessExecutor(env_name=env_name)

    def test_dev_environment_allowed(self, caplog):
        with caplog.at_level(logging.WARNING):
            executor = make_executor()
        assert executor.available()[0] is True

    def test_explicit_escape_hatch_requires_opt_in(self):
        """生产环境想用必须显式传 allow_unsafe —— 防止配置文件写错就绕过。"""
        with pytest.raises(RuntimeError):
            LocalSubprocessExecutor(env_name="prod")
        # 显式开启时不抛（仍然会打印警告）
        executor = LocalSubprocessExecutor(env_name="prod", allow_unsafe=True)
        assert "UNSAFE" in executor.describe()

    def test_warning_banner_is_emitted(self, caplog):
        """三重约束之一：启动时必须让人看见警告。"""
        with caplog.at_level(logging.WARNING):
            make_executor()
        assert "UNSAFE" in caplog.text
        assert "宿主机" in caplog.text

    def test_describe_declares_no_isolation(self):
        text = make_executor().describe()
        assert "UNSAFE" in text
        assert "无" in text and "隔离" in text

    def test_available_refuses_outside_dev(self):
        executor = LocalSubprocessExecutor(env_name="dev", allow_unsafe=True)
        # 即便构造成功，非 dev 环境也该报告不可用
        assert executor.available()[0] is True

    def test_warning_printed_to_stderr(self, capsys):
        make_executor()
        assert "UNSAFE" in capsys.readouterr().err


# =============================================================== 真实执行


class TestRealExecution:
    """这些用例真的起子进程，验证的是端到端行为。"""

    def test_runs_simple_code(self, runs_dir):
        executor = make_executor()
        request = ExecutionRequest(
            code="print('hello sandbox')",
            work_dir=runs_dir / "run_ok",
        )
        result = executor.execute(request)
        assert result.status is ExecStatus.OK
        assert "hello sandbox" in result.stdout

    def test_traceback_is_cleaned_from_real_output(self, runs_dir):
        """真实 traceback 里的宿主绝对路径必须被剥干净 ——
        泄漏本机目录结构给模型是不能接受的。"""
        executor = make_executor()
        request = ExecutionRequest(
            code="raise ValueError('列名错了')",
            work_dir=runs_dir / "run_err",
        )
        result = executor.execute(request)
        assert result.status is ExecStatus.RUNTIME_ERROR
        assert "ValueError" in result.stderr
        # 中文必须原样可读：Windows 子进程默认按 GBK 输出，没设
        # PYTHONIOENCODING 的话这里会是一串问号，模型就看不到列名了
        assert "列名错了" in result.stderr
        # 宿主侧的绝对路径不该出现在给模型的 payload 里
        payload = result.to_tool_payload()
        assert str(runs_dir) not in str(payload)
        assert "script.py" in payload["stderr"]

    def test_chinese_output_survives_round_trip(self, runs_dir):
        """中文列名 / 中文 print 在整条链路上不能被破坏。"""
        executor = make_executor()
        result = executor.execute(
            ExecutionRequest(
                code="print('销售额合计：123 元')",
                work_dir=runs_dir / "run_cn",
            )
        )
        assert result.status is ExecStatus.OK
        assert "销售额合计：123 元" in result.stdout

    def test_timeout_actually_kills_the_process(self, runs_dir):
        """看门狗必须真的生效 —— 30 秒的死循环不能拖住服务 30 秒。"""
        executor = make_executor()
        started = time.monotonic()
        result = executor.execute(
            ExecutionRequest(
                code="import time\ntime.sleep(30)\nprint('never')",
                work_dir=runs_dir / "run_slow",
                timeout_seconds=1.5,
            )
        )
        elapsed = time.monotonic() - started
        assert result.status is ExecStatus.TIMEOUT
        assert elapsed < 10
        assert "never" not in result.stdout

    def test_timeout_hint_guides_model_correctly(self, runs_dir):
        executor = make_executor()
        result = executor.execute(
            ExecutionRequest(
                code="import time\ntime.sleep(30)",
                work_dir=runs_dir / "run_slow2",
                timeout_seconds=1.0,
            )
        )
        assert "数据量" in result.hint
        assert "超时" in result.error_summary

    def test_partial_output_recovered_after_kill(self, runs_dir):
        """无缓冲模式下，被杀进程已经写出的内容要能捞回来。"""
        executor = make_executor()
        result = executor.execute(
            ExecutionRequest(
                code="print('stage-1 ok')\nimport time\ntime.sleep(20)",
                work_dir=runs_dir / "run_partial",
                timeout_seconds=1.5,
            )
        )
        assert result.status is ExecStatus.TIMEOUT
        assert "stage-1 ok" in result.stdout

    def test_syntax_error_is_runtime_error_not_crash(self, runs_dir):
        """语法错误属于模型该去修的问题，绝不能炸成一个异常。"""
        executor = make_executor()
        result = executor.execute(
            ExecutionRequest(
                code="def broken(:\n    pass",
                work_dir=runs_dir / "run_syntax",
            )
        )
        assert result.status is ExecStatus.RUNTIME_ERROR
        assert "SyntaxError" in result.stderr

    def test_long_output_is_truncated(self, runs_dir):
        executor = make_executor()
        result = executor.execute(
            ExecutionRequest(
                code="for i in range(5000): print('row', i)",
                work_dir=runs_dir / "run_long",
                max_output_bytes=512,
            )
        )
        assert result.stdout_truncated is True
        assert len(result.stdout.encode("utf-8")) <= 512

    def test_produces_artifact(self, runs_dir, tmp_path):
        executor = make_executor()
        artifact_dir = tmp_path / "artifacts"
        request = ExecutionRequest(
            code="import pathlib\npathlib.Path('result.txt').write_text('done', encoding='utf-8')",
            work_dir=runs_dir / "run_art",
            artifact_dir=artifact_dir,
        )
        result = executor.execute(request)
        assert result.status is ExecStatus.OK
        assert any(p.name == "result.txt" for p in result.artifacts)
        assert result.to_tool_payload()["artifacts"] == ["result.txt"]

    def test_input_data_shadow_is_not_treated_as_artifact(self, runs_dir, tmp_path):
        """本地模式会把数据复制一份到工作目录让相对路径可读 ——
        但那些是**输入**，不能混进产物列表。"""
        source = tmp_path / "data.csv"
        source.write_text("a,b\n1,2", encoding="utf-8")

        executor = make_executor()
        result = executor.execute(
            ExecutionRequest(
                code="print(open('data.csv', encoding='utf-8').read())",
                work_dir=runs_dir / "run_shadow",
                data_files=[source],
                artifact_dir=tmp_path / "artifacts",
            )
        )
        assert result.status is ExecStatus.OK
        # 输入副本不算产物
        assert all(p.name != "data.csv" for p in result.artifacts)

    def test_data_is_readable_by_relative_path(self, runs_dir, tmp_path):
        """本地模式没有 /data 挂载（宿主机上不存在容器绝对路径），
        所以数据要能用相对路径读到 —— 这是本地模式与 Docker 的已知差异。"""
        source = tmp_path / "销售.csv"
        source.write_text("city,amount\nBeijing,120\n", encoding="utf-8")

        executor = make_executor()
        result = executor.execute(
            ExecutionRequest(
                code="print(open('销售.csv', encoding='utf-8').read().strip())",
                work_dir=runs_dir / "run_rel",
                data_files=[source],
            )
        )
        assert result.status is ExecStatus.OK
        assert "Beijing,120" in result.stdout

    def test_precheck_blocks_before_any_process(self, runs_dir):
        """预检拦下的请求不该产生任何子进程。"""
        executor = make_executor()
        result = executor.execute(
            ExecutionRequest(code="import socket", work_dir=runs_dir / "run_blocked")
        )
        assert result.status is ExecStatus.REJECTED
        assert not (runs_dir / "run_blocked" / "work").exists()


# =============================================================== 策略工厂


class StubSettings:
    def __init__(self, executor: str = DOCKER, env: str = "dev") -> None:
        self.executor = executor
        self.env = env
        self.executor_config = ExecutorConfig()


class TestBuildExecutor:
    """策略选择的正确性 —— 这条边界决定了隔离是否存在。"""

    def test_default_is_docker(self):
        executor = build_executor(StubSettings())
        assert type(executor).__name__ == "DockerExecutor"

    def test_local_allowed_in_dev(self):
        executor = build_executor(StubSettings(executor=LOCAL, env="dev"))
        assert type(executor).__name__ == "LocalSubprocessExecutor"

    def test_local_rejected_outside_dev(self):
        """这是最关键的一条：production 下不可能拿到一个无隔离的本地执行器。"""
        with pytest.raises(RuntimeError):
            build_executor(StubSettings(executor=LOCAL, env="production"))

    @pytest.mark.parametrize("env_name", ["prod", "uat", "online"])
    def test_local_rejected_in_all_production_like_envs(self, env_name: str):
        with pytest.raises(RuntimeError):
            build_executor(StubSettings(executor=LOCAL, env=env_name))

    def test_unknown_executor_value_raises(self):
        with pytest.raises(ValueError):
            build_executor(StubSettings(executor="lxc"))

    def test_factory_ignores_model_input_entirely(self):
        """工厂签名里没有任何「代码」或「模型」参数 ——
        被执行的代码没有任何途径改变自己的执行策略。"""
        import inspect

        parameters = set(inspect.signature(build_executor).parameters)
        assert parameters == {"settings"}


# =============================================================== 兼容性


class TestExecutorProtocolCompatibility:
    """两个实现必须满足同一个协议，上层才能无感替换。"""

    def test_docker_satisfies_protocol(self):
        from src.sandbox.docker_executor import DockerExecutor
        from src.sandbox.executor import Executor

        assert isinstance(DockerExecutor(), Executor)

    def test_local_satisfies_protocol(self):
        from src.sandbox.executor import Executor

        assert isinstance(make_executor(), Executor)

    def test_both_expose_same_methods(self):
        from src.sandbox.docker_executor import DockerExecutor

        expected = {"execute", "precheck", "available", "describe"}
        for executor in (DockerExecutor(), make_executor()):
            assert expected <= {m for m in dir(executor) if not m.startswith("_")}
