"""Docker 沙箱执行器 —— 默认实现，也是唯一可用于实际部署的实现。

隔离靠六件事叠起来，缺一个防护面就破了：

    1. 根文件系统  --read-only        只有挂载进来的 /out 能写
    2. network     --network=none     没有任何出网口，掐灭数据外泄
    3. 资源        --cpus/--memory    触发即被杀，不会拖慢宿主机
    4. 进程数      --pids-limit       防 fork 炸弹打满宿主进程表
    5. 提权        --user + no-new-privileges
    6. 数据最小权限 只挂载本次实际用到的文件副本，且只读

超时是这个实现里最容易写错的一环，要点写在 `_wait` 与 `SubprocessRunner` 上：

    **计时必须在容器外部**。被执行的代码不可信，它完全可以捕获 signal.alarm
    或者干脆不响应用户态信号。唯一可靠的看门狗是宿主这条 Python 进程。

    **超时后还要把输出读回来**。被强杀的进程可能已经往 stdout 写了东西
    （画图前的调试打印、循环里前几轮的结果），直接丢弃会让模型只能盲猜。
    所以顺序是：kill 进程 → docker rm -f 容器 → 再 communicate() 收残骸。
"""

from __future__ import annotations

import shutil
import subprocess
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol, Sequence

from .analysis import (
    clean_traceback,
    classify_execution,
    default_hint,
    precheck_code,
    safe_target_name,
    truncate_output,
    unique_path,
)
from .executor import (
    ExecStatus,
    ExecutionRequest,
    ExecutionResult,
    Executor,
    ExecutorConfig,
    sandbox_error,
)

SCRIPT_NAME = "script.py"
"""容器内路径固定为 /work/script.py。

固定路径不是为了省事：清洗 traceback 时会把它剥成 `script.py`，
模型就能对着自己写的代码直接找到出错行 —— 路径一变，这个对应关系就废了。
"""


# =============================================================== 命令执行抽象


@dataclass(slots=True)
class RunOutcome:
    """一次命令执行的原始信号（尚未归类）。"""

    exit_code: int | None
    stdout: str
    stderr: str
    timed_out: bool


class CommandRunner(Protocol):
    """把「跑一条命令」这件事抽象出来。

    存在的意义：让 DockerExecutor 的全部业务逻辑（命令构造、归类、清洗、
    产物处理、旧目录清理）都能用桩对象测试，不需要真的装 Docker。
    真实只有 `SubprocessRunner` 这一个实现。
    """

    def run(self, command: Sequence[str], timeout: float, container_name: str) -> RunOutcome:
        """执行并返回原始结果。超时必须返回 RunOutcome(timed_out=True)，不得抛出。"""
        ...


class SubprocessRunner:
    """真实的命令执行器 —— 带墙钟超时看护。"""

    def run(
        self,
        command: Sequence[str],
        timeout: float,
        container_name: str,
    ) -> RunOutcome:
        started = time.monotonic()
        process = subprocess.Popen(
            list(command),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            # 不走 shell：代码块永远不会出现在需要 shell 解析的位置
            shell=False,
        )
        try:
            stdout, stderr = process.communicate(timeout=timeout)
            exit_code: int | None = process.returncode
            timed_out = False
        except subprocess.TimeoutExpired:
            timed_out = True
            exit_code = None
            # 下面两步都是**尽力而为**：走到这里我们已经确定「超时」这个事实，
            # 清理失败绝不能把 TIMEOUT 结果变成一次崩溃 ——
            # 那会让模型以为是自己代码崩了，方向全错。
            try:
                # ① 先杀宿主侧的 CLI。Python 在这一步**不会**替我们杀进程。
                process.kill()
            except OSError:
                pass
            try:
                # ② 再强杀容器本身。CLI 没了容器还在跑的话，超时就是假的
                #    （CPU/内存继续被占）。
                subprocess.run(
                    ["docker", "rm", "-f", container_name],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    timeout=15,
                    check=False,
                )
            except (OSError, subprocess.SubprocessError):
                # docker 不存在 / daemon 不可达：容器本来就起不来，
                # 这条路径不该影响已经确定的超时结论
                pass
            # ③ 最后收残骸：这一步拿到的输出，往往是排查「为什么会卡死」的唯一线索
            stdout, stderr = process.communicate()

        return RunOutcome(
            exit_code=exit_code,
            stdout=stdout.decode("utf-8", errors="replace"),
            stderr=stderr.decode("utf-8", errors="replace"),
            timed_out=timed_out,
        )


