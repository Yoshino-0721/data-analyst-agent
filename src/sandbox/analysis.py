"""执行结果的判定与净化逻辑（纯函数，无 I/O）。

抽成独立模块有两个理由：
  1. 这些函数是整套「错误分类」规则的落点，最容易出错也最需要细测 —— 抽出来
     就能用纯数据单测，不需要真的起容器。
  2. Docker 实现与本地实现共用同一套归类与清洗逻辑，保证「跑在容器里」和
     「跑在本地」给模型返回的信号口径一致。

归类为什么值得单独做成一组规则：拿到这个信号去用的人是模型，它拿到什么信号就会
往什么方向改。把 TIMEOUT 误报成 RUNTIME_ERROR，模型会去找不存在的语法错误；
把 OOM 误报成 TIMEOUT，模型会去做无谓的分块。信号的准确性直接决定下一轮
能不能修对。
"""

from __future__ import annotations

import re

from .executor import ExecStatus, ExecutionResult

# ---------------------------------------------------------------- 输出截断


def truncate_output(text: str, max_bytes: int) -> tuple[str, bool]:
    """把输出截断到 `max_bytes` 字节，返回 (结果, 是否被截断)。

    按**字节**而不是字符计数：一个中文字符占 3 字节，按字符截断会低估
    实际体积，日志照样能被撑爆。

    截断时不切断多字节字符 —— 用 errors="ignore" 丢掉尾巴上的半个字，
    总好过得到一段 UnicodeDecodeError 的解码垃圾。
    """
    if max_bytes <= 0:
        return "", bool(text)

    raw = text.encode("utf-8")
    if len(raw) <= max_bytes:
        return text, False

    return raw[:max_bytes].decode("utf-8", errors="ignore"), True


# ---------------------------------------------------------------- traceback 清洗

# 形如 /tmp/run_ab12/script.py、C:\Users\x\AppData\Local\Temp\run_x\out\plot.png
_TMP_DIR_PATTERN = re.compile(
    r"[A-Za-z]:?[\\/]*(?:[^\s\"']*?)[\\/]run_[0-9a-fA-F-]{8,}[\\/]"
)

# Python traceback 的典型行：  File "/abs/path/script.py", line 12, in <module>
_FILE_LINE_PATTERN = re.compile(r'(File\s+")([^"]+)(",\s*line\s+\d+)')


def clean_traceback(stderr: str, *, max_lines: int = 30) -> str:
    """把 traceback 里的宿主绝对路径剥掉，只留下对模型有用的部分。

    做这件事有两个原因，第二个比第一个更重要：

    1. **安全**：不该把宿主机的目录结构（用户名、临时目录布局）泄漏给模型。
    2. **可用**：路径越长，模型越容易把注意力浪费在它看不懂的路径上。
       而且 paths 被剥成 `script.py` 之后，行号与文件名对模型来说才是可读的
       —— 它能直接对着自己写的代码找到出错的那一行。

    保留**末段**而不是开头：Python traceback 的最后几行才是异常类型与消息，
    前面的调用栈对模型改代码几乎没有帮助。
    """
    if not stderr.strip():
        return ""

    lines = [line for line in stderr.splitlines() if line.strip()]
    tail = lines[-max_lines:]

    cleaned: list[str] = []
    for line in tail:
        cleaned.append(_basename_paths(line))

    result = "\n".join(cleaned).rstrip()
    if len(lines) > max_lines:
        result = f"...（前面 {len(lines) - max_lines} 行已省略）\n" + result
    return result


def _basename_paths(line: str) -> str:
    """把一行里的绝对路径替换成纯文件名。"""
    # 先精确处理 traceback 的标准格式 File "xxx", line N
    def _repl(m: re.Match[str]) -> str:
        path = m.group(2).replace("\\", "/")
        return f'{m.group(1)}{path.rsplit("/", 1)[-1]}{m.group(3)}'

    line = _FILE_LINE_PATTERN.sub(_repl, line)
    # 再兜底处理其他形态的临时运行目录路径
    line = _TMP_DIR_PATTERN.sub("", line)
    return line


# ---------------------------------------------------------------- 结果归类

# cgroup OOM Kill 在 stderr 里通常没有任何 Python 输出，只有 Shell 层的 "Killed"；
# Docker 也可能把容器层日志带出来
_OOM_MARKERS = ("killed", "memoryerror", "out of memory", "oom")


