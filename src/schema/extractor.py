"""Schema 提取 —— 让模型看懂数据，但绝不把数据塞进 Prompt。

这个模块是**成本控制的第一道闸门**。它的产出会被整个 Prompt 反复携带，
所以这里的每一处取舍都直接决定后续每一轮对话的 token 成本与出错概率。

四条硬规则（每一条都对应一个真实的翻车点）：

1. **绝不 `read_csv(path)` 再 `.head()`**
   10 万行的表全量读进宿主进程，轻则几秒卡死，重则把宿主 python 吃光内存 ——
   讽刺的是，这恰恰是本项目沙箱要防的那类事故，却在宿主侧自己犯了。
   任何一次读取都必须带 `nrows=` 或 `usecols=` 裁剪。

2. **dtype 不能用 pandas 的粗粒度类型**
   `object`（pandas 3.0 起字符串列是 `str`）太宽泛了：一列地区名和一列长评论
   都是字符串，但模型对它们的处理方式完全不同。必须拆成
   `string` / `category` / `mixed` / `datetime`，并给出 `unique_count`，
   模型才知道能不能 groupby。

3. **缺失率必须显式给出**
   某列 90% 为空，正确做法是**直接丢弃**，而不是花三轮去「处理缺失值」。
   不给这个数字，模型只能盲猜。

4. **给模型的路径必须是容器内的路径**
   宿主是 Windows（`D:\\...`），容器里是 `/data/xxx.csv`。把宿主路径塞给模型，
   它生成的代码在沙箱里 100% 打不开文件。这个映射由本模块负责，
   上层编排只管拿 `DatasetSchema.data_path` 用。

另外一处隐含的取舍：算 `mean` / `std` / 分位数**必须读到整列**，这与规则 1
存在张力。解法是**按列裁剪**而非按行裁剪：
  - 数值列合并读一次（数值列通常很窄，一次 IO 换 n 列统计很划算）；
  - 文本列逐列读（字符串列才是内存杀手，一次读进来可能就是几百 MB）。
这样最坏情况是 n_cols 次解析（慢一点），但内存占用始终只有**一列**。
"""

from __future__ import annotations

import math
import warnings
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Any, Iterable, Sequence

import pandas as pd

from ..sandbox.analysis import safe_target_name

__all__ = [
    "ColumnSchema",
    "DatasetSchema",
    "SchemaError",
    "UnsupportedFileError",
    "extract_schema",
    "extract_schemas",
    "schemas_to_prompt_string",
]

# ------------------------------------------------------------------ 可调常量

SAMPLE_ROWS = 5
"""采样行数。**这是上限，不是建议值** —— 采样只用于让模型看懂形态。"""

CATEGORY_MAX_UNIQUE = 50
"""唯一值不超过这个数，才可能算「分类变量」。"""

CATEGORY_MAX_RATIO = 0.5
"""唯一值 / 行数 不超过这个比例，才可能算「分类变量」。

地区列（1 万行 7 个取值）两条都满足 → category；
订单号列（1 万行 9990 个取值）unique 超阈值 → string。
"""

CATEGORY_MIN_ROWS_FOR_RATIO = 100
"""行数少于这个值时，**不做比例推断**。

5 行的样本里 3 个不同取值，比例是 0.6 —— 远超阈值，会被判成自由文本。
但这明显是样本太小导致的统计失真，不是这列真的像自由文本。
样本不足时只看 unique_count 的绝对值。
"""

HIGH_NULL_RATIO = 0.5
"""缺失率超过这个比例，在 Prompt 里打 ⚠️ 并明确建议丢弃。"""

MAX_PROMPT_COLUMNS = 30
"""Prompt 里最多展示的列数。列特别宽时，宁可截断也不要撑爆上下文。"""

DATETIME_SAMPLE_SIZE = 500
DATETIME_HIT_RATIO = 0.9
"""日期列检测：抽样 N 个非空值，解析成功率 ≥ 90% 才认作 datetime。"""

