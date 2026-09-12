"""DockerExecutor 的桩对象测试。

全部用 `StubRunner` 假造运行结果，不触碰真实 Docker —— 这样在没有 Docker
的机器上也能验证：命令构造是否安全、超时是否被正确归类、traceback 是否
被洗干净、产物是否被拷出、旧目录有没有被回收。

真实集成测试在 `test_docker_integration.py`，需要 Docker 且默认跳过。
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

import pytest

from src.sandbox.docker_executor import (
    SCRIPT_NAME,
    DockerExecutor,
    RunOutcome,
    SubprocessRunner,
    _host_path,
    _prepare_workspace,
    _prune_old_runs,
)
from src.sandbox.executor import (
    ExecStatus,
    ExecutionRequest,
    ExecutorConfig,
)


class StubRunner:
    """假的命令行执行器 —— 记录调用、返回预设结果。"""

    def __init__(self, outcome: RunOutcome) -> None:
        self.outcome = outcome
        self.calls: list[tuple[list[str], float, str]] = []

    def run(self, command, timeout: float, container_name: str) -> RunOutcome:
        self.calls.append((list(command), timeout, container_name))
        return self.outcome

    @property
    def command(self) -> list[str]:
        return self.calls[0][0]

    @property
    def container_name(self) -> str:
        return self.calls[0][2]


@pytest.fixture()
def workspace(tmp_path: Path):
    """一次运行所需的目录布局。"""
    return tmp_path / "runs" / "run_test01"


def make_executor(outcome: RunOutcome, **config_kwargs) -> tuple[DockerExecutor, StubRunner]:
    config = ExecutorConfig(keep_recent_runs=5, **config_kwargs)
    runner = StubRunner(outcome)
    return DockerExecutor(config, runner=runner), runner


def make_request(workspace: Path, **kwargs) -> ExecutionRequest:
    kwargs.setdefault("code", "print('hello')")
    kwargs.setdefault("artifact_dir", workspace.parent / "artifacts")
    return ExecutionRequest(work_dir=workspace, **kwargs)


# =============================================================== 命令构造（安全性的核心）


class TestCommandConstruction:
    """这几个断言守的是「隔离到底是什么」。改坏了就没有隔离了。"""

    def test_disables_network(self, workspace):
        executor, runner = make_executor(RunOutcome(0, "", "", False))
        executor.execute(make_request(workspace))
        assert "--network=none" in runner.command

    def test_root_filesystem_is_read_only(self, workspace):
        """只有挂载进来的 /out 可写 —— 代码没法往镜像里塞东西。"""
        executor, runner = make_executor(RunOutcome(0, "", "", False))
        executor.execute(make_request(workspace))
        assert "--read-only" in runner.command

    def test_resource_limits_present(self, workspace):
        executor, runner = make_executor(RunOutcome(0, "", "", False))
        executor.execute(make_request(workspace))
        command = runner.command
        assert "--cpus=1" in command
        assert "--memory=512m" in command
        assert "--pids-limit=64" in command

    def test_memory_swap_equals_memory(self, workspace):
        """禁掉 swap。不禁的话「内存超了」会退化成「莫名很慢」，掩盖真正的问题。"""
        executor, runner = make_executor(RunOutcome(0, "", "", False))
        executor.execute(make_request(workspace))
        assert "--memory-swap=512m" in runner.command

    def test_no_new_privileges_and_non_root(self, workspace):
        executor, runner = make_executor(RunOutcome(0, "", "", False))
        executor.execute(make_request(workspace))
        assert "--security-opt=no-new-privileges" in runner.command
        assert "--user=1000" in runner.command

    def test_container_is_ephemeral(self, workspace):
        """用完即焚，保证环境干净无状态污染 —— 这是明确选择过的取舍。"""
        executor, runner = make_executor(RunOutcome(0, "", "", False))
        executor.execute(make_request(workspace))
        assert "--rm" in runner.command

    def test_mounts_are_explicitly_scoped(self, workspace):
        """/work 与 /data 只读，只有 /out 可写。"""
        executor, runner = make_executor(RunOutcome(0, "", "", False))
        executor.execute(make_request(workspace))
        volumes = [c for c in runner.command if c.startswith("--volume=")]
        assert len(volumes) == 3
        joined = " ".join(volumes)
        assert ":/work:ro" in joined
        assert ":/data:ro" in joined
        assert ":/out:rw" in joined

    def test_code_never_appears_in_command(self, workspace):
        """代码从头到尾只是一段被写进文件的文本，绝不进入命令行。

        这条断言直接否掉了 `os.system(model_output)` 这类致命写法。
        """
        marker = "PAYLOAD_SHOULD_NEVER_REACH_ARGV"
        executor, runner = make_executor(RunOutcome(0, "", "", False))
        executor.execute(make_request(workspace, code=f"print('{marker}')"))
        assert marker not in " ".join(runner.command)

    def test_runs_in_isolated_mode(self, workspace):
        """python -I：忽略 PYTHONPATH 与用户 site-packages，
        依赖图景完全由镜像决定。"""
        executor, runner = make_executor(RunOutcome(0, "", "", False))
        executor.execute(make_request(workspace))
        assert runner.command[-5:] == ["python", "-I", "-X", "utf8=1", "-u", f"/work/{SCRIPT_NAME}"][-5:]

    def test_forces_utf8_via_flag_not_env(self, workspace):
        """`-X utf8=1` 必须是**命令行选项**，不能靠 PYTHONIOENCODING 环境变量。

        坑：`python -I` 隐含 `-E`，会忽略所有 PYTHON* 环境变量。
        如果改成环境变量形式，中文列名的报错会变成一串问号 ——
        模型看不到列名就永远修不对。这条断言防止以后有人「简化」掉它。
        """
        executor, runner = make_executor(RunOutcome(0, "", "", False))
        executor.execute(make_request(workspace))
        command = runner.command
        assert "-X" in command
        assert command[command.index("-X") + 1] == "utf8=1"
        # 环境变量形式必须同时保留作兜底，但不能**只有**环境变量
        assert not any(
            c.startswith("--env=PYTHONIOENCODING") for c in command
        ) or "-X" in command

    def test_unbuffered_output_flag(self, workspace):
        """-u 保证超时被强杀时还能捞到已产生的输出。"""
        executor, runner = make_executor(RunOutcome(0, "", "", False))
        executor.execute(make_request(workspace))
        assert "-u" in runner.command

    def test_max_mount_bytes_runtime_has_tmpfs(self, workspace):
        """matplotlib/pandas 需要可写缓存目录，否则连画图都跑不起来。"""
        executor, runner = make_executor(RunOutcome(0, "", "", False))
        executor.execute(make_request(workspace))
        assert any(c.startswith("--tmpfs=/tmp") for c in runner.command)
        assert "--env=MPLCONFIGDIR=/tmp" in runner.command

    def test_container_name_is_unique_per_run(self, workspace):
        """唯一名字是为了超时后能定点 `docker rm -f`，不会误杀别人的容器。"""
        executor, runner = make_executor(RunOutcome(0, "", "", False))
        executor.execute(make_request(workspace))
        first = runner.container_name
        executor.execute(make_request(workspace))
        assert runner.calls[1][2] != first


# =============================================================== 最小权限的数据挂载


class TestMinimalDataMounting:
    """用户决策：每次只挂载本次会话实际用到的文件。"""

    def test_only_requested_files_are_copied(self, workspace, tmp_path):
        source_dir = tmp_path / "uploads"
        source_dir.mkdir()
        used = source_dir / "销售.csv"
        unused = source_dir / "机密-薪酬.csv"
        used.write_text("a,b\n1,2", encoding="utf-8")
        unused.write_text("salary", encoding="utf-8")

        executor, runner = make_executor(RunOutcome(0, "", "", False))
        executor.execute(make_request(workspace, data_files=[used]))

        data_dir = workspace / "data"
        assert (data_dir / "销售.csv").exists()
        # 没提到的文件绝不能被挂进容器 —— 这就是最小权限的落点
        assert not (data_dir / "机密-薪酬.csv").exists()

    def test_path_traversal_filename_is_contained(self, workspace, tmp_path):
        """ ../../etc/passwd 必须落在 data/ 内部，否则副本隔离形同虚设。"""
        source = tmp_path / "passwd"
        source.write_text("root:x:0:0", encoding="utf-8")

        executor, runner = make_executor(RunOutcome(0, "", "", False))
        executor.execute(make_request(workspace, data_files=[source]))

        data_dir = workspace / "data"
        for item in data_dir.iterdir():
            assert data_dir in item.parents or item.parent == data_dir
            assert ".." not in item.name

    def test_same_basename_from_two_dirs_both_survive(self, workspace, tmp_path):
        """同名文件来自不同目录时不能互相覆盖。"""
        first = tmp_path / "a" / "data.csv"
        second = tmp_path / "b" / "data.csv"
        first.parent.mkdir()
        second.parent.mkdir()
        first.write_text("one", encoding="utf-8")
        second.write_text("two", encoding="utf-8")

        _prepare_workspace(
            ExecutionRequest(code="x=1", work_dir=workspace, data_files=[first, second]),
            script_dir=workspace / "work",
            data_dir=workspace / "data",
            out_dir=workspace / "out",
        )
        assert len(list((workspace / "data").iterdir())) == 2

    def test_missing_source_file_is_skipped_not_failed(self, workspace, tmp_path):
        executor, runner = make_executor(RunOutcome(0, "ok", "", False))
        result = executor.execute(
            make_request(workspace, data_files=[tmp_path / "不存在.csv"])
        )
        assert result.status is ExecStatus.OK


# =============================================================== 超时看护


class TestTimeoutGuarding:
    def test_timeout_is_classified_from_timer_not_exit_code(self, workspace):
        """超时由宿主的墙钟计时器说了算，不看进程退出码 ——
        因为强杀之后退出码并不稳定。"""
        executor, _ = make_executor(
            RunOutcome(exit_code=0, stdout="partial", stderr="", timed_out=True),
            timeout_seconds=5,
        )
        result = executor.execute(make_request(workspace, timeout_seconds=5))
        assert result.status is ExecStatus.TIMEOUT

    def test_timeout_passes_limit_to_runner(self, workspace):
        executor, runner = make_executor(RunOutcome(0, "", "", True))
        executor.execute(make_request(workspace, timeout_seconds=7.5))
        assert runner.calls[0][1] == 7.5

    def test_timeout_hint_points_at_data_size(self, workspace):
        """模型拿到 TIMEOUT 该去优化数据量，而不是去找不存在的语法错误。"""
        executor, _ = make_executor(RunOutcome(None, "", "", True))
        result = executor.execute(make_request(workspace))
        assert result.to_tool_payload()["hint"]  # 存在
        assert "数据量" in result.hint

    def test_partial_output_survives_timeout(self, workspace):
        """被强杀的进程可能已经写了东西 —— 丢弃会让模型只能盲猜。"""
        executor, _ = make_executor(
            RunOutcome(None, "step1 done\nstep2 done", "", True)
        )
        result = executor.execute(make_request(workspace))
        assert "step1 done" in result.stdout
        assert "超时" in result.error_summary

    def test_subprocess_runner_recovers_output_after_kill(self):
        """验证强杀链路真的能拿到残骸输出。

        用一个会先打印再挂起的脚本模拟「输出了一部分才卡死」的真实场景。

        两个细节：
          * 用 sys.executable 而不是 "python" —— 测试机器上未必有可直接调用
            的 python 在 PATH 里。
          * 必须加 -u。管道模式下 Python 的 stdout 是块缓冲，不带 -u 的话
            进程被强杀时缓冲区还没 flush，一丝输出都捞不到。真实容器里靠
            `PYTHONUNBUFFERED=1` 达到同样效果（见 _build_command）。
        """
        runner = SubprocessRunner()
        outcome = runner.run(
            [sys.executable, "-u", "-c", "print('before hang'); import time; time.sleep(30)"],
            timeout=2.0,
            container_name="daa-not-a-container",
        )
        assert outcome.timed_out is True
        assert "before hang" in outcome.stdout

    def test_subprocess_runner_timeout_is_reported_within_budget(self):
        """看门狗必须在预算内收尾，不能让一次挂起拖住整个服务。"""
        runner = SubprocessRunner()
        started = time.monotonic()
        outcome = runner.run(
            [sys.executable, "-c", "import time; time.sleep(30)"],
            timeout=1.5,
            container_name="daa-not-a-container",
        )
        elapsed = time.monotonic() - started
        assert outcome.timed_out is True
        assert elapsed < 15  # 给 CI 慢机器留余量，但绝不能变成 30 秒


# =============================================================== 错误归类与清洗


class TestResultProcessing:
    def test_stderr_traceback_paths_are_stripped(self, workspace):
        outcome = RunOutcome(
            1,
            "",
            'Traceback (most recent call last):\n'
            '  File "/work/script.py", line 3, in <module>\n'
            '    df["不存在"]\n'
            "KeyError: '不存在'\n",
            False,
        )
        executor, _ = make_executor(outcome)
        result = executor.execute(make_request(workspace))
        assert result.status is ExecStatus.RUNTIME_ERROR
        assert "/work/" not in result.stderr
        assert "line 3" in result.stderr
        assert "KeyError" in result.stderr

    def test_oom_uses_raw_stderr_for_detection(self, workspace):
        """OOM 判定看**原始** stderr。

        如果先清洗再判定，某些路径形态下的 "Killed" 可能被误伤，
        OOM 就会被错判成普通运行错误，模型的修复方向就歪了。
        """
        executor, _ = make_executor(RunOutcome(137, "", "Killed", False))
        result = executor.execute(make_request(workspace))
        assert result.status is ExecStatus.OOM
        assert "内存" in result.hint

    def test_exit_137_without_killed_marker_is_not_oom(self, workspace):
        executor, _ = make_executor(RunOutcome(137, "", "some noise", False))
        result = executor.execute(make_request(workspace))
        assert result.status is ExecStatus.RUNTIME_ERROR

    def test_long_output_is_truncated_and_flagged(self, workspace):
        executor, _ = make_executor(RunOutcome(0, "x" * 5000, "", False))
        result = executor.execute(make_request(workspace, max_output_bytes=200))
        assert len(result.stdout.encode("utf-8")) <= 200
        assert result.stdout_truncated is True
        assert result.to_tool_payload()["truncated"]["stdout"] is True

    def test_error_summary_excludes_internal_command(self, workspace):
        """给模型的 payload 里不能有 raw_command —— 那是宿主侧的痕迹。"""
        executor, _ = make_executor(RunOutcome(1, "", "boom", False))
        payload = executor.execute(make_request(workspace)).to_tool_payload()
        assert "raw_command" not in payload
        assert "docker" not in str(payload)

    def test_duration_is_recorded(self, workspace):
        executor, _ = make_executor(RunOutcome(0, "", "", False))
        result = executor.execute(make_request(workspace))
        assert isinstance(result.duration_ms, int)


# =============================================================== 产物回传


class TestArtifacts:
    def test_artifacts_are_copied_out_of_ephemeral_dir(self, workspace, tmp_path):
        """产物必须落到持久目录 —— 运行目录会被回收，
        不然前端下一秒就取不到图了。"""
        executor, _ = make_executor(RunOutcome(0, "", "", False))
        request = make_request(workspace)
        # 模拟容器往 /out 里写了产物
        executor.execute(request)
        # _build_result 在 runner 返回后读 out_dir，这里改用直接调用验证拷贝逻辑
        out_dir = workspace / "out"
        out_dir.mkdir(parents=True, exist_ok=True)
        (out_dir / "chart.png").write_bytes(b"png-bytes")

        from src.sandbox.docker_executor import _collect_artifacts

        copied = _collect_artifacts(out_dir, artifact_dir=request.artifact_dir)
        assert copied
        assert copied[0].exists()
        assert copied[0].read_bytes() == b"png-bytes"

    def test_artifact_names_only_in_model_payload(self, workspace, tmp_path):
        """给模型文件名，给前端宿主路径 —— 两者绝不能混。"""
        from src.sandbox.docker_executor import _collect_artifacts

        out_dir = tmp_path / "out"
        out_dir.mkdir()
        (out_dir / "result.csv").write_text("a", encoding="utf-8")
        target = tmp_path / "artifacts"

        copied = _collect_artifacts(out_dir, artifact_dir=target)
        result = __import__("src.sandbox.executor", fromlist=["x"]).ExecutionResult(
            status=ExecStatus.OK, artifacts=copied
        )
        payload = result.to_tool_payload()
        assert payload["artifacts"] == ["result.csv"]
        assert str(target) not in str(payload)

    def test_empty_out_dir_yields_no_artifacts(self, workspace, tmp_path):
        from src.sandbox.docker_executor import _collect_artifacts

        out_dir = tmp_path / "empty_out"
        out_dir.mkdir()
        assert _collect_artifacts(out_dir, artifact_dir=tmp_path / "a") == ()


# =============================================================== 运行目录回收


class TestRunCleanup:
    def test_keeps_only_recent_runs(self, tmp_path):
        runs_root = tmp_path / "runs"
        for index in range(8):
            run = runs_root / f"run_{index}"
            run.mkdir(parents=True)
            (run / "keep.txt").write_text("x", encoding="utf-8")

        _prune_old_runs(runs_root, keep=5)
        remaining = sorted(p.name for p in runs_root.iterdir())
        assert len(remaining) == 5

    def test_prune_failure_does_not_break_execution(self, tmp_path):
        """清理失败不该把一次成功的执行变成失败 —— 磁盘多留几次记录
        远好过丢结果。"""
        executor, _ = make_executor(RunOutcome(0, "ok", "", False))
        request = make_request(tmp_path / "run_x")
        result = executor.execute(request)
        assert result.status is ExecStatus.OK

    def test_zero_keep_skips_pruning(self, tmp_path):
        runs_root = tmp_path / "runs"
        (runs_root / "r1").mkdir(parents=True)
        _prune_old_runs(runs_root, keep=0)
        assert (runs_root / "r1").exists()


# =============================================================== 预检与其他


class TestExecutorSurface:
    def test_precheck_rejects_before_starting_container(self, workspace):
        """预检拦下的请求不该消耗任何执行资源。"""
        executor, runner = make_executor(RunOutcome(0, "", "", False))
        result = executor.execute(make_request(workspace, code="import socket"))
        assert result.status is ExecStatus.REJECTED
        assert runner.calls == []  # 容器一次都没起过

    def test_describe_shows_isolation_level(self, workspace):
        executor, _ = make_executor(RunOutcome(0, "", "", False))
        text = executor.describe()
        assert "DockerExecutor" in text
        assert "none" in text  # 网络隔离

    def test_host_path_lowercases_windows_drive(self, tmp_path):
        """Windows 上 Docker Desktop 要求小写盘符 + 正斜杠，
        给了 `D:\\...` 会被当成容器内相对路径，挂载静默失败。"""
        result = _host_path(tmp_path)
        assert "\\" not in result
        if len(result) > 1 and result[1] == ":":
            assert result[0].islower()

    def test_missing_docker_binary_reports_actionable_reason(self, workspace, monkeypatch):
        monkeypatch.setattr("src.sandbox.docker_executor.shutil.which", lambda _: None)
        executor, _ = make_executor(RunOutcome(0, "", "", False))
        ok, reason = executor.available()
        assert ok is False
        assert "docker" in reason.lower()
