"""Schema 提取的测试。

这个文件里最有价值的是 `TestNeverReadsFullTable` —— 它不是在测功能，
而是在**守住一条纪律**：任何一次文件读取都必须带 `nrows` 或 `usecols`。
功能测试能发现「算错了」，但发现不了「这次改动让内存占用翻了 100 倍」，
而后者在真实数据集上就是一次宿主进程 OOM —— 恰恰是本项目沙箱要防的事故。
"""

from __future__ import annotations

import json
from dataclasses import asdict
from pathlib import Path

import pandas as pd
import pytest

from src.schema.extractor import (
    SAMPLE_ROWS,
    ColumnSchema,
    DatasetSchema,
    SchemaError,
    UnsupportedFileError,
    extract_schema,
    extract_schemas,
    schemas_to_prompt_string,
)


# ------------------------------------------------------------------ fixtures


@pytest.fixture
def sales_csv(tmp_path: Path) -> Path:
    """一份典型的小样本销售数据。"""
    target = tmp_path / "销售.csv"
    target.write_text(
        "地区,销售额,数量,备注,日期,是否促销\n"
        "华东,1200.5,3,,2024-01-01,True\n"
        "华南,800.0,2,缺货,2024-01-02,False\n"
        "华北,1500.75,5,,2024-01-03,True\n"
        "华东,300.0,1,ok,2024-01-04,False\n"
        "华南,2200.0,8,,2024-01-05,True\n"
        "华东,110.0,2,test,2024-01-06,False\n",
        encoding="utf-8",
    )
    return target


@pytest.fixture
def numbers_csv(tmp_path: Path) -> Path:
    """1..5 的纯数值列 —— 统计量可以手算校验。"""
    target = tmp_path / "numbers.csv"
    target.write_text("值\n1\n2\n3\n4\n5\n", encoding="utf-8")
    return target


@pytest.fixture
def wide_large_csv(tmp_path: Path) -> Path:
    """2000 行，用于验证行数统计准确、且不被全量读取。"""
    target = tmp_path / "large.csv"
    frame = pd.DataFrame(
        {
            "id": range(2000),
            "金额": [i * 1.5 for i in range(2000)],
            "地区": (["华东", "华南", "华北"] * 700)[:2000],
        }
    )
    frame.to_csv(target, index=False)
    return target


# ------------------------------------------------------- 纪律：绝不全量读取


class TestNeverReadsFullTable:
    """守住成本控制的第一道闸门。"""

    def test_every_read_is_row_or_column_limited(self, monkeypatch, wide_large_csv):
        """任何一次 read_csv 都必须带 nrows 或 usecols。

        这条断言的价值：将来有人为图省事改成 `read_csv(path)`，
        单测全绿、功能也正常，但真实数据集上会直接把宿主进程吃掉 ——
        功能测试永远抓不到这种回归，只有这种「检查调用参数」的测试能。
        """
        calls: list[dict] = []
        real_read_csv = pd.read_csv

        def spy(*args, **kwargs):
            calls.append(dict(kwargs))
            return real_read_csv(*args, **kwargs)

        monkeypatch.setattr(pd, "read_csv", spy)
        extract_schema(wide_large_csv)

        assert calls, "应当至少读取一次"
        for kwargs in calls:
            assert "nrows" in kwargs or "usecols" in kwargs, (
                f"发现全量读取（既无 nrows 也无 usecols）：{kwargs}。"
                "大文件会因此把宿主进程内存吃光。"
            )

    def test_excel_reads_are_also_limited(self, monkeypatch, tmp_path):
        """Excel 同样受限 —— 它比 CSV 更容易一次读爆（整表进内存）。"""
        pytest.importorskip("openpyxl")
        target = tmp_path / "库存.xlsx"
        pd.DataFrame(
            {"仓库": ["A", "B", "C"] * 100, "数量": list(range(300))}
        ).to_excel(target, index=False)

        calls: list[dict] = []
        real_read_excel = pd.read_excel

        def spy(*args, **kwargs):
            calls.append(dict(kwargs))
            return real_read_excel(*args, **kwargs)

        monkeypatch.setattr(pd, "read_excel", spy)
        extract_schema(target)

        assert calls
        for kwargs in calls:
            assert "nrows" in kwargs or "usecols" in kwargs, f"Excel 全量读取：{kwargs}"

    def test_sample_rows_never_exceeds_limit(self, sales_csv):
        assert len(extract_schema(sales_csv).sample_rows) <= SAMPLE_ROWS

    def test_sample_rows_stay_small_on_large_file(self, wide_large_csv):
        """2000 行的表，采样仍然只有 5 行 —— 这是 token 成本的底线。"""
        schema = extract_schema(wide_large_csv)
        assert len(schema.sample_rows) == SAMPLE_ROWS
        assert schema.n_rows == 2000


