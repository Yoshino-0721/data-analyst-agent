"""本地子进程执行器 —— **开发期专用**，绝不用于实际部署。

它在宿主机上直接起一个 Python 子进程执行模型生成的代码。**没有文件系统
隔离、没有网络隔离、没有资源限额** —— 与之相比 DockerExecutor 的六层防护
在这里一层都没有。

提供的唯一价值：调 Prompt / 试依赖时不必等镜像重建，迭代快。

因为风险是真实的，这个类用三重约束把自己锁死：

    1. 构造即检查环境，非 dev 环境直接抛异常 —— 宁可起不来，也不降级运行。
    2. 每次实例化都打印醒目警告，避免有人以为它是等价方案。
    3. describe() 里带 UNSAFE 字样，健康检查页面上瞒不住。

即便如此，仍然保留 DockerExecutor 的三项能力（超时强杀、输出截断、独立
临时目录）—— 这是「本地调试」的下限，连这个都没有就是在裸奔了。
"""

from __future__ import annotations

import logging
import os
import subprocess
import sys
import time
import warnings
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence

from .analysis import (
    clean_traceback,
    classify_execution,
    default_hint,
    precheck_code,
    safe_target_name,
    truncate_output,
    unique_path,
)
from .docker_executor import SCRIPT_NAME, _host_path, _prune_old_runs
from .executor import (
    ExecStatus,
    ExecutionRequest,
    ExecutionResult,
    Executor,
    ExecutorConfig,
    sandbox_error,
)

logger = logging.getLogger(__name__)

WARNING_BANNER = (
    "=" * 68 + "\n"
    "⚠️  UNSAFE: 正在使用 LocalSubprocessExecutor —— 本地子进程执行器。\n"
    "    模型生成的代码将在**宿主机上直接执行**，具备：\n"
    "      · 完整的本机文件系统读写权限\n"
    "      · 完整的网络访问能力\n"
    "      · 不受限制的 CPU / 内存占用\n"
    "    它**没有** DockerExecutor 的任何一层隔离，仅用于本地调试，\n"
    "    严禁部署到任何联网或他人可访问的环境。\n" + "=" * 68
)


@dataclass(slots=True)
class RunOutcome:
    """一次命令执行的原始信号（与 docker 版同名同义，便于统一处理）。"""

    exit_code: int | None
    stdout: str
    stderr: str
    timed_out: bool


