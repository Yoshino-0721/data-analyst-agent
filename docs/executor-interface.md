# Executor 抽象接口设计

> 第二个项目：基于 Function Calling 的「私人数据分析师」Agent
> 本文档给出执行层的接口契约。**先定契约，再写实现** —— 契约稳定之后，
> Docker 实现与本地实现可以并行推进，测试也能用桩对象提前写完。

---

## 1. 设计目标

1. **两种实现可互换**：`DockerExecutor`（默认，真隔离）与
   `LocalSubprocessExecutor`（备选，仅本地调试）满足同一个接口，
   上层编排代码对二者无感知。
2. **错误是一等公民**：调用方拿到的不是 `str`，而是带 `status` 的结构体。
   「超时」和「代码写错了」对模型来说是完全不同的信号，绝不能被压成同一句
   「执行失败」。
3. **不泄漏宿主细节**：返回给模型的内容里不能出现宿主机绝对路径、
   容器 id、内部环境变量。
4. **失败可归类、可测试**：所有判定逻辑（超时 / OOM / 语法错误 / 运行错误）
   都能用桩数据独立测试，不需要真的起容器。
5. **无隐藏副作用**：接口不提供「在宿主上执行命令」这类能力，
   连签名上都不给这条路。

---

## 2. 核心数据结构

```python
from __future__ import annotations

import enum
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol, Sequence


class ExecStatus(str, enum.Enum):
    """执行结果分类。

    用 str 枚举而不是裸字符串：既能让 JSON 序列化直接可用，
    又不会被拼写错误悄悄漏过（IDE 与类型检查能兜住）。
    """

    OK = "OK"
    """正常结束，exit code 为 0。"""

    TIMEOUT = "TIMEOUT"
    """超过墙钟时限，由执行器从**外部**强杀。"""

    OOM = "OOM"
    """被内存限额杀死（cgroup OOM Kill）。"""

    RUNTIME_ERROR = "RUNTIME_ERROR"
    """代码本身出错：语法错误、异常、非零退出码。这是**模型该去修**的那类。"""

    REJECTED = "REJECTED"
    """静态预检拒绝，容器根本没起。视为成本优化，不是执行失败。"""

    SANDBOX_ERROR = "SANDBOX_ERROR"
    """执行环境自身有问题：镜像缺失、docker 不可用、挂载失败。

    这类**不能**当作模型的问题回填 —— 模型改一百遍代码也没用，
    应该上升为系统性错误，让调用方决定是抛异常还是提示用户。
    """
```

```python
@dataclass(slots=True)
class ExecutionResult:
    """单次代码执行的完整结果。

    这是执行器与编排层之间**唯一**的数据契约。
    """

    status: ExecStatus

    # ---- 输出 ----
    stdout: str = ""
    """标准输出，已按 max_output_bytes 截断。"""

    stderr: str = ""
    """标准错误。RUNTIME_ERROR 时这里放**清洗后**的 traceback 末段。"""

    stdout_truncated: bool = False
    """stdout 是否被截断。让模型知道「你看到的不全」，避免基于残缺输出下结论。"""

    stderr_truncated: bool = False

    # ---- 产物 ----
    artifacts: list[Path] = field(default_factory=list)
    """执行过程中写到产物目录的文件（图表、结果 csv 等）的**宿主侧路径**。

    注意：给模型看的是文件名，给前端用的是宿主路径。两者不要混。
    """

    # ---- 诊断 ----
    exit_code: int | None = None
    """进程退出码。被强杀或未启动时为 None。"""

    duration_ms: int = 0
    """墙钟耗时，用于成本与性能观测。"""

    error_summary: str = ""
    """一句话的人话总结，用于前端展示与日志。

    与 stderr 的区别：stderr 是给模型看的原始材料，
    error_summary 是给人看的一句话结论。
    """

    hint: str = ""
    """给模型的**下一步建议**（可选）。

    例：TIMEOUT → "数据量可能过大，试试先按列筛选或分块处理"。
    这一条会被拼进回填消息的末尾 —— 位置很关键，见 4.2。
    """

    # ---- 原始信息（不进 Prompt） ----
    raw_command: tuple[str, ...] = ()
    """实际执行的命令，仅用于日志与调试，**不**回填给模型。"""

    def ok(self) -> bool:
        return self.status is ExecStatus.OK

    def to_tool_payload(self) -> dict:
        """转成回填给模型的工具结果（**净化版**）。

        这是安全边界的一部分：只暴露该让模型知道的字段，
        宿主路径、容器 id、原始命令一律剥掉。
        """
```