# ------------------------------------------------------------ dtype 细分


class TestDtypeRefinement:
    def test_category_for_low_cardinality(self, sales_csv):
        columns = {c.name: c for c in extract_schema(sales_csv).columns}
        assert columns["地区"].dtype == "category"
        # 分类列要给得出取值样例，模型才知道能按什么聚合
        assert columns["地区"].examples

    def test_string_for_free_text(self, tmp_path):
        """取值足够多 → 自由文本，不能让模型误以为可以 groupby。"""
        target = tmp_path / "notes.csv"
        rows = ["备注"] + [f"这是第 {i} 条各不相同的备注" for i in range(60)]
        target.write_text("\n".join(rows) + "\n", encoding="utf-8")
        column = extract_schema(target).columns[0]
        assert column.dtype == "string"
        assert column.unique_count == 60

    def test_mixed_when_numbers_and_text_share_column(self, tmp_path):
        """前几行是数字、后面是文本 —— CSV 里最常见的数据脏点。"""
        target = tmp_path / "mixed.csv"
        target.write_text("值\n1\n2\n3\n4\n5\nabc\ndef\nxyz\n", encoding="utf-8")
        assert extract_schema(target).columns[0].dtype == "mixed"

    def test_datetime_is_detected(self, sales_csv):
        """日期列要认出来，否则模型会拿字符串比较去排序时间。"""
        columns = {c.name: c for c in extract_schema(sales_csv).columns}
        assert columns["日期"].dtype == "datetime"

    def test_bool_is_not_treated_as_number(self, sales_csv):
        """bool 若被当数值列，mean/std 毫无意义且会误导模型。"""
        columns = {c.name: c for c in extract_schema(sales_csv).columns}
        assert columns["是否促销"].dtype == "bool"
        assert columns["是否促销"].stats is None

    def test_integer_and_float_are_distinguished(self, tmp_path):
        target = tmp_path / "t.csv"
        target.write_text("整数,小数\n1,1.5\n2,2.5\n3,3.5\n", encoding="utf-8")
        columns = {c.name: c for c in extract_schema(target).columns}
        assert columns["整数"].dtype == "int64"
        assert columns["小数"].dtype == "float64"

    def test_small_sample_does_not_distort_category_detection(self, tmp_path):
        """5 行 3 个取值，比例 0.6 远超阈值 —— 但样本太小，不该按比例判。

        曾经这里会判成 string，导致小样本文件的所有文本列都失去「可聚合」信息。
        """
        target = tmp_path / "small.csv"
        target.write_text("仓库\nA\nB\nC\nA\nB\n", encoding="utf-8")
        assert extract_schema(target).columns[0].dtype == "category"


# ------------------------------------------------------------ 缺失值统计


class TestMissingStats:
    def test_null_count_and_ratio(self, sales_csv):
        columns = {c.name: c for c in extract_schema(sales_csv).columns}
        # 6 行里 3 行为空
        assert columns["备注"].null_count == 3
        assert columns["备注"].null_ratio == pytest.approx(0.5)
        assert columns["地区"].null_count == 0

    def test_high_null_columns_are_flagged_as_droppable(self, tmp_path):
        """80% 缺失的列要被点名 —— 正确做法是丢弃，不是填补。"""
        target = tmp_path / "sparse.csv"
        pd.DataFrame(
            {"a": [1, 2] + [None] * 8}
        ).to_csv(target, index=False)
        schema = extract_schema(target)
        assert schema.columns[0].is_high_null
        assert schema.droppable_columns == ["a"]

    def test_high_null_warning_reaches_prompt(self, tmp_path):
        """标记只存在于结构体里是不够的 —— 模型看不到结构体，只看得到文本。"""
        target = tmp_path / "sparse.csv"
        pd.DataFrame({"a": [1, 2] + [None] * 8}).to_csv(target, index=False)
        prompt = extract_schema(target).to_prompt_string()
        assert "建议直接丢弃" in prompt


# ------------------------------------------------------------ 数值统计