def classify_execution(
    *,
    exit_code: int | None,
    stderr: str,
    timed_out: bool,
) -> ExecStatus:
    """把一次执行的原始信号归成 ExecStatus。

    判定顺序很重要：**先看是否超时**。TIMEOUT 是由宿主的墙钟计时器判定的，
    属于「我们自己掌握的真相」，比 exit_code 可靠 —— 强杀之后容器/进程返回
    什么退出码并不稳定。所以超时优先，不用退出码去反推。

    OOM 需要双条件（137 + stderr 特征）：137 也可能来自其他强杀场景。拿不准
    时宁可归为 RUNTIME_ERROR 并附原始 stderr，让模型看到真实材料自己判断，
    也好过一个自信但错误的分类把它带偏。
    """
    if timed_out:
        return ExecStatus.TIMEOUT

    if exit_code == 0:
        return ExecStatus.OK

    if exit_code == 137:
        lowered = stderr.lower()
        if any(marker in lowered for marker in _OOM_MARKERS):
            return ExecStatus.OOM

    return ExecStatus.RUNTIME_ERROR


def default_hint(status: ExecStatus) -> str:
    """按分类给出默认修复建议。

    这些是从「模型常犯的错」里总结出来的：TIMEOUT 通常是把全量数据拉进来算，
    OOM 通常是一次性 read_csv 大文件，RUNTIME_ERROR 里最常见的是列名猜错。
    给方向，而不是给答案 —— 具体怎么改仍然是模型的事。
    """
    return {
        ExecStatus.OK: "",
        ExecStatus.TIMEOUT: (
            "执行超时。常见原因是数据量过大或存在低效循环 —— "
            "试试先筛选列/行、用 pandas 向量化运算替代 for 循环，或减少画图点数。"
        ),
        ExecStatus.OOM: (
            "内存超限被杀。常见原因是一次性把整个文件读进内存 —— "
            "试试 read_csv(usecols=...) 只读需要的列、chunksize 分块读取，"
            "或先聚合再处理。"
        ),
        ExecStatus.RUNTIME_ERROR: (
            "代码执行出错。若为 KeyError/列名相关错误，"
            "先用 get_schema 确认列名与实际数据一致。"
        ),
        ExecStatus.REJECTED: "请参考拒绝原因重写代码。",
        ExecStatus.SANDBOX_ERROR: "",
    }[status]


# ---------------------------------------------------------------- 静态预检

# 这些是被限制/常用的入口。注意：**黑名单只是提示层，不是安全防线** ——
# 真正的防线是容器的 network=none 与只读挂载。这里做拦截是为了省一次容器
# 启动开销、并给模型一个明确的「别这么写」的信号。
_RESTRICTED_IMPORTS = (
    "socket",
    "requests",
    "urllib",
    "httpx",
    "subprocess",
    "ctypes",
    "pty",
    "multiprocessing",
    "shutil",
)

_IMPORT_PATTERN = re.compile(r"^\s*(?:from|import)\s+([A-Za-z_][\w.]*)", re.MULTILINE)

# 不做 open('/ 这种粗暴匹配 —— 那会把「读取挂载进来的数据文件」一起拦掉，
# 而那恰恰是这个系统存在的原因（数据就在 /data 下，不读它读什么）。
# 真正的规则是：**只允许访问两个挂载点**，别的绝对路径一律拒绝。
_OPEN_CALL_PATTERN = re.compile(r"""open\(\s*['"]([^'"]+)['"]""")

# 容器内唯一存在的两个挂载点
_ALLOWED_MOUNT_PREFIXES = ("/data", "/out")
_WINDOWS_DRIVE_PATTERN = re.compile(r"^[A-Za-z]:/")

_HARD_PATTERNS = (
    ("os.system", "不要调用 os.system 执行系统命令"),
    ("os.remove", "不要删除数据目录中的文件"),
    ("rmtree", "不要递归删除目录"),
    ("eval(", "不要使用 eval"),
    ("exec(", "不要使用 exec"),
    ("__import__", "不要使用 __import__ 动态加载模块"),
)