`to_tool_payload()` 的字段与净化规则：

| 字段 | 是否给模型 | 规则 |
|---|---|---|
| `status` | ✅ | 枚举值字符串 |
| `stdout` / `stderr` | ✅ | 已截断 + 已清洗路径 |
| `stdout_truncated` / `stderr_truncated` | ✅ | 布尔，提醒输出不全 |
| `artifacts` | ✅ | **只给文件名**，不给宿主路径 |
| `hint` | ✅ | 下一步建议 |
| `duration_ms` | ✅ | 帮助模型感知「这个做法太慢」 |
| `error_summary` | ⚠️ | 仅 `SANDBOX_ERROR` 时不给模型（属系统性问题），其余给 |
| `exit_code` | ✅ | 便于模型判断 |
| `raw_command` | ❌ | 含宿主信息，永不回填 |

---

## 3. 执行上下文与请求

```python
@dataclass(slots=True)
class ExecutionRequest:
    """一次执行请求。由编排层构造，执行器只读。"""

    code: str
    """待执行的 Python 源码。**始终当作纯文本**，绝不拼接进 shell。"""

    data_dir: Path
    """数据目录（宿主侧）。容器内只读挂载为 /data。"""

    work_dir: Path
    """本次运行的工作目录（宿主侧），由编排层创建，形如 /tmp/run_<uuid>。
    执行器在其内部准备 script.py、data/、out/ 三个子项。"""

    timeout_seconds: float = 30.0
    max_output_bytes: int = 8192
    """stdout / stderr 各自的上限，超出截断。"""


@dataclass(slots=True)
class ExecutorConfig:
    """执行器构造参数。集中放限额，便于测试时调小。"""

    timeout_seconds: float = 30.0
    max_output_bytes: int = 8192
    max_code_bytes: int = 20_480

    # 资源限额（仅 Docker 实现使用；本地实现忽略并记入警告）
    cpus: str = "1"
    memory: str = "512m"
    pids_limit: int = 64

    # 镜像与用户
    image: str = "data-analyst-sandbox:latest"
    run_as_uid: int = 1000

    keep_recent_runs: int = 5
    """保留最近多少次运行目录便于排查，更早的清理掉。"""
```

---

## 4. 接口契约

```python
class Executor(Protocol):
    """代码执行器策略接口。

    实现者：DockerExecutor（默认）、LocalSubprocessExecutor（仅本地调试）。

    实现约定（所有实现都必须满足）：
      1. **不抛异常表示执行失败** —— 代码写错、超时、OOM 都通过
         ExecutionResult.status 表达。抛出的异常只表示「执行器自身坏了」
         （对应 SANDBOX_ERROR）或调用方用法错误。
      2. **必须强制超时**，且计时发生在被执行的代码**无法干预**的位置。
      3. **必须截断输出**，遵守 max_output_bytes。
      4. **返回前清理宿主侧痕迹**：不留下能被下次执行读到的东西
         （超出 keep_recent_runs 的运行目录要删）。
      5. **绝不给被执行的代码提供改变执行策略的途径**。
    """

    def execute(self, request: ExecutionRequest) -> ExecutionResult:
        """执行一段代码，返回结构化结果。"""
        ...

    def precheck(self, code: str) -> ExecutionResult | None:
        """静态预检。通过则返回 None；拒绝则返回 REJECTED 结果。

        单独暴露成方法（而不是藏在 execute 里）是为了：
          • 让编排层能在起容器**之前**就把废话拦掉，省一次启动开销；
          • 让预检规则可以独立单测。
        """
        ...

    def available(self) -> tuple[bool, str]:
        """自检：当前环境能否使用这个执行器。

        返回 (是否可用, 原因)。Docker 实现会检查 daemon 是否可达、
        镜像是否存在；本地实现会检查是否处于允许的环境。
        """
        ...

    def describe(self) -> str:
        """一行描述，用于启动日志和 /health，让用户一眼看出
        「现在跑的到底是哪一种执行器、隔离到什么程度」。"""
        ...
```

### 4.1 为什么 `precheck` 要单独暴露

三个理由：

