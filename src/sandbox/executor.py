"""沙箱执行层的契约定义。

这一层是整个项目的信任边界：**信任 Host，信任 Docker API，绝不信任代码**。
模型生成的 Python 代码在这里被当作纯文本搬运、投递到隔离环境里跑，
它的输出（stdout / traceback / 产物）再被结构化地捞回来。

契约层本身不含任何具体实现 —— 只定义「什么是结果」「怎么分类」「哪些字段
可以给模型看」。实现放在 `docker_executor.py` 与 `local_executor.py`。

设计要点：
  * 错误是一等公民。执行器不通过抛异常表达「代码写错了」，而是通过
    `ExecutionResult.status` 表达。抛异常只表示执行环境自身坏了。
  * 「超时」与「代码有 bug」对模型是完全不同的信号，绝不能被压成同一句
    「执行失败」—— 压在一起模型就只会瞎改，而不是去缩减数据量。
  * 给模型看的内容必须经过净化：`to_tool_payload()` 是唯一出口。
"""

from __future__ import annotations

import enum
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol, Sequence, runtime_checkable


class ExecStatus(str, enum.Enum):
    """执行结果分类。

    用 str 枚举而不是裸字符串：既让 JSON 序列化直接可用，又不会被拼写错误
    悄悄漏过（IDE 与类型检查能兜住）。
    """

    OK = "OK"
    """正常结束，退出码为 0。"""

    TIMEOUT = "TIMEOUT"
    """超过墙钟时限，由执行器从**外部**强杀。

    这是最有价值的一类信号：它告诉模型「思路可能没错，但代价太高」，
    下一轮该去优化数据量或换检索方式，而不是去找语法错误。
    """

    OOM = "OOM"
    """被内存限额杀死（cgroup OOM Kill）。

    与 TIMEOUT 分开的理由：OOM 指向「一次性把数据全读进内存」这类
    具体坏习惯，修复方向完全不同。
    """

    RUNTIME_ERROR = "RUNTIME_ERROR"
    """代码本身出错：语法错误、异常、非零退出码。

    这是唯一「模型该去改代码」的一类。
    """

    REJECTED = "REJECTED"
    """静态预检拒绝，容器根本没起。

    本质是成本优化 —— 不用为一个注定出错的请求付一次容器启动开销。
    """

    SANDBOX_ERROR = "SANDBOX_ERROR"
    """执行环境自身有问题：镜像缺失、docker 不可用、挂载失败。

    这类**不能**当作模型的问题回填 —— 模型改一百遍代码也没用。
    应该上升为系统性错误，由调用方决定是抛异常还是提示用户。
    """


# ---------------------------------------------------------------- 结果结构体


@dataclass(slots=True)
class ExecutionRequest:
    """一次执行请求。由编排层构造，执行器只读不写。"""

    code: str
    """待执行的 Python 源码。**始终当作纯文本**，绝不拼接进 shell。"""

    work_dir: Path
    """本次运行的工作目录（宿主侧），由编排层创建，形如 <tmp>/run_<uuid4>。

    执行器在其内部维护三个子目录：
        run_<id>/
        ├── script.py   待执行代码（容器内只读挂载）
        ├── data/       本次会话实际用到的数据文件（容器内只读挂载）
        └── out/        容器唯一可写目录，产物落这里
    """

    data_files: Sequence[Path] = ()
    """本次会话**实际用到**的数据文件。

    最小权限原则：只挂载这里列出的文件，绝不把整个上传目录挂进容器。
    执行器会把它们复制进 `work_dir/data/`，并做文件名归一化（见 analysis.py）。
    """

    timeout_seconds: float = 30.0
    """墙钟超时。超时判定发生在**容器/进程之外**，被执行的代码无法规避。"""

    max_output_bytes: int = 8192
    """stdout / stderr 各自的上限，超出截断。

    防「打印一百万行把日志撑爆」，也防模型基于残缺输出下结论
    （因为有 *_truncated 标记提醒它看到的不全）。
    """

    max_code_bytes: int = 20_480
    """代码长度上限。超过则 REJECTED —— 这种体量基本是模型陷入了重复生成。"""

    artifact_dir: Path | None = None
    """产物的**持久**存放目录（会话级）。

    为什么不能直接留在 work_dir：运行目录会被「保留最近 N 次」的策略回收，
    而产物是这次会话的结果，必须活得比容器久，不然前端下一秒就取不到图了。
    为 None 时产物保留在运行目录里（仅调试用）。
    """