EXAMPLE_VALUES = 5
EXAMPLE_MAX_CHARS = 20

_CSV_SUFFIXES = {".csv", ".tsv", ".txt"}
_EXCEL_SUFFIXES = {".xlsx", ".xlsm"}
_LEGACY_EXCEL_SUFFIXES = {".xls"}
_ALL_SUFFIXES = _CSV_SUFFIXES | _EXCEL_SUFFIXES | _LEGACY_EXCEL_SUFFIXES

_READ_HINT = {
    ".csv": "pd.read_csv",
    ".tsv": "pd.read_csv(..., sep='\\t')",
    ".txt": "pd.read_csv",
    ".xlsx": "pd.read_excel",
    ".xlsm": "pd.read_excel",
    ".xls": "pd.read_excel",
}


class SchemaError(RuntimeError):
    """Schema 提取失败。区别于代码 bug —— 属于「这份数据读不了」，要告诉用户。"""


class UnsupportedFileError(SchemaError):
    """不支持的文件类型。"""


# ------------------------------------------------------------------ 数据结构


@dataclass
class ColumnSchema:
    """一列的画像。"""

    name: str
    dtype: str
    """语义化类型：`int64` / `float64` / `bool` / `datetime` /
    `category` / `string` / `mixed`。

    注意这里**不是** pandas 原始 dtype：object 会被细分，
    因为模型需要知道「能不能 groupby」而不只是「是不是 object」。
    """

    null_count: int
    null_ratio: float
    unique_count: int
    stats: dict[str, Any] | None = None
    """仅数值列有：`min` / `max` / `mean` / `std` / `p25` / `p50` / `p75`。

    数值一律转成 Python 原生 float，缺失（NaN/inf）转 None —— 保证整个
    DatasetSchema 能直接 JSON 序列化后塞进 API 请求体。
    """

    examples: list[str] = field(default_factory=list)
    """若干取值样例（已转字符串并截断）。

    对分类列尤其有用：看到「华东 | 华南 | 华北」，模型才知道能按地区聚合。
    """

    @property
    def is_numeric(self) -> bool:
        return self.dtype in {"int64", "float64"}

    @property
    def is_high_null(self) -> bool:
        return self.null_ratio >= HIGH_NULL_RATIO


@dataclass
class DatasetSchema:
    """一份数据文件的画像。"""

    file_name: str
    """原始文件名。用于在 Prompt 里称呼它。"""

    data_path: str
    """**容器内**的路径，形如 `/data/sales.csv`。

    这是给模型用的路径，不是宿主的 `D:\\...\\sales.csv`。
    容器内文件名由 `safe_target_name()` 归一化得到，与执行层挂载时用的
    是同一套规则，保证模型拼出的路径一定打得开。
    """

    n_rows: int
    n_cols: int
    columns: list[ColumnSchema]
    sample_rows: list[dict[str, Any]]

    @property
    def numeric_columns(self) -> list[str]:
        return [c.name for c in self.columns if c.is_numeric]

    @property
    def droppable_columns(self) -> list[str]:
        """缺失率过高的列 —— 通常应直接丢弃，而不是花轮次去填补。"""
        return [c.name for c in self.columns if c.is_high_null]

    def to_prompt_string(self) -> str:
        """拼成给模型看的紧凑文本。**绝不塞全量数据**。"""
        return _render_schema(self)


# ------------------------------------------------------------------ 对外入口


def extract_schema(
    path: str | Path,
    *,
    mount_root: str = "/data",
    sample_rows: int = SAMPLE_ROWS,
) -> DatasetSchema:
    """提取单份数据文件的 Schema。

    Args:
        path: 宿主机上的文件路径。
        mount_root: 数据在**容器内**的挂载目录。DockerExecutor 用 `/data`；
            本地调试执行器没有挂载概念，传 `""` 得到相对路径。
        sample_rows: 采样行数，默认 5。
    """
    source = Path(path)
    _require_readable(source)

    # nrows 裁剪 —— 规则 1
    sample = _read(source, nrows=sample_rows)
    if sample.columns.empty:
        raise SchemaError(f"{source.name} 没有可识别的列（文件可能是空的）")

    n_rows = _count_rows(source, sample)
    columns = _profile_columns(source, sample, n_rows)
    sample_rows_data = _sample_records(sample)

    return DatasetSchema(
        file_name=source.name,
        data_path=_container_path(source, mount_root),
        n_rows=n_rows,
        n_cols=len(sample.columns),
        columns=columns,
        sample_rows=sample_rows_data,
    )