class LocalSubprocessExecutor:
    """在宿主机上直接执行代码。**生产环境禁用。**"""

    is_local = True
    """执行器自述：本机模式。上层（提示词/工具说明书）据此决定给模型的路径说法 ——
    容器里是 `/data` + `/out`，本机模式下两者都不存在，文件被复制进工作目录。
    这个标记必须留在执行器身上：真正跑代码的是它，模式判断只该有一处。"""

    def __init__(
        self,
        config: ExecutorConfig | None = None,
        *,
        env_name: str = "dev",
        allow_unsafe: bool = False,
    ) -> None:
        self.config = config or ExecutorConfig()
        self.env_name = env_name
        self._warned = False
        self._emit_warning()

        # 唯一的安全阀：非 dev 环境直接拒绝构造，而不是「跑起来但悄悄降级」
        if not allow_unsafe and env_name != "dev":
            raise RuntimeError(
                f"LOCAL 执行器无隔离（可读写全盘、可出网），"
                f"仅允许在 APP_ENV=dev 下使用；当前 APP_ENV={env_name!r}。\n"
                f"{WARNING_BANNER}"
            )

    def _emit_warning(self) -> None:
        """三重约束之二：不留余地地警告使用者。"""
        if self._warned:
            return
        self._warned = True
        logger.warning(WARNING_BANNER)
        # 同时打到 stderr 和 warnings：日志可能被关掉，但这两条不容易被忽略
        print(WARNING_BANNER, file=sys.stderr, flush=True)
        warnings.warn(
            "LocalSubprocessExecutor 无沙箱隔离，仅限本地调试使用。",
            stacklevel=3,
        )

    # -------------------------------------------------- Executor 协议

    def describe(self) -> str:
        return (
            f"[UNSAFE] LocalSubprocessExecutor(env={self.env_name}) "
            f"-- 宿主机直连执行，无文件/网络/资源隔离，仅限本地调试"
        )

    def available(self) -> tuple[bool, str]:
        """本地执行器唯一的前置条件是「确实处于开发环境」。"""
        if self.env_name != "dev":
            return False, f"LOCAL 执行器禁止在非 dev 环境使用（当前 {self.env_name!r}）"
        if not sys.executable:
            return False, "无法确定 Python 解释器路径"
        return True, "ok（但无隔离，仅调试用）"

    def precheck(self, code: str) -> ExecutionResult | None:
        return precheck_code(code, max_code_bytes=self.config.max_code_bytes)

    def execute(self, request: ExecutionRequest) -> ExecutionResult:
        rejected = self.precheck(request.code)
        if rejected is not None:
            return rejected

        work_dir = Path(request.work_dir)
        script_dir = work_dir / "work"
        data_dir = work_dir / "data"
        out_dir = work_dir / "out"

        try:
            self._prepare(request, script_dir=script_dir, data_dir=data_dir, out_dir=out_dir)

            command = self._build_command(script_dir=script_dir)
            started = time.monotonic()
            outcome = self._run(command, timeout=request.timeout_seconds, cwd=out_dir)
            duration_ms = int((time.monotonic() - started) * 1000)

            return self._build_result(
                request=request,
                outcome=outcome,
                duration_ms=duration_ms,
                out_dir=out_dir,
                command=command,
            )
        except OSError as exc:
            return sandbox_error(f"本地执行器准备失败：{exc}")
        finally:
            # 与 Docker 版一致：留给 prune 回收，让排查有现场可用
            _prune_old_runs(work_dir.parent, keep=self.config.keep_recent_runs)

    # -------------------------------------------------- 内部实现

    def _prepare(
        self,
        request: ExecutionRequest,
        *,
        script_dir: Path,
        data_dir: Path,
        out_dir: Path,
    ) -> None:
        """准备本地运行目录。

        ⚠️ 已知差异：**本地模式下没有 /data 挂载**。这一点让 exec 层面无法
        完全复现 Docker 的路径契约 —— 宿主机上不存在容器的绝对路径。

        处理方式是把数据文件额外复制一份到工作目录（out_dir），让代码用
        **相对路径**访问数据。代价是本地调好的代码路径语义与 Docker 下
        略有不同，这也是它是「调试工具」而非等价实现的又一佐证。
        """
        import shutil

        for directory in (script_dir, data_dir, out_dir):
            directory.mkdir(parents=True, exist_ok=True)

        (script_dir / SCRIPT_NAME).write_text(request.code, encoding="utf-8")

        for source in request.data_files:
            source = Path(source)
            if not source.is_file():
                continue
            name = safe_target_name(source)
            # data/ 下放一份，保持与 Docker 版一致的结构
            target = unique_path(data_dir, name)
            shutil.copy2(source, target)
            # 工作目录下再放一份，让相对路径在本地模式也读得到
            shadow = unique_path(out_dir, name)
            shutil.copy2(source, shadow)

    def _build_command(self, *, script_dir: Path) -> list[str]:
        """构造本地命令。四个参数各有不可替代的作用：

          * `-I` 隔离模式：忽略 PYTHONPATH 与用户 site-packages，依赖图景干净。
            ⚠️ 它**隐含 `-E`**，会一并忽略所有 `PYTHON*` 环境变量 ——
            所以 PYTHONIOENCODING / PYTHONUNBUFFERED 在这里全部失效。
          * `-X utf8=1` 强制 UTF-8 模式：命令行选项，不受 -E 影响。
            缺了它，Windows 上中文列名报错时 traceback 里会是一串问号。
          * `-u` 无缓冲：超时被强杀时已经写出的输出才捞得回来。
        """
        script_path = script_dir / SCRIPT_NAME
        return [sys.executable, "-I", "-X", "utf8=1", "-u", str(script_path)]

    def _run(self, command: Sequence[str], *, timeout: float, cwd: Path) -> RunOutcome:
        """带墙钟超时的本地执行 —— 这是本地模式下**唯一**保留的防护措施之一。"""
        process = subprocess.Popen(
            list(command),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            cwd=str(cwd),
            shell=False,
            env=self._child_env(),
        )
        try:
            stdout, stderr = process.communicate(timeout=timeout)
            return RunOutcome(
                exit_code=process.returncode,
                stdout=stdout.decode("utf-8", errors="replace"),
                stderr=stderr.decode("utf-8", errors="replace"),
                timed_out=False,
            )
        except subprocess.TimeoutExpired:
            try:
                process.kill()
            except OSError:
                pass
            stdout, stderr = process.communicate()
            return RunOutcome(
                exit_code=None,
                stdout=stdout.decode("utf-8", errors="replace"),
                stderr=stderr.decode("utf-8", errors="replace"),
                timed_out=True,
            )

    def _child_env(self) -> dict[str, str]:
        """子进程环境变量。

        两个都必须有：
          * `PYTHONIOENCODING=utf-8`：Windows 上子进程默认按 GBK 输出，
            中文列名在 traceback 里会变成一串问号，模型看不到列名就永远
            修不对 —— 这不是「优化」，是能不能收敛的问题。
          * `PYTHONUNBUFFERED=1`：管道模式下 stdout 是块缓冲，不带它的话
            超时被强杀时一丝输出都捞不回来。
        """
        env = dict(os.environ)
        env["PYTHONIOENCODING"] = "utf-8"
        env["PYTHONUNBUFFERED"] = "1"
        return env

    def _build_result(
        self,
        *,
        request: ExecutionRequest,
        outcome: RunOutcome,
        duration_ms: int,
        out_dir: Path,
        command: Sequence[str],
    ) -> ExecutionResult:
        """与 Docker 版共用同一套归类与清洗口径 —— 换实现不该换信号语义。"""
        from .docker_executor import _collect_artifacts, _summarize

        stdout, stdout_truncated = truncate_output(outcome.stdout, request.max_output_bytes)
        cleaned = clean_traceback(outcome.stderr)
        stderr, stderr_truncated = truncate_output(cleaned, request.max_output_bytes)

        status = classify_execution(
            exit_code=outcome.exit_code,
            stderr=outcome.stderr,
            timed_out=outcome.timed_out,
        )
        # 排掉被复制进来当输入的数据副本 —— 它们不是这次的「产物」
        input_names = frozenset(safe_target_name(Path(p)) for p in request.data_files)
        artifacts = _collect_artifacts(
            out_dir, artifact_dir=request.artifact_dir, exclude_names=input_names
        )

        result = ExecutionResult(
            status=status,
            stdout=stdout,
            stderr=stderr,
            stdout_truncated=stdout_truncated,
            stderr_truncated=stderr_truncated,
            artifacts=artifacts,
            exit_code=outcome.exit_code,
            duration_ms=duration_ms,
            hint=default_hint(status),
            raw_command=tuple(command),
        )
        if status is not ExecStatus.OK:
            result.error_summary = _summarize(status, result, duration_ms)
        return result


__all__ = ["LocalSubprocessExecutor", "RunOutcome", "WARNING_BANNER"]