@dataclass(slots=True)
class ExecutorConfig:
    """执行器构造参数。集中放限额，便于测试时调小。"""

    image: str = "data-analyst-sandbox:latest"
    """沙箱镜像运行期确定，容器每次新建、用完即焚，所以环境天然干净。"""

    timeout_seconds: float = 30.0
    max_output_bytes: int = 8192
    max_code_bytes: int = 20_480

    # ---- 资源限额（仅 Docker 实现使用）----
    cpus: str = "1"
    memory: str = "512m"
    """--memory 与 --memory-swap 取同值：禁掉 swap，避免靠换页拖慢整台机器
    而不是触发 OOM —— 那样会让「内存超了」变成「莫名很慢」。"""

    pids_limit: int = 64
    """防 fork 炸弹打满宿主进程表。"""

    network_mode: str = "none"
    """容器没有任何出网口。数据分析任务不需要联网，这条直接掐灭数据外泄路径。"""

    run_as_uid: int = 1000
    """容器内以非 root 运行。配合 no-new-privileges 阻断提权。"""

    container_prefix: str = "daa-run"
    """容器名前缀。用于超时后的定点强杀。"""

    keep_recent_runs: int = 5
    """保留最近多少次运行目录便于排查，更早的清理掉。"""


@dataclass(slots=True)
class ExecutionResult:
    """单次代码执行的完整结果 —— 执行器与编排层之间**唯一**的数据契约。"""

    status: ExecStatus

    # ---- 输出 ----
    stdout: str = ""
    """标准输出，已按 max_output_bytes 截断。"""

    stderr: str = ""
    """标准错误。RUNTIME_ERROR 时这里放**清洗后**的 traceback 末段。"""

    stdout_truncated: bool = False
    """stdout 是否被截断。模型据此知道自己看到的不完整。"""

    stderr_truncated: bool = False

    # ---- 产物 ----
    artifacts: tuple[Path, ...] = ()
    """执行过程中写到产物目录的文件（图表、结果 csv 等）的**宿主侧路径**。

    注意区分两个视角：给模型看的是文件名（在 to_tool_payload 里转换），
    给前端用的是这里的宿主路径。两者不要混。
    """

    # ---- 诊断 ----
    exit_code: int | None = None
    """进程退出码。被强杀或压根没启动时为 None。"""

    duration_ms: int = 0
    """墙钟耗时，用于成本与性能观测。"""

    error_summary: str = ""
    """一句话的人话总结，给人看（前端展示 / 日志）。

    与 stderr 的区别：stderr 是给模型看的原始材料，
    error_summary 是给人类看的一句话结论。
    """

    hint: str = ""
    """给模型的**下一步建议**。

    会被拼进回填消息的**末尾** —— 落点很关键，这一点在第一个项目里踩过：
    约束写在系统提示里几乎没效果，挂在紧邻生成位置的那条消息上才管用。
    """

    # ---- 原始信息（不进 Prompt）----
    raw_command: tuple[str, ...] = ()
    """实际执行的命令，仅用于日志与调试，**不**回填给模型。"""

    @property
    def ok(self) -> bool:
        return self.status is ExecStatus.OK

    def to_tool_payload(self) -> dict[str, Any]:
        """转成回填给模型的工具结果（**净化版**）。

        这是安全边界的一部分：只暴露该让模型知道的字段，
        宿主绝对路径、原始命令、容器 id 一律剥掉。
        """
        payload: dict[str, Any] = {
            "status": self.status.value,
            "stdout": self.stdout,
            "stderr": self.stderr,
            "exit_code": self.exit_code,
            "duration_ms": self.duration_ms,
        }

        # 只给文件名，不给宿主路径 —— 模型不应该知道我们机器的目录结构
        if self.artifacts:
            payload["artifacts"] = [p.name for p in self.artifacts]

        # 输出被截断时必须显式说明，否则模型会把残缺输出当全貌来下结论
        if self.stdout_truncated or self.stderr_truncated:
            payload["truncated"] = {
                "stdout": self.stdout_truncated,
                "stderr": self.stderr_truncated,
                "note": "输出超出上限已被截断，看到的内容不完整，不要据此统计行数或总数。",
            }

        # SANDBOX_ERROR 属系统性问题，把内部摘要甩给模型只会让它瞎改代码
        if self.status is not ExecStatus.SANDBOX_ERROR and self.error_summary:
            payload["error_summary"] = self.error_summary

        if self.hint:
            payload["hint"] = self.hint

        return payload