def extract_schemas(
    paths: Iterable[str | Path],
    *,
    mount_root: str = "/data",
    sample_rows: int = SAMPLE_ROWS,
) -> list[DatasetSchema]:
    """批量提取，并**提前发现文件名冲突**。

    冲突必须在这一步就报出来：两个文件归一化后同名（例如 `a/销售.csv` 与
    `b/销售.csv`），容器里后一个会覆盖前一个，而模型拿到的路径却指向同一处。
    这类 bug 极难排查（表现为「数据突然少了一半」），宁可早失败。
    """
    schemas: list[DatasetSchema] = []
    taken: dict[str, str] = {}

    for raw in paths:
        source = Path(raw)
        name = safe_target_name(source)
        if name in taken and taken[name] != str(source):
            raise SchemaError(
                f"文件名冲突：\n  {taken[name]}\n  {source}\n"
                f"两者在容器内都会变成 {mount_root.rstrip('/')}/{name}，"
                "后挂载的会覆盖前一个。请改名后再上传。"
            )
        taken[name] = str(source)
        schemas.append(extract_schema(source, mount_root=mount_root, sample_rows=sample_rows))

    return schemas


def schemas_to_prompt_string(schemas: Sequence[DatasetSchema]) -> str:
    """把多份 Schema 拼成一整段 Prompt 文本。"""
    return "\n\n".join(s.to_prompt_string() for s in schemas)


# ------------------------------------------------------------------ 读取封装


def _require_readable(source: Path) -> None:
    if not source.exists():
        raise SchemaError(f"文件不存在：{source}")
    if not source.is_file():
        raise SchemaError(f"不是文件：{source}")
    if source.suffix.lower() not in _ALL_SUFFIXES:
        raise UnsupportedFileError(
            f"不支持的文件类型 {source.suffix or '(无后缀)'}：{source.name}。"
            f"目前支持 {' / '.join(sorted(_ALL_SUFFIXES))}"
        )


def _read(source: Path, **kwargs: Any) -> pd.DataFrame:
    """统一的读取入口。**调用方必须传 nrows 或 usecols**，否则视同违规。"""
    suffix = source.suffix.lower()
    try:
        if suffix in _CSV_SUFFIXES:
            sep = "\t" if suffix == ".tsv" else ","
            return pd.read_csv(source, sep=sep, **kwargs)
        if suffix in _EXCEL_SUFFIXES:
            return pd.read_excel(source, **kwargs)
        # .xls 需要 xlrd
        return pd.read_excel(source, engine="xlrd", **kwargs)
    except UnsupportedFileError:
        raise
    except Exception as exc:  # noqa: BLE001 —— 转成调用方能处理的错误
        raise SchemaError(f"读取 {source.name} 失败：{exc}") from exc


def _count_rows(source: Path, sample: pd.DataFrame) -> int:
    """数总行数，**只读一列**。

    行数必须给准：模型要靠它判断「能不能全量读」以及缺失率的分母。
    但为此读全表就本末倒置了 —— 只读第一列足够。
    """
    suffix = source.suffix.lower()
    fallback = len(sample)
    try:
        if suffix in _EXCEL_SUFFIXES or suffix in _LEGACY_EXCEL_SUFFIXES:
            return _count_excel_rows(source, fallback)
        # usecols=[0] 按位置取第一列，避免列名重复带来的歧义
        column = pd.read_csv(source, usecols=[0])
        return len(column)
    except Exception:  # noqa: BLE001 —— 行数不准不该让整个流程挂掉
        return fallback