class TestNumericStats:
    def test_stats_are_correct(self, numbers_csv):
        """1..5：min=1 max=5 mean=3 p25=2 p50=3 p75=4，std 为样本标准差。"""
        stats = extract_schema(numbers_csv).columns[0].stats
        assert stats is not None
        assert stats["min"] == pytest.approx(1)
        assert stats["max"] == pytest.approx(5)
        assert stats["mean"] == pytest.approx(3)
        assert stats["p25"] == pytest.approx(2)
        assert stats["p50"] == pytest.approx(3)
        assert stats["p75"] == pytest.approx(4)
        assert stats["std"] == pytest.approx(1.5811, abs=1e-3)

    def test_text_columns_have_no_stats(self, sales_csv):
        columns = {c.name: c for c in extract_schema(sales_csv).columns}
        assert columns["地区"].stats is None

    def test_stats_survive_json_serialization(self, numbers_csv):
        """stats 会直接进 API 请求体，numpy 标量必须已经转成原生类型。"""
        schema = extract_schema(numbers_csv)
        payload = json.dumps(asdict(schema), ensure_ascii=False)
        assert '"min": 1' in payload or '"min": 1.0' in payload

    def test_nan_std_becomes_none(self, tmp_path):
        """只有一行的数值列，std 是 NaN —— 不能让 NaN 流进 JSON。"""
        target = tmp_path / "one.csv"
        target.write_text("值\n7\n", encoding="utf-8")
        assert extract_schema(target).columns[0].stats["std"] is None


# ------------------------------------------------------------ 路径映射


class TestContainerPathMapping:
    """模型必须在沙箱里打得开这个文件，所以路径必须是容器视角。"""

    def test_docker_mode_uses_data_mount(self, sales_csv):
        assert extract_schema(sales_csv).data_path == "/data/销售.csv"

    def test_local_mode_uses_relative_path(self, sales_csv):
        """本地调试执行器没有 /data 挂载，退回相对路径。"""
        assert extract_schema(sales_csv, mount_root="").data_path == "销售.csv"

    def test_chinese_filename_is_preserved(self, sales_csv):
        """中文文件名不该被改写 —— 否则模型拼出的路径对不上。"""
        assert "销售.csv" in extract_schema(sales_csv).data_path

    def test_uses_same_normalization_as_executor(self, tmp_path):
        """必须与执行层挂载时的文件名规则一致，否则容器内路径对不上。"""
        from src.sandbox.analysis import safe_target_name

        source = tmp_path / "weird name.csv"
        source.write_text("a\n1\n", encoding="utf-8")
        schema = extract_schema(source)
        assert schema.data_path.endswith(safe_target_name(source))


class TestMultipleFiles:
    def test_extract_schemas_returns_all(self, tmp_path):
        first = tmp_path / "a.csv"
        second = tmp_path / "b.csv"
        first.write_text("x\n1\n", encoding="utf-8")
        second.write_text("y\n2\n", encoding="utf-8")
        schemas = extract_schemas([first, second])
        assert [s.file_name for s in schemas] == ["a.csv", "b.csv"]
        assert [s.data_path for s in schemas] == ["/data/a.csv", "/data/b.csv"]

    def test_name_collision_is_rejected_early(self, tmp_path):
        """同名文件在容器里会互相覆盖，而模型拿到的却是同一个路径。

        这种 bug 表现为「数据突然少了一半」，极难排查，必须在入口就拦下。
        """
        (tmp_path / "a").mkdir()
        (tmp_path / "b").mkdir()
        first = tmp_path / "a" / "销售.csv"
        second = tmp_path / "b" / "销售.csv"
        first.write_text("x\n1\n", encoding="utf-8")
        second.write_text("y\n2\n", encoding="utf-8")

        with pytest.raises(SchemaError, match="文件名冲突"):
            extract_schemas([first, second])

    def test_same_file_twice_is_not_a_collision(self, tmp_path):
        first = tmp_path / "a.csv"
        first.write_text("x\n1\n", encoding="utf-8")
        schemas = extract_schemas([first, first])
        assert len(schemas) == 2

    def test_schemas_to_prompt_string(self, tmp_path):
        first = tmp_path / "a.csv"
        second = tmp_path / "b.csv"
        first.write_text("x\n1\n", encoding="utf-8")
        second.write_text("y\n2\n", encoding="utf-8")
        prompt = schemas_to_prompt_string(extract_schemas([first, second]))
        assert "### a.csv" in prompt and "### b.csv" in prompt


# ------------------------------------------------------------ Prompt 文本