# ---------------------------------------------------------------- 执行器协议


@runtime_checkable
class Executor(Protocol):
    """代码执行器策略接口。

    实现者：`DockerExecutor`（默认）、`LocalSubprocessExecutor`（仅本地调试）。

    实现约定（所有实现都必须满足）：
      1. **不抛异常表示执行失败** —— 代码写错、超时、OOM 都通过
         `ExecutionResult.status` 表达。抛出的异常只表示执行器自身坏了。
      2. **必须强制超时**，且计时发生在被执行的代码**无法干预**的位置。
         不能在代码内部装 signal.alarm —— 那代码可以自己捕获或忽略它。
      3. **必须截断输出**，遵守 `max_output_bytes`。
      4. **返回前清理宿主侧痕迹**：超出 keep_recent_runs 的运行目录要删，
         不留下能被下次执行读到的东西。
      5. **绝不给被执行的代码提供改变执行策略的途径**。策略只读配置。
    """

    def execute(self, request: ExecutionRequest) -> ExecutionResult:
        """执行一段代码，返回结构化结果。"""
        ...

    def precheck(self, code: str) -> ExecutionResult | None:
        """静态预检。通过则返回 None；拒绝则返回 REJECTED 结果。

        单独暴露成方法（而不是藏在 execute 里）是为了：
          * 让编排层能在起容器**之前**就把废话拦掉，省一次启动开销；
          * 让预检规则可以独立单测；
          * 让 REJECTED 的次数成为可观测指标 —— 如果频繁出现，说明该去改
            Prompt，而不是放宽规则。
        """
        ...

    def available(self) -> tuple[bool, str]:
        """自检：当前环境能否使用这个执行器。返回 (是否可用, 原因)。

        Docker 实现检查 daemon 是否可达、镜像是否存在；
        本地实现检查是否处于允许的开发环境。
        """
        ...

    def describe(self) -> str:
        """一行描述，用于启动日志与 /health。

        让用户一眼看出「现在跑的到底是哪一种执行器、隔离到什么程度」，
        避免有人误以为本地模式下也做了隔离。
        """
        ...


# ---------------------------------------------------------------- 拒绝 / 错误的快捷构造


def rejection(summary: str, *, hint: str = "") -> ExecutionResult:
    """构造一个 REJECTED 结果。

    预检拒绝时容器还没起，所以 exit_code 留 None —— 这本身也是一种信息：
    调用方能据此判断「这次没有消耗任何执行资源」。
    """
    return ExecutionResult(
        status=ExecStatus.REJECTED,
        error_summary=summary,
        hint=hint,
        exit_code=None,
    )


def sandbox_error(summary: str, *, command: Sequence[str] = ()) -> ExecutionResult:
    """构造一个 SANDBOX_ERROR 结果。

    注意：这里**不给 hint**。环境坏了不是模型能修的问题，给建议只会误导它
    去改本来没错的代码。
    """
    return ExecutionResult(
        status=ExecStatus.SANDBOX_ERROR,
        error_summary=summary,
        raw_command=tuple(command),
        exit_code=None,
    )


# 导出到这里是为了让使用方只需 `from src.sandbox.executor import Executor, ...`
__all__ = [
    "ExecStatus",
    "ExecutionRequest",
    "ExecutionResult",
    "Executor",
    "ExecutorConfig",
    "rejection",
    "sandbox_error",
]