def _count_excel_rows(source: Path, fallback: int) -> int:
    """用 openpyxl 的 read_only 模式逐行数第一列，不把整表读进内存。"""
    try:
        import openpyxl
    except ImportError:
        return fallback

    try:
        workbook = openpyxl.load_workbook(source, read_only=True, data_only=True)
    except Exception:  # noqa: BLE001
        return fallback

    try:
        sheet = workbook[workbook.sheetnames[0]]
        filled = sum(
            1 for (value,) in sheet.iter_rows(min_col=1, max_col=1, values_only=True)
            if value is not None
        )
        # 第一行是表头
        return max(filled - 1, 0)
    except Exception:  # noqa: BLE001
        return fallback
    finally:
        workbook.close()


# ------------------------------------------------------------------ 列画像


def _profile_columns(
    source: Path,
    sample: pd.DataFrame,
    n_rows: int,
) -> list[ColumnSchema]:
    """逐列画像，按 sample 中的原始顺序返回。"""
    # 先按样本初判哪些是数值列。注意这**只是读取策略的分组依据**，
    # 真实 dtype 以整列读入后的结果为准（前 5 行全整数、后面有小数的情况很常见）。
    numeric_guess = [c for c in sample.columns if pd.api.types.is_numeric_dtype(sample[c])]
    text_guess = [c for c in sample.columns if c not in numeric_guess]

    profiled: dict[str, ColumnSchema] = {}

    if numeric_guess:
        # 数值列合并读一次：窄、安全、省 IO
        frame = _read(source, usecols=list(numeric_guess))
        for name in numeric_guess:
            series = frame[name]
            if pd.api.types.is_bool_dtype(series):
                profiled[name] = _make_column(name, "bool", series, n_rows)
            elif pd.api.types.is_numeric_dtype(series):
                profiled[name] = _make_numeric_column(name, series, n_rows)
            else:
                # 样本骗了我们：整列读入后是 object（混合了文本）
                profiled[name] = _make_column(name, _refine_text_dtype(series, n_rows), series, n_rows)

    for name in text_guess:
        # 文本列逐列读：字符串列才是内存杀手，一次全读可能几百 MB
        series = _read(source, usecols=[name])[name]
        profiled[name] = _make_column(name, _refine_text_dtype(series, n_rows), series, n_rows)

    return [profiled[name] for name in sample.columns]


def _make_numeric_column(name: str, series: pd.Series, n_rows: int) -> ColumnSchema:
    dtype = "int64" if pd.api.types.is_integer_dtype(series) else "float64"
    stats = {
        "min": _jsonable(series.min()),
        "max": _jsonable(series.max()),
        "mean": _jsonable(series.mean()),
        "std": _jsonable(series.std()),
        "p25": _jsonable(series.quantile(0.25)),
        "p50": _jsonable(series.quantile(0.50)),
        "p75": _jsonable(series.quantile(0.75)),
    }
    return ColumnSchema(
        name=name,
        dtype=dtype,
        null_count=int(series.isna().sum()),
        null_ratio=_ratio(int(series.isna().sum()), n_rows),
        unique_count=int(series.nunique(dropna=True)),
        stats=stats,
    )


def _make_column(
    name: str,
    dtype: str,
    series: pd.Series,
    n_rows: int,
) -> ColumnSchema:
    null_count = int(series.isna().sum())
    unique_count = int(series.nunique(dropna=True))
    return ColumnSchema(
        name=name,
        dtype=dtype,
        null_count=null_count,
        null_ratio=_ratio(null_count, n_rows),
        unique_count=unique_count,
        stats=None,
        examples=_examples(series),
    )