class TestPromptString:
    def test_contains_container_path_not_host_path(self, sales_csv):
        prompt = extract_schema(sales_csv).to_prompt_string()
        assert "/data/销售.csv" in prompt
        # 宿主绝对路径绝不能出现（Windows 盘符）
        assert ":\\" not in prompt
        assert str(sales_csv.parent).replace("\\", "/") not in prompt

    def test_does_not_contain_full_data(self, wide_large_csv):
        """2000 行数据不能出现在 Prompt 里 —— 这是这个模块存在的全部意义。

        注意不要拿「某个数字没出现」当判据：max / p75 这类统计值本来就会
        带上数据里的极端值，误伤面很大。真正该数的是**表格有几行**。
        """
        prompt = extract_schema(wide_large_csv).to_prompt_string()
        assert len(prompt) < 3000, f"Prompt 过长（{len(prompt)} 字符），疑似塞了数据"

        table_lines = [
            line for line in prompt.splitlines()
            if line.startswith("| ") and "---" not in line
        ]
        # 表头 1 行 + 数据 SAMPLE_ROWS 行
        assert len(table_lines) == 1 + SAMPLE_ROWS

    def test_shows_shape_and_columns(self, sales_csv):
        prompt = extract_schema(sales_csv).to_prompt_string()
        assert "6 行" in prompt and "6 列" in prompt
        assert "销售额" in prompt

    def test_shows_read_hint(self, sales_csv):
        """直接告诉模型怎么读 —— 省一轮试错。"""
        prompt = extract_schema(sales_csv).to_prompt_string()
        assert "pd.read_csv('/data/销售.csv')" in prompt

    def test_wide_table_is_truncated(self, tmp_path):
        """列特别多时宁可截断，也不能撑爆上下文。"""
        target = tmp_path / "wide.csv"
        columns = {f"c{i}": [1, 2, 3] for i in range(60)}
        pd.DataFrame(columns).to_csv(target, index=False)
        prompt = extract_schema(target).to_prompt_string()
        assert "另有" in prompt and "列未展示" in prompt

    def test_empty_table_renders_without_crash(self, tmp_path):
        target = tmp_path / "empty.csv"
        target.write_text("a,b\n", encoding="utf-8")
        prompt = extract_schema(target).to_prompt_string()
        assert "0 行" in prompt


# ------------------------------------------------------------ 错误处理


class TestErrors:
    def test_missing_file(self, tmp_path):
        with pytest.raises(SchemaError, match="文件不存在"):
            extract_schema(tmp_path / "nope.csv")

    def test_unsupported_suffix(self, tmp_path):
        target = tmp_path / "x.pdf"
        target.write_bytes(b"%PDF-1.4")
        with pytest.raises(UnsupportedFileError, match="不支持的文件类型"):
            extract_schema(target)

    def test_directory_is_rejected(self, tmp_path):
        with pytest.raises(SchemaError, match="不是文件"):
            extract_schema(tmp_path)

    def test_corrupt_csv_raises_schema_error_not_traceback(self, tmp_path):
        """读不了要转成 SchemaError —— 编排层要能区分「数据问题」和「代码 bug」。"""
        target = tmp_path / "broken.xlsx"
        target.write_bytes(b"not an excel file at all")
        with pytest.raises(SchemaError):
            extract_schema(target)


# ------------------------------------------------------------ Excel


class TestExcel:
    @pytest.fixture
    def xlsx(self, tmp_path: Path) -> Path:
        pytest.importorskip("openpyxl")
        target = tmp_path / "库存.xlsx"
        pd.DataFrame(
            {
                "仓库": ["A", "B", "C"] * 20,
                "库存量": list(range(60)),
                "单价": [i * 1.5 for i in range(60)],
            }
        ).to_excel(target, index=False)
        return target

    def test_row_count_is_accurate(self, xlsx):
        """行数要给准 —— 它是缺失率的分母，也是模型判断能否全量读的依据。"""
        assert extract_schema(xlsx).n_rows == 60

    def test_columns_are_profiled(self, xlsx):
        columns = {c.name: c for c in extract_schema(xlsx).columns}
        assert columns["仓库"].dtype == "category"
        assert columns["库存量"].dtype == "int64"
        assert columns["单价"].stats is not None

    def test_read_hint_uses_read_excel(self, xlsx):
        assert "pd.read_excel('/data/库存.xlsx')" in extract_schema(xlsx).to_prompt_string()


# ------------------------------------------------------------ 数据结构


class TestDataStructures:
    def test_numeric_columns_property(self, sales_csv):
        schema = extract_schema(sales_csv)
        assert set(schema.numeric_columns) == {"销售额", "数量"}

    def test_column_schema_defaults(self):
        column = ColumnSchema(name="x", dtype="string", null_count=0, null_ratio=0.0, unique_count=1)
        assert column.stats is None
        assert column.examples == []
        assert column.is_numeric is False
        assert column.is_high_null is False

    def test_dataset_schema_is_json_serializable(self, sales_csv):
        """整个对象要能直接进 API 请求体。"""
        payload = json.dumps(asdict(extract_schema(sales_csv)), ensure_ascii=False)
        assert "销售额" in payload

    def test_returned_type(self, sales_csv):
        assert isinstance(extract_schema(sales_csv), DatasetSchema)