def _check_file_access(code: str) -> str | None:
    """检查文件读写是否只落在允许的挂载点内。

    这是「最小权限」在代码层的对应物：容器里除了 /data 与 /out，
    其他路径理论上也不存在（且根文件系统只读），这里是提前给出明确反馈，
    让模型不必浪费一轮执行去撞墙。

    用 mount 之外的绝对路径（如 /etc/passwd、C:\\Windows）→ 拒绝；
    访问 /data 下的数据文件 → 放行，这是正常的分析行为；
    相对路径 → 放行，工作目录是 /out，落点必然在沙箱内。
    """
    for raw_path in _OPEN_CALL_PATTERN.findall(code):
        path = raw_path.replace("\\", "/")

        if path.startswith("..") or "/../" in path:
            return f"不允许通过 .. 访问沙箱之外的路径：{raw_path}"

        # Windows 盘符路径 = 试图摸宿主机 —— 容器里不存在这些数据
        if _WINDOWS_DRIVE_PATTERN.match(path):
            return f"不允许访问主机绝对路径：{raw_path}"

        if path.startswith("/"):
            if not path.startswith(_ALLOWED_MOUNT_PREFIXES):
                return f"只允许读写 /data 与 /out 两个挂载目录，不得访问：{raw_path}"

    return None


def precheck_code(
    code: str,
    *,
    max_code_bytes: int = 20_480,
) -> ExecutionResult | None:
    """静态预检。通过返回 None，拒绝返回 REJECTED 结果。

    这一层**快速失败**的价值在于：一次容器启动是百毫秒级开销，而下面这些
    问题在字符串层面就能发现。更重要的是它给出了比「执行失败」具体得多的
    反馈 —— 明确告诉模型哪一行、哪个模块不该用。
    """
    from .executor import rejection  # 局部导入避免循环依赖

    stripped = code.strip()
    if not stripped:
        return rejection("代码为空。", hint="请写出完整的可执行 Python 代码。")

    # 纯注释 / 只有文档字符串：不值得起容器
    meaningful = [
        line
        for line in stripped.splitlines()
        if line.strip() and not line.strip().startswith("#")
    ]
    if not meaningful:
        return rejection("代码中只有注释，没有可执行的语句。")

    raw = code.encode("utf-8")
    if len(raw) > max_code_bytes:
        return rejection(
            f"代码长度 {len(raw)} 字节，超过上限 {max_code_bytes} 字节。",
            hint="通常是陷入了重复生成。请精简代码，只保留必要的分析步骤。",
        )

    for module in _IMPORT_PATTERN.findall(code):
        root = module.split(".", 1)[0]
        if root in _RESTRICTED_IMPORTS:
            return rejection(
                f"不允许 import {root}：沙箱内无网络，也不允许操作进程与文件系统。",
                hint=f"请改用 pandas / numpy / matplotlib 等数据分析库实现。",
            )

    for pattern, reason in _HARD_PATTERNS:
        if pattern in code:
            return rejection(f"检测到受限写法：{reason}。", hint="请改用沙箱允许的方式实现。")

    access_violation = _check_file_access(code)
    if access_violation:
        return rejection(
            access_violation,
            hint="数据文件在 /data 下，分析结果请写到 /out（相对路径默认是 /out）。",
        )

    return None


# ---------------------------------------------------------------- 文件名归一化


def safe_target_name(source: Path) -> str:
    """把源文件名归一化成一个可安全放进挂载目录的纯文件名。

    这条是**数据挂载最小权限**的落地关键之一：被分析的文件名可能来自用户
    上传，形如 `../../etc/passwd`。如果直接拼接，`..` 会让这个「副本」落到
    挂载目录之外，等于白做了隔离。取 basename 之后，无论传进来什么路径，
    落点都只会在 data/ 目录内部。

    冲突时加短后缀避免相互覆盖（同名文件可能来自不同子目录）。
    """
    name = source.name.strip()
    if not name or name in {".", ".."}:
        name = "data"
    # 再兜一层：即便 basename 里还带着分隔符也要清掉
    name = name.replace("/", "_").replace("\\", "_")
    return name or "data"


def unique_path(directory: Path, name: str) -> Path:
    """在 directory 里给 name 找一个不冲突的路径。"""
    target = directory / name
    if not target.exists():
        return target

    stem, suffix = (name.rsplit(".", 1) + [""])[:2] if "." in name else (name, "")
    dotted = f".{suffix}" if suffix else ""
    for index in range(2, 100):
        candidate = directory / f"{stem}_{index}{dotted}"
        if not candidate.exists():
            return candidate
    return directory / f"{stem}_{id(name) % 10000}{dotted}"


__all__ = [
    "clean_traceback",
    "classify_execution",
    "default_hint",
    "precheck_code",
    "safe_target_name",
    "truncate_output",
    "unique_path",
]