def _refine_text_dtype(series: pd.Series, n_rows: int) -> str:
    """把非数值列细分成本模块承诺的语义类型。

    pandas 只告诉我们「这不是数字」，而模型需要知道的是
    「能不能 groupby」—— 这两者之间隔着 category / string / mixed 的差别。
    """
    if pd.api.types.is_datetime64_any_dtype(series):
        return "datetime"

    values = series.dropna()
    if values.empty:
        return "string"

    head = values.head(2000)
    has_text = any(isinstance(v, str) for v in head)
    has_number = any(
        isinstance(v, (int, float)) and not isinstance(v, bool) for v in head
    )

    # Excel 里真会有「一个单元格是数字、另一个是文本」的列，
    # 此时 pandas 保留原始 Python 类型，isinstance 就能看出来。
    if has_text and has_number:
        return "mixed"

    # CSV 里则不然：pandas 读 CSV 时**整列统一成一个 dtype**，
    # 混合列会被整体读成字符串，原始的数字信息已经丢失，
    # isinstance 一个数字都认不出来。只能反过来验：
    # 部分值能解析成数字、部分不能 → 说明这列确实是混着的。
    if has_text and 0.0 < _numeric_parse_ratio(values) < 1.0:
        return "mixed"

    if has_text and _looks_like_datetime(values):
        return "datetime"

    unique_count = int(values.nunique())
    if _is_category(unique_count, n_rows):
        return "category"

    return "string"


def _is_category(unique_count: int, n_rows: int) -> bool:
    """判断一列是否更像「分类变量」而不是「自由文本」。"""
    if unique_count > CATEGORY_MAX_UNIQUE:
        return False
    # 样本太少时比例没有统计意义，只看绝对取值个数
    if n_rows < CATEGORY_MIN_ROWS_FOR_RATIO:
        return True
    return _ratio(unique_count, n_rows) <= CATEGORY_MAX_RATIO


def _numeric_parse_ratio(values: pd.Series) -> float:
    """抽样看有多少比例的值能解析成数字。

    这是识别 mixed 列的唯一可靠手段（见 `_refine_text_dtype` 里的说明）：
    pandas 读 CSV 时已经把类型抹平了，只能靠「能不能转回数字」反推。
    """
    head = values.head(2000)
    if head.empty:
        return 0.0
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            parsed = pd.to_numeric(head, errors="coerce")
    except Exception:  # noqa: BLE001
        return 0.0
    return float(parsed.notna().mean())


def _looks_like_datetime(values: pd.Series) -> bool:
    """抽样判断一列文本是否其实是日期。

    只在**非数值列**上调用，所以不用担心纯数字（如 20240101）被误判成日期。
    """
    head = values.head(DATETIME_SAMPLE_SIZE)
    if head.empty:
        return False
    try:
        with warnings.catch_warnings():
            # 推断不出统一格式是常态（用户日期写法五花八门），
            # 逐元素 fallback 到 dateutil 本来就是可以接受的，不必刷警告。
            warnings.simplefilter("ignore")
            parsed = pd.to_datetime(head, errors="coerce", format="mixed")
    except Exception:  # noqa: BLE001 —— 解析不了就不是日期，不是错误
        return False
    return float(parsed.notna().mean()) >= DATETIME_HIT_RATIO


def _examples(series: pd.Series) -> list[str]:
    """取若干个不重复的取值样例，转成短字符串。"""
    values = series.dropna()
    if values.empty:
        return []
    out: list[str] = []
    for value in values.unique():
        text = str(value)
        if len(text) > EXAMPLE_MAX_CHARS:
            text = text[: EXAMPLE_MAX_CHARS - 1] + "…"
        out.append(text)
        if len(out) >= EXAMPLE_VALUES:
            break
    return out


# ------------------------------------------------------------------ 渲染