# =============================================================== 执行器本体


class DockerExecutor:
    """在一次性 Docker 容器里执行模型生成的 Python 代码。

    容器每次新建、用完即焚（`--rm`），接受百毫秒级的启动开销 ——
    换来的是环境绝对干净，上一次运行不会以任何形式污染下一次。
    """

    def __init__(
        self,
        config: ExecutorConfig | None = None,
        *,
        runner: CommandRunner | None = None,
    ) -> None:
        self.config = config or ExecutorConfig()
        self._runner = runner or SubprocessRunner()

    # -------------------------------------------------- Executor 协议

    def describe(self) -> str:
        cfg = self.config
        return (
            f"DockerExecutor(image={cfg.image}, network={cfg.network_mode}, "
            f"cpu={cfg.cpus}, mem={cfg.memory}, timeout={cfg.timeout_seconds}s) "
            f"-- 文件系统只读挂载、无网络、资源受限"
        )

    def available(self) -> tuple[bool, str]:
        """自检分两级，给出可行动的原因而不是笼统失败。"""
        if shutil.which("docker") is None:
            return False, "PATH 里找不到 docker 命令"

        probe = subprocess.run(
            ["docker", "info"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            timeout=15,
            check=False,
        )
        if probe.returncode != 0:
            reason = probe.stderr.decode("utf-8", errors="replace").strip()
            return False, f"Docker daemon 不可达：{reason[:200]}"

        inspect = subprocess.run(
            ["docker", "image", "inspect", self.config.image],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=30,
            check=False,
        )
        if inspect.returncode != 0:
            return False, (
                f"沙箱镜像 {self.config.image} 不存在，"
                f"请先执行 docker build -t {self.config.image} src/sandbox/image"
            )
        return True, "ok"

    def precheck(self, code: str) -> ExecutionResult | None:
        return precheck_code(code, max_code_bytes=self.config.max_code_bytes)

    def execute(self, request: ExecutionRequest) -> ExecutionResult:
        rejected = self.precheck(request.code)
        if rejected is not None:
            return rejected

        run_id = uuid.uuid4().hex[:12]
        container_name = f"{self.config.container_prefix}-{run_id}"
        work_dir = Path(request.work_dir)
        script_dir = work_dir / "work"
        data_dir = work_dir / "data"
        out_dir = work_dir / "out"

        try:
            _prepare_workspace(request, script_dir=script_dir, data_dir=data_dir, out_dir=out_dir)
            command = self._build_command(
                container_name=container_name,
                script_dir=script_dir,
                data_dir=data_dir,
                out_dir=out_dir,
            )

            started = time.monotonic()
            outcome = self._runner.run(
                command,
                timeout=request.timeout_seconds or self.config.timeout_seconds,
                container_name=container_name,
            )
            duration_ms = int((time.monotonic() - started) * 1000)

            return self._build_result(
                request=request,
                outcome=outcome,
                duration_ms=duration_ms,
                out_dir=out_dir,
                command=command,
            )
        except FileNotFoundError as exc:
            # 命令根本没找到 —— 是环境问题，不是代码写错了
            return sandbox_error(f"无法启动执行器：{exc}", command=command_preview(self.config))
        except subprocess.SubprocessError as exc:
            return sandbox_error(f"执行器异常：{exc}", command=command_preview(self.config))
        except OSError as exc:
            # 磁盘写满、没有权限这类 —— 同样不属于模型能修的范畴
            return sandbox_error(f"准备运行环境失败：{exc}")
        finally:
            # 故意**不**在这里删 work_dir：保留下来的目录是排查线上问题的
            # 唯一现场（当时跑的代码、喂进去的数据、产出的图）。
            # 回收交给 _prune_old_runs —— 按保留最近 N 次的策略统一清理。
            _prune_old_runs(work_dir.parent, keep=self.config.keep_recent_runs)

    # -------------------------------------------------- 内部实现

    def _build_command(
        self,
        *,
        container_name: str,
        script_dir: Path,
        data_dir: Path,
        out_dir: Path,
    ) -> list[str]:
        """构造 docker run 命令。

        返回 list 而不是字符串 —— 命令不经 shell，`request.code` 从头到尾
        只是一段被复制到文件里的文本，永远不会出现在这个列表里。
        """
        cfg = self.config
        return [
            "docker", "run", "--rm",
            f"--name={container_name}",
            # ---- 网络：没有任何出网口 ----
            f"--network={cfg.network_mode}",
            # ---- 资源：超了就被杀，而不是拖慢宿主机 ----
            f"--cpus={cfg.cpus}",
            f"--memory={cfg.memory}",
            # memory-swap 取同值 = 禁 swap。不禁的话「内存超了」会退化成
            # 「莫名其妙很慢」，反而掩盖问题
            f"--memory-swap={cfg.memory}",
            f"--pids-limit={cfg.pids_limit}",
            # ---- 提权：非 root + 不允许获得新特权 ----
            "--security-opt=no-new-privileges",
            f"--user={cfg.run_as_uid}",
            # ---- 文件系统：根只读，唯一可写的是 /out ----
            "--read-only",
            # 给 matplotlib / pandas 一个可写的临时区，否则连画图都跑不起来
            "--tmpfs=/tmp:rw,noexec,nosuid,size=64m",
            f"--volume={_host_path(script_dir)}:/work:ro",
            f"--volume={_host_path(data_dir)}:/data:ro",
            f"--volume={_host_path(out_dir)}:/out:rw",
            "--workdir=/out",
            # ---- 环境变量 ----
            # 注意：`python -I` 隐含 `-E`，会忽略所有 PYTHON* 环境变量，
            # 所以编码必须用命令行选项 -X utf8=1 来定，不能指望这里。
            # 下面这几条是给**被测代码里可能再起的进程**留的兜底。
            "--env=PYTHONIOENCODING=utf-8",
            "--env=PYTHONUNBUFFERED=1",
            # matplotlib / pandas 的缓存目录必须可写，容器根文件系统是只读的
            "--env=MPLCONFIGDIR=/tmp",
            "--env=HOME=/tmp",
            cfg.image,
            # -I = 隔离模式：忽略 PYTHONPATH 与用户 site-packages，
            #     让容器里的依赖图景完全由镜像决定。
            #     注意它**隐含 -E**，会一并忽略所有 PYTHON* 环境变量。
            # -X utf8=1 = 强制 UTF-8 模式（命令行选项，不受 -E 影响）。
            #     中文列名报错时，容器默认编码会把 traceback 里的中文变成问号，
            #     模型看不到列名就永远修不对 —— 这不是优化，是能否收敛的前提。
            "python", "-I", "-X", "utf8=1", "-u", f"/work/{SCRIPT_NAME}",
        ]

    def _build_result(
        self,
        *,
        request: ExecutionRequest,
        outcome: RunOutcome,
        duration_ms: int,
        out_dir: Path,
        command: Sequence[str],
    ) -> ExecutionResult:
        """把原始信号加工成结构化结果 —— 归类和清洗都在这一步。"""
        cfg = self.config

        stdout, stdout_truncated = truncate_output(outcome.stdout, request.max_output_bytes)
        # stderr 清洗后才截断：先剥路径省下来的字节，能多留几行真 traceback
        cleaned_stderr = clean_traceback(outcome.stderr)
        stderr, stderr_truncated = truncate_output(cleaned_stderr, request.max_output_bytes)

        status = classify_execution(
            exit_code=outcome.exit_code,
            stderr=outcome.stderr,  # OOM 判定看**原始** stderr，清洗会抹掉 "Killed"
            timed_out=outcome.timed_out,
        )

        artifacts = _collect_artifacts(out_dir, artifact_dir=request.artifact_dir)

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


# =============================================================== 辅助函数


def command_preview(config: ExecutorConfig) -> tuple[str, ...]:
    """失败时留一份「原本打算跑什么」的痕迹，便于排查。"""
    return ("docker", "run", "--rm", "--network=" + config.network_mode, config.image)


def _host_path(path: Path) -> str:
    """转成 Docker 能接受的宿主路径格式。

    Windows 上 Docker Desktop 要求盘符小写且用正斜杠（`d:/...`），
    直接给它 `D:\\...` 会被当成容器内的相对路径，挂载就静默失败了。
    """
    posix = Path(path).resolve().as_posix()
    if len(posix) > 1 and posix[1] == ":":
        posix = posix[0].lower() + posix[1:]
    return posix


def _prepare_workspace(
    request: ExecutionRequest,
    *,
    script_dir: Path,
    data_dir: Path,
    out_dir: Path,
) -> None:
    """把「这次执行需要的一切」落成三个目录。

    数据文件用**副本**而不是软链，两个原因：
      1. Windows 上创建符号链接默认需要管理员权限，软链方案在这台机器上不可行；
      2. 更重要的是隔离语义 —— 副本目录里只有我们明确放进去的东西，
         挂载出去的边界一目了然。
    """
    for directory in (script_dir, data_dir, out_dir):
        directory.mkdir(parents=True, exist_ok=True)

    (script_dir / SCRIPT_NAME).write_text(request.code, encoding="utf-8")

    for source in request.data_files:
        source = Path(source)
        if not source.is_file():
            continue
        target = unique_path(data_dir, safe_target_name(source))
        shutil.copy2(source, target)


def _collect_artifacts(
    out_dir: Path,
    *,
    artifact_dir: Path | None,
    exclude_names: frozenset[str] = frozenset(),
) -> tuple[Path, ...]:
    """把 /out 里的产物拷出到持久目录。

    拷出而不是就地保留：运行目录会被 `_prune_old_runs` 回收，
    而产物是这次会话的结果，该活得比容器久。

    `exclude_names` 用于排除被放进来当输入的同名文件（本地模式会把数据
    复制一份到工作目录，它们不是「产物」）。
    """
    if not out_dir.is_dir():
        return ()

    produced = sorted(
        p for p in out_dir.iterdir() if p.is_file() and p.name not in exclude_names
    )
    if not produced:
        return ()

    target_root = Path(artifact_dir) if artifact_dir else None
    copied: list[Path] = []
    for item in produced:
        if target_root is None:
            copied.append(item)
            continue
        target_root.mkdir(parents=True, exist_ok=True)
        destination = target_root / item.name
        if destination.exists():
            destination = unique_path(target_root, item.name)
        shutil.copy2(item, destination)
        copied.append(destination)
    return tuple(copied)


def _prune_old_runs(runs_root: Path, *, keep: int) -> None:
    """清理旧的运行目录，只保留最近 `keep` 个。

    按修改时间排序。清理失败不影响结果 —— 磁盘上多留几次运行记录
    远好过因为清理异常把一次成功的执行变成失败。
    """
    if keep <= 0 or not runs_root.is_dir():
        return

    candidates = [p for p in runs_root.iterdir() if p.is_dir()]
    if len(candidates) <= keep:
        return

    stale = sorted(candidates, key=lambda p: p.stat().st_mtime, reverse=True)[keep:]
    for directory in stale:
        shutil.rmtree(directory, ignore_errors=True)


def _summarize(status: ExecStatus, result: ExecutionResult, duration_ms: int) -> str:
    """生成一句人话总结。

    给日志和前端看的，所以带上耗时；与给模型看的 hint 是两回事 ——
    hint 讲「下一步怎么办」，这里讲「刚才发生了什么」。
    """
    if status is ExecStatus.TIMEOUT:
        base = f"执行超时（{duration_ms} ms 后被强杀）"
    else:
        base = {
            ExecStatus.OOM: "内存超出限额，进程被 cgroup 杀死",
            ExecStatus.RUNTIME_ERROR: "代码执行出错（非零退出）",
            ExecStatus.REJECTED: "静态预检未通过",
            ExecStatus.SANDBOX_ERROR: "执行环境异常",
            ExecStatus.OK: "",
        }[status]

    # 超时时被强杀的进程可能已经写了一些东西，最后一行错误往往是最有用的线索
    tail = ""
    if result.stderr.strip():
        tail = f"｜最后一行错误：{result.stderr.strip().splitlines()[-1][:120]}"

    return f"{base}{tail}"


__all__ = [
    "CommandRunner",
    "DockerExecutor",
    "RunOutcome",
    "SCRIPT_NAME",
    "SubprocessRunner",
]