1. **省成本**：容器启动百毫秒起，而「代码为空」「超长」「明显引用了
   socket」这类问题在字符串层面就能发现。
2. **可测试**：预检规则会不断演化（新学到一个危险模式就加一条），
   把它做成纯函数式的独立方法，测试不需要任何桩。
3. **可观测**：拒绝次数是很有价值的指标 —— 如果 REJECTED 频繁出现，
   说明 Prompt 在诱导模型写不该写的代码，该去改 Prompt 而不是放宽规则。

### 4.2 `hint` 为什么要跟着工具结果一起回填

这一点是在第一个项目里踩坑学到的：**只在系统提示里写约束，效果远不如
把提醒挂在紧邻生成位置的那条消息上。**

当时的情况是「多诉求问题只答了一半」，在 system prompt 里加了「请回答全部
诉求」的说明几乎没效果；改成把提醒拼在**工具结果回填消息的末尾**之后就修好了。
所以这里 `hint` 的落点明确为：拼在回填消息最后一行，而不是另行发一条系统消息。

---

## 5. 两个实现的差异对照

| 维度 | DockerExecutor | LocalSubprocessExecutor |
|---|---|---|
| 文件系统隔离 | ✅ 只挂两个目录 | ❌ 能读写全盘 |
| 网络隔离 | ✅ `network=none` | ❌ 能出网 |
| 资源限额 | ✅ cgroups | ⚠️ 仅超时可控 |
| 提权防护 | ✅ 非 root + no-new-privileges | ❌ 无 |
| 超时强杀 | ✅ `docker rm -f` | ✅ `kill` 进程组 |
| 输出截断 | ✅ | ✅ |
| 启动开销 | 百毫秒级 | 毫秒级 |
| 适用场景 | **默认，任何实际使用** | 仅本地调 Prompt / 调依赖 |

**关于本地实现的硬性约束**（写进代码注释与启动日志）：

- 类名与日志都必须带 `UNSAFE` 字样，避免被误当成等价方案。
- 配置为 local 且检测到非 dev 环境时，**启动即失败**，不做降级。
- 保留超时强杀 + 输出截断 + 独立临时目录（这三项是成本下限）。

---

## 6. 上层如何选择实现（策略模式落点）

```python
def build_executor(config: Settings) -> Executor:
    """按配置选择执行器。**只读配置，不读模型输出。**"""
    if config.executor == "local":
        if not config.is_dev:
            raise RuntimeError(
                "LOCAL 执行器无隔离（可读写全盘、可出网），"
                "仅允许在 APP_ENV=dev 下使用。"
            )
        return LocalSubprocessExecutor(config.executor_config)
    return DockerExecutor(config.executor_config)
```

关键点：**策略由配置决定，模型没有任何途径影响它**。如果让模型能选
执行器（哪怕是间接的），整套隔离就是纸糊的。

---

## 7. 需要为契约写的测试（不依赖 Docker）

| 测试目标 | 做法 |
|---|---|
| `ExecStatus` 五类判定 | 给定 (exit_code, stderr 特征, 是否超时) 的桩，断言归类正确 |
| traceback 清洗 | 喂含 `/tmp/run_xxx/script.py` 的栈，断言输出里只剩 `script.py` |
| 输出截断 | 喂超长字符串，断言长度 ≤ 上限且 `*_truncated` 为真 |
| `to_tool_payload` 净化 | 断言不含 `raw_command`、不含宿主绝对路径、artifacts 只有文件名 |
| 静态预检 | 空代码 / 超长 / 黑名单关键字 → REJECTED；正常代码 → None |
| 策略选择 | 非 dev + local 配置 → 抛 RuntimeError |
| `available()` | 用桩模拟 docker 不可达，断言返回 (False, 原因) |

另有一组打标 `@pytest.mark.docker` 的真实集成测试（默认 `-m "not docker"`
跳过），只验四件事：正常执行、超时被强杀、OOM 被归类、产物能被拷出。

---

## 8. 落地进度

契约已按上述顺序实现完成：

1. ✅ `src/sandbox/executor.py` —— `ExecStatus` / `ExecutionResult` /
   `ExecutionRequest` / `ExecutorConfig` / `Executor` 协议
2. ✅ `src/sandbox/analysis.py` —— 归类、traceback 清洗、输出截断、静态预检
   （纯函数，全部用桩数据单测）