def _render_schema(schema: DatasetSchema) -> str:
    lines: list[str] = []
    suffix = Path(schema.file_name).suffix.lower()
    reader = _READ_HINT.get(suffix, "pd.read_csv")

    lines.append(f"### {schema.file_name}")
    lines.append(f"- 容器内路径：`{schema.data_path}`（**代码里必须用这个路径**）")
    lines.append(f"- 规模：{schema.n_rows} 行 × {schema.n_cols} 列")
    lines.append(f"- 读取方式：`{reader}('{schema.data_path}')`")

    shown = schema.columns[:MAX_PROMPT_COLUMNS]
    lines.append("")
    lines.append("列信息：")
    for column in shown:
        lines.append(f"- `{column.name}` {column.dtype}"
                     f"  缺失 {column.null_count} ({column.null_ratio:.1%})"
                     f"  唯一 {column.unique_count}"
                     + _render_stats(column)
                     + _render_examples(column)
                     + ("  ⚠️ 缺失过半，建议直接丢弃" if column.is_high_null else ""))

    if len(schema.columns) > len(shown):
        lines.append(f"- …… 另有 {len(schema.columns) - len(shown)} 列未展示")

    droppable = schema.droppable_columns
    if droppable:
        lines.append("")
        lines.append(f"高缺失列（≥{HIGH_NULL_RATIO:.0%}）：{', '.join(droppable)} —— 通常应直接丢弃")

    lines.append("")
    lines.append(f"前 {len(schema.sample_rows)} 行样例：")
    lines.extend(_render_table(schema.sample_rows, [c.name for c in shown]))

    return "\n".join(lines)


def _render_stats(column: ColumnSchema) -> str:
    if not column.stats:
        return ""
    parts = []
    for key in ("min", "max", "mean", "std", "p25", "p50", "p75"):
        if key in column.stats:
            parts.append(f"{key}={_fmt_num(column.stats[key])}")
    return "  " + " ".join(parts) if parts else ""


def _render_examples(column: ColumnSchema) -> str:
    if not column.examples:
        return ""
    return "  例：" + " | ".join(column.examples)


def _render_table(rows: list[dict[str, Any]], columns: list[str]) -> list[str]:
    if not rows:
        return ["（无数据）"]

    header = "| " + " | ".join(str(c) for c in columns) + " |"
    divider = "|" + "|".join("---" for _ in columns) + "|"
    body = []
    for row in rows:
        cells = []
        for column in columns:
            value = row.get(column)
            cells.append("—" if value is None else str(value))
        body.append("| " + " | ".join(cells) + " |")
    return [header, divider, *body]


# ------------------------------------------------------------------ 小工具


def _container_path(source: Path, mount_root: str) -> str:
    """宿主路径 → 容器内路径。

    文件名归一化必须与执行层挂载时用同一套规则（`safe_target_name`），
    否则模型拼出的路径在容器里打不开。
    """
    name = safe_target_name(source)
    root = (mount_root or "").rstrip("/")
    return f"{root}/{name}" if root else name


def _ratio(count: int, total: int) -> float:
    return 0.0 if total <= 0 else count / total


def _jsonable(value: Any) -> Any:
    """把 numpy / pandas 的标量转成能进 JSON 的原生类型。

    这里必须显式处理 `pd.NA` / `pd.NaT`：它们既不是 float 也不是 None，
    漏掉就会让整个 Schema 无法 JSON 序列化 —— 而这个对象是要直接进
    API 请求体的，序列化失败意味着整轮对话崩掉。
    """
    if value is None:
        return None
    try:
        if pd.isna(value):  # NaN / NaT / pd.NA 一网打尽
            return None
    except (TypeError, ValueError):
        pass
    if isinstance(value, (datetime, date)):
        return str(value)
    if hasattr(value, "item"):
        try:
            value = value.item()
        except Exception:  # noqa: BLE001
            return None
    if isinstance(value, float) and (math.isnan(value) or math.isinf(value)):
        return None
    return value


def _sample_records(sample: pd.DataFrame) -> list[dict[str, Any]]:
    return [
        {key: _jsonable(value) for key, value in record.items()}
        for record in sample.to_dict(orient="records")
    ]


def _fmt_num(value: Any) -> str:
    if value is None:
        return "—"
    if isinstance(value, bool):
        return str(value)
    if isinstance(value, float):
        if math.isnan(value) or math.isinf(value):
            return "—"
        if value != 0 and (abs(value) >= 1e6 or abs(value) < 1e-3):
            return f"{value:.3e}"
        return f"{value:.6g}"
    return str(value)
