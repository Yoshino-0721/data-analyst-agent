"""工具定义与执行。

这个模块最关键的决策：**`run_python` 的入参只有 `code`，没有文件路径。**

如果给模型一个 `data_path` 参数，它迟早会猜 —— 猜 `D:\\data\\sales.csv`、
猜 `./uploads/xxx.csv`。猜错只是跑不通，真正危险的是它会**照着这个思路去读
宿主机上的其他文件**。而文件能不能读到，本来就该由编排层挂载清单决定，
不是模型该操心的事。

所以路径只在系统提示里出现一次：「你当前可以访问的文件有：/data/sales.csv」。
模型照抄即可，没有任何发挥空间。

工具说明书也写得比通常更死板：把「能用哪些库、能写哪里、不能做什么」
全部写死在 description 里。这部分 token 花得值 —— 与其让模型试错三轮，
不如第一轮就说清楚。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Sequence

from ..sandbox.executor import ExecutionRequest, ExecutionResult, Executor
from ..schema.extractor import DatasetSchema

logger = logging.getLogger(__name__)

RUN_PYTHON = "run_python"
GET_SCHEMA = "get_schema"

LOCAL_MODE_HINT = (
    "\n注意：当前运行在**本机调试模式**（没有容器挂载，也没有 `/data`、`/out`）—— "
    "数据文件就在你的工作目录里，直接用文件名读取（如 `pd.read_excel('销售.xlsx')`）；"
    "写文件也用相对路径。**不要用 `os.walk('/')` 之类的方式满盘找文件**："
    "本机模式下那会扫到整个磁盘，既慢又会撞上执行超时。"
)

# ---------------------------------------------------------------- 字体兜底
#
# 为什么光靠「说明书里写一条」不够：2026-09-15 实测，模型在系统提示与工具说明书中
# 都看到了「YaHei 排最前」的建议，仍然照抄自己上一轮的 `['SimHei', ...]`，
# 于是 `HUNKEMÖLLER` 的 Ö 真的渲染成了方块（探针复现：SimHei 在前必报
# `Glyph 214 missing`；YaHei 在前只剩 emoji 一条）。
# 按本项目自己的经验（§2.7 第 4 条）：**约束挂在回填消息末尾才有效** ——
# 所以这里改成「从真实 stderr 里看见缺字形，再回一条末尾提示」。

FONT_GLYPH_MARKERS = ("missing from font(s)", "Failed to find font weight bold")
"""matplotlib 缺字形 / 缺粗体字重时会打的告警。**用结果当证据**，
而不是去正则匹配模型的写法 —— 不管它是设 rcParams 还是给单个 Text 传 fontproperties。"""

LOCAL_FONT_HINT = (
    "字体：把 `font.sans-serif` 设成 `['Microsoft YaHei', 'SimHei', 'DejaVu Sans']` —— "
    "**YaHei 必须在最前**（本机 SimHei 没有粗体字重、也缺 Ö 这类字符，会显示成方块）；"
    "另外图里**不要**放 emoji（本机没有 emoji 字体，同样是方块）。"
)
CONTAINER_FONT_HINT = (
    "字体：**不要**自己设 `font.sans-serif` —— 镜像的 matplotlibrc 已经配好中文字体"
    "（Noto Sans CJK），自己写会把默认值顶掉、中文反而变方块。"
)


def font_glyph_hint(stderr: str, *, local: bool) -> str:
    """本次执行真的缺字形时，给一条挂末尾的字体提示；否则返回空串（不打扰）。"""
    if not stderr or not any(marker in stderr for marker in FONT_GLYPH_MARKERS):
        return ""
    return LOCAL_FONT_HINT if local else CONTAINER_FONT_HINT


# ---------------------------------------------------------------- 布局兜底
#
# 与字体同一条思路：从 stderr 里取证据。这次是 matplotlib 的一句警告 ——
# 模型既用 `fig.add_axes([...])` 手工摆 KPI 卡片、又调 `plt.tight_layout()`，
# 后者管不到手工定位的 Axes，于是**每张图**都带一句
# `This figure includes Axes that are not compatible with tight_layout`。
# 它不影响数值，但布局确实可能不准，而且让"警告"和"报错"混在一起。
TIGHT_LAYOUT_MARKER = "not compatible with tight_layout"

LAYOUT_HINT = (
    "布局：这张图用了 `fig.add_axes([...])` 手工定位，就**不要**再调 `plt.tight_layout()` —— "
    "它管不到手工定位的 Axes（matplotlib 因此警告 results might be incorrect）。"
    "改用 `fig.subplots_adjust(top=…, bottom=…)` 自己留白即可。"
)


def layout_hint(stderr: str) -> str:
    """matplotlib 抱怨 tight_layout 与手工定位的 Axes 冲突时，回一条布局提示。"""
    if not stderr or TIGHT_LAYOUT_MARKER not in stderr:
        return ""
    return LAYOUT_HINT



# ------------------------------------------------------------------ 说明书


def build_tool_schemas(*, local: bool = False) -> list[dict[str, Any]]:
    """给模型的 JSON Schema。

    注意 description 的写法：**先说清楚能做什么，再划出红线**。
    模型对「不要做什么」的遵守度，取决于它是否知道替代方案 ——
    所以每条禁令后面都给了该怎么做。

    Args:
        local: 本地调试执行器模式。数据路径的说法必须跟着执行器走：
            容器里是 `/data` + 工作目录 `/out`，本地模式下两者都不存在，
            文件被复制进工作目录、用相对名读。2026-09-15 的事故就是
            「提示说 /data、实现放工作目录」，模型第一次读必失败。
    """
    # 第 2 条规则是**唯一**随模式变化的措辞：容器里工作目录就是 /out，
    # 本机模式下没有这个绝对路径。用三元式显式拼，不做「替换自己的字符串」
    # —— 那种写法一旦原文改了就静默不命中，正好是 §5.1 记过的坑。
    work_dir_rule = (
        "2. 工作目录是你唯一可写的地方 —— 直接用相对路径写文件"
        "（如 plt.savefig('chart.png')）；\n"
        if local
        else "2. 工作目录就是 /out —— 直接用相对路径写文件"
        "（如 plt.savefig('chart.png')），只有 /out 可写；\n"
    )
    # 第 6 条同样随模式变化（§2.5 记的第二处语义差异）：容器镜像改好了
    # matplotlibrc，本机什么都没有。措辞之外还有一道运行时兜底 ——
    # 真缺字形时由 `font_glyph_hint()` 把提示挂在回填消息末尾（见下方说明）。
    font_rule = (
        "6. 画图要用中文字体时，把 font.sans-serif 设成 "
        "['Microsoft YaHei', 'SimHei', 'DejaVu Sans'] —— **YaHei 必须在最前**"
        "（SimHei 没有粗体字重、也缺 Ö 这类字符）；图里**不要**放 emoji（本机没有 emoji 字体）。\n"
        if local
        else "6. 画图**不要**自己设 font.sans-serif：镜像里的 matplotlibrc 已经配好中文字体"
        "（Noto Sans CJK）；容器里没有 SimHei 这类 Windows 字体，写了会顶掉默认值、"
        "中文反而变方块。\n"
    )
    run_python_description = (
        "在隔离沙箱里执行一段 Python 代码，用于读取和分析数据。\n"
        "环境里有 pandas / numpy / matplotlib / openpyxl，**没有网络**。\n"
        "规则：\n"
        "1. 数据文件的路径请**照抄**系统提示里给出的那个，不要自行猜测或拼接其它路径；\n"
        + work_dir_rule
        + "3. 用 print() 输出关键结论，只有 stdout 会回传给你；\n"
        "4. 不要用 subprocess / socket / requests / os.system，也不要读写数据文件与工作目录之外的路径；\n"
        "5. 中文输出正常，无需额外设置编码。\n"
        + font_rule.rstrip("\n")
    )
    if local:
        run_python_description += LOCAL_MODE_HINT

    return [
        {
            "type": "function",
            "function": {
                "name": RUN_PYTHON,
                "description": run_python_description,
                "parameters": {
                    "type": "object",
                    "properties": {
                        "code": {
                            "type": "string",
                            "description": "完整的 Python 源码。必须包含 print() 输出结论。",
                        }
                    },
                    "required": ["code"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": GET_SCHEMA,
                "description": (
                    "查看数据文件的列名与类型。\n"
                    "当你**不确定列名**、或怀疑之前的列名猜错了时**先调用它**，"
                    "不要凭记忆写代码 —— 列名写错是最常见的失败原因。\n"
                    "不传 file_name 时返回所有文件的概览；传入则返回该文件的**完整**列清单"
                    "（系统提示里可能因列数过多而被截断）。"
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "file_name": {
                            "type": "string",
                            "description": "要查询的文件名，如 sales.csv。留空则返回全部文件概览。",
                        }
                    },
                    "required": [],
                },
            },
        },
    ]


# ------------------------------------------------------------------ 执行结果


@dataclass
class ToolOutcome:
    """一次工具执行的产出。"""

    text: str
    """回填给模型的**完整文本**。结构固定：结果在前，hint 在最后。"""

    status: str | None = None
    """仅 run_python 有：OK / TIMEOUT / OOM / RUNTIME_ERROR / REJECTED / SANDBOX_ERROR。"""

    fingerprint: str | None = None
    """失败指纹。同一处错误反复出现时指纹相同，循环层据此防撞墙。"""

    artifacts: tuple[str, ...] = ()
    """本次产出的文件名（只有文件名，不含宿主路径 —— 净化在 to_tool_payload 里做完了）。"""

    # 以下三个是给**前端**用的结构化字段。
    # 模型看的是 text（一段拼好的文本），前端要的是能分栏折叠的结构 ——
    # 两者需要的东西不一样，所以同时保留，而不是让前端去解析文本。
    stdout: str = ""
    stderr: str = ""
    hint: str = ""


def failure_fingerprint(payload: dict[str, Any]) -> str | None:
    """给一次失败算指纹，用于识别「模型在撞同一堵墙」。

    指纹只取**错误位置与异常信息**，不含具体数值 —— 模型换了个写法但
    错在同一处（比如还是拼错列名），指纹仍然相同，这正是我们要识别的情况。
    """
    status = payload.get("status")
    if status == "OK":
        return None

    parts = [str(status)]
    summary = str(payload.get("error_summary") or "").strip()
    if summary:
        parts.append(summary[:120])

    stderr = str(payload.get("stderr") or "")
    lines = [line.strip() for line in stderr.splitlines() if line.strip()]
    if lines:
        # traceback 的最后一行是异常本体，最能代表「错在哪」
        parts.append(lines[-1][:200])
    return "|".join(parts) if len(parts) > 1 else parts[0]


# ------------------------------------------------------------------ 运行时


@dataclass
class ToolRuntime:
    """把工具调用分发到真正的实现上。"""

    executor: Executor
    schemas: Sequence[DatasetSchema] = ()
    data_files: Sequence[Path] = ()
    artifact_dir: Path | None = None
    run_dir_factory: Callable[[], Path] | None = None
    timeout_seconds: float = 30.0
    max_output_bytes: int = 8192
    _by_name: dict[str, DatasetSchema] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self._by_name = {schema.file_name: schema for schema in self.schemas}

    @property
    def local_executor(self) -> bool:
        """当前执行器是不是本地调试模式（决定提示词里的路径说法）。

        问**执行器自己**，不问配置：真正跑代码的是它，模式判断只有一处。
        测试里的桩执行器没有 `is_local`，一律按容器语义处理 ——
        这正是既有断言（含 `/data/...`）继续成立的原因。
        """
        return bool(getattr(self.executor, "is_local", False))

    def execute(self, call: Any) -> ToolOutcome:
        """执行一个工具调用。**任何异常都不该让循环崩掉**。"""
        name = getattr(call, "name", "")
        arguments = call.parsed_arguments() if hasattr(call, "parsed_arguments") else {}

        if name == RUN_PYTHON:
            return self._run_python(arguments)
        if name == GET_SCHEMA:
            return self._get_schema(arguments)

        known = f"{RUN_PYTHON} / {GET_SCHEMA}"
        return ToolOutcome(
            text=f"未知工具：{name or '(空)'}。可用工具只有：{known}。",
            status=None,
        )

    # ---- run_python ----

    def _run_python(self, arguments: dict[str, Any]) -> ToolOutcome:
        code = str(arguments.get("code") or "").strip()
        if not code:
            return ToolOutcome(
                text="run_python 缺少 code 参数 —— 请提供完整的 Python 源码。",
                status="REJECTED",
            )

        if self.run_dir_factory is None:
            raise RuntimeError("ToolRuntime 缺少 run_dir_factory，无法为本次执行创建工作目录")

        work_dir = self.run_dir_factory()
        request = ExecutionRequest(
            code=code,
            work_dir=work_dir,
            data_files=self.data_files,
            timeout_seconds=self.timeout_seconds,
            max_output_bytes=self.max_output_bytes,
            artifact_dir=self.artifact_dir,
        )

        result: ExecutionResult = self.executor.execute(request)
        payload = result.to_tool_payload()
        stderr_text = str(payload.get("stderr") or "")

        # 提示**顺序刻意**：执行层给的（列名/超时那类）在前，字体与布局兜底在后 ——
        # `_render_payload` 会把 hint 摆在整条消息的最后，最后读到的最影响下一段代码。
        hint = "\n".join(
            part
            for part in (
                str(payload.get("hint") or "").strip(),
                font_glyph_hint(stderr_text, local=self.local_executor),
                layout_hint(stderr_text),
            )
            if part
        )

        return ToolOutcome(
            text=_render_payload({**payload, "hint": hint}),
            status=str(result.status.value),
            fingerprint=failure_fingerprint(payload),
            # 注意键名是 artifacts（与 ExecutionResult.to_tool_payload 对齐），
            # 值已经过净化，只有文件名、不含宿主路径。
            artifacts=tuple(payload.get("artifacts") or ()),
            stdout=str(payload.get("stdout") or ""),
            stderr=stderr_text,
            hint=hint,
        )

    # ---- get_schema ----

    def _get_schema(self, arguments: dict[str, Any]) -> ToolOutcome:
        wanted = str(arguments.get("file_name") or "").strip()

        if not self.schemas:
            return ToolOutcome(text="当前会话没有已加载的数据文件。", status=None)

        if wanted:
            schema = self._by_name.get(wanted)
            if schema is None:
                available = "、".join(self._by_name)
                return ToolOutcome(
                    text=f"没有名为 {wanted!r} 的文件。当前可访问：{available}。"
                    " 请照抄系统提示中给出的文件名。",
                    status=None,
                )
            return ToolOutcome(text=_full_column_listing(schema), status=None)

        lines = []
        for schema in self.schemas:
            lines.append(
                f"- {schema.file_name}：{schema.n_rows} 行 × {schema.n_cols} 列，"
                f"路径 {schema.data_path}"
            )
        lines.append("")
        lines.append("传入 file_name 可查看该文件的完整列清单。")
        return ToolOutcome(text="\n".join(lines), status=None)


# ------------------------------------------------------------------ 渲染


def _render_payload(payload: dict[str, Any]) -> str:
    """把执行结果渲染成回填文本。

    **顺序是刻意的：hint 永远在最后。**
    项目一实测过：把约束放在系统提示里几乎无效，挂在最靠近生成位置的
    那条消息末尾才有效 —— 模型生成下一段代码时，最后读到的是什么，
    什么就最影响它。
    """
    lines: list[str] = []
    lines.append(f"status: {payload.get('status')}")

    stdout = str(payload.get("stdout") or "").strip()
    if stdout:
        lines.append(f"stdout:\n{stdout}")

    stderr = str(payload.get("stderr") or "").strip()
    if stderr:
        lines.append(f"stderr:\n{stderr}")

    artifacts = payload.get("artifacts") or ()
    if artifacts:
        lines.append("artifacts: " + ", ".join(str(name) for name in artifacts))

    truncated = payload.get("truncated") or {}
    if any(truncated.values()):
        which = "、".join(key for key, value in truncated.items() if value)
        lines.append(f"[注意] 以下字段因超长被截断：{which}。结论请基于已看到的部分。")

    hint = str(payload.get("hint") or "").strip()
    if hint:
        lines.append(f"hint: {hint}")

    return "\n".join(lines)


def _full_column_listing(schema: DatasetSchema) -> str:
    """完整列清单 —— 不受系统提示里 MAX_PROMPT_COLUMNS 的截断影响。"""
    lines = [f"# {schema.file_name}（{schema.data_path}）",
             f"{schema.n_rows} 行 × {schema.n_cols} 列", ""]
    for column in schema.columns:
        detail = f"- `{column.name}` {column.dtype}  缺失 {column.null_count} ({column.null_ratio:.1%})  唯一 {column.unique_count}"
        if column.stats:
            stats = " ".join(f"{k}={_fmt(v)}" for k, v in column.stats.items())
            detail += f"  {stats}"
        if column.examples:
            detail += "  例：" + " | ".join(column.examples)
        lines.append(detail)
    return "\n".join(lines)


def _fmt(value: Any) -> str:
    if value is None:
        return "—"
    if isinstance(value, float):
        return f"{value:.6g}"
    return str(value)


__all__ = [
    "CONTAINER_FONT_HINT",
    "FONT_GLYPH_MARKERS",
    "GET_SCHEMA",
    "LAYOUT_HINT",
    "LOCAL_FONT_HINT",
    "LOCAL_MODE_HINT",
    "RUN_PYTHON",
    "TIGHT_LAYOUT_MARKER",
    "ToolOutcome",
    "ToolRuntime",
    "build_tool_schemas",
    "failure_fingerprint",
    "font_glyph_hint",
    "layout_hint",
]