3. ✅ `src/sandbox/docker_executor.py` —— 命令构造、超时看护、结果归类、
   产物拷出、旧目录回收
4. ✅ `src/sandbox/local_executor.py` —— 本地备选实现 + 环境硬检查
5. ✅ `src/sandbox/factory.py` —— 策略选择（只读配置）
6. ✅ `src/sandbox/image/Dockerfile` —— 沙箱镜像定义

### 实现过程中修正的设计缺陷

写代码与真机验证把几个隐藏问题顶了出来，都已修掉并加了回归测试。
前三条来自实现期，后两条是**真机跑起来之后才暴露的**，格外值得记：

1. **超时强杀链路会被环境因素带崩**：原本在 `TimeoutExpired` 分支里直接调
   `docker rm -f`，若 docker 命令不存在会抛 `FileNotFoundError`，把 TIMEOUT
   变成一次崩溃 —— 模型就会以为是自己代码崩了。现在清理步骤全部包在
   try/except 里做**尽力而为**。已确定的超时事实不能被清理失败掩盖。
2. **`-I` 隐含 `-E`**：原计划用 `PYTHONIOENCODING=utf-8` 保证中文可读，
   但 `python -I`（隔离模式）会忽略所有 `PYTHON*` 环境变量，中文列名的报错
   全变成问号 —— 模型看不到列名就永远修不对。改用命令行选项 `-X utf8=1`。
3. **预检误伤合法读取**：原来的 `open('/` 粗暴匹配会把「读 `/data/销售.csv`」
   也判为危险 —— 可数据本来就在那儿，不让读等于废掉整套系统。改成按挂载
   点白名单判断：`/data` 与 `/out` 放行，其余绝对路径拒绝。
4. **OOM 的「双条件」判据是错的**（真机推翻）：原设计要求
   「exit 137 **且** stderr 含 `Killed`」才算 OOM。但 `Killed` 是 **shell**
   在子进程被 SIGKILL 时打印的，而这里是 `docker run` 直接起 python、
   中间没有 shell —— 实测 OOM 时就是 `exit_code=137` + `stderr=""`。
   按双条件会把 OOM 误判成 RUNTIME_ERROR，模型于是一头扎去改本来没写错的
   代码，真正的解法（分块读取）被完全带偏。
   **现判据**：非超时的 137 直接判 OOM（`docker run --rm` 场景下，
   非超时 SIGKILL 只可能来自 cgroup OOM killer，我们自己只在超时时 kill，
   而那时 `timed_out` 已是 True）；stderr 关键词改为兜住另一条路径 ——
   Python 接住 `MemoryError` 后正常退出（exit 1）。
5. **裸 `"oom"` 造成子串误伤**（真机推翻）：判定词表里放了裸 `oom`，
   子串匹配一上就把 `boom` / `room` / `zoom` 全吃进来，任何含这类词的
   普通报错都被报成 OOM。现在只用完整短语或带连字符的形式
   （`out of memory`、`cannot allocate memory`、`oom-kill`）。

### 真实集成测试

`tests/test_docker_integration.py` 打标 `@pytest.mark.docker`，默认被
`-m "not docker"` 跳过（CI/没装 Docker 的机器不受影响），需要时用
`pytest -m docker` 单独跑。它不做配置检查，只做**实证**，覆盖：
正常执行、中文可读、pandas 可用、traceback 清洗、四项隔离实证、最小权限挂载、
超时强杀且无残留容器、OOM 归类、产物拷出、matplotlib 出图。

其中隔离相关的用例都写成**两段式**，这个结构本身就是一条经验：

> 静态预检挡在容器前面。如果只写「正常写法」的用例（如 `import socket`），
> 实际验到的永远是预检，容器那层反而没人守、哪天坏了也不知道。
> 所以每个风险点验两遍：先断言预检会拦，再用预检查不见的等价写法
> （`importlib.import_module('soc' + 'ket')`、路径拼接）去撞容器，
> 确认第二道门自己站得住。

### 下一步

前端沿用第一个项目的 HTML 基础，加两块：**代码展示区**（把模型生成的代码
原样展示，让用户看得见它要跑什么）与 **ECharts 图表区**（渲染产物）。
上层编排（Schema 提取 + 手写 Function Calling 循环）尚未开始。

跑 `python scripts/demo_loop.py` 可以看到「写错 → 结构化反馈 → 修好」的
完整闭环演示。
