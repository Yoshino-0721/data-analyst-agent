"""契约层的桩对象测试。

这里测的全部是**纯逻辑**：给一组输入信号，断言归类正确、路径被剥干净、
输出被截断、to_tool_payload 没漏出宿主信息。

刻意不引入任何 Docker / 子进程：这些规则是整套错误反馈机制的源头，
必须能在没有 Docker 的机器上（CI、别人的电脑）跑起来验证。
"""

from __future__ import annotations

import pytest

from src.sandbox.analysis import (
    clean_traceback,
    classify_execution,
    default_hint,
    precheck_code,
    safe_target_name,
    truncate_output,
    unique_path,
)
from src.sandbox.executor import (
    ExecStatus,
    ExecutionResult,
    rejection,
    sandbox_error,
)


# =============================================================== 归类规则


class TestClassifyExecution:
    """六分类判定 —— 这是整套设计里最核心的一组规则。"""

    def test_exit_zero_is_ok(self):
        assert (
            classify_execution(exit_code=0, stderr="anything", timed_out=False)
            is ExecStatus.OK
        )

    def test_timeout_wins_over_exit_code(self):
        """超时由宿主的墙钟计时器判定，属于「我们自己掌握的真相」。

        强杀之后进程/容器返回什么退出码并不稳定，所以不能用 exit_code 去
        反推是否超时 —— 计时器说了算。
        """
        assert (
            classify_execution(exit_code=0, stderr="", timed_out=True)
            is ExecStatus.TIMEOUT
        )
        assert (
            classify_execution(exit_code=137, stderr="Killed", timed_out=True)
            is ExecStatus.TIMEOUT
        )

    @pytest.mark.parametrize("stderr", ["", "Killed", "MemoryError", "out of memory"])
    def test_exit_137_is_oom_even_with_empty_stderr(self, stderr: str):
        """非超时的 137 一律判 OOM，哪怕 stderr 是空的。

        这是被真机推翻过一次的设计：容器里 `docker run` 直接起 python、
        中间没有 shell，cgroup OOM killer 杀进程时没人往 stderr 写 "Killed"
        （那个词是 shell 打印的）。实测就是 exit_code=137 + stderr=""。
        当初要求「双条件」会让 OOM 被误判成 RUNTIME_ERROR，模型于是去改
        本来没写错的代码，修复方向彻底跑偏。
        """
        assert (
            classify_execution(exit_code=137, stderr=stderr, timed_out=False)
            is ExecStatus.OOM
        )

    @pytest.mark.parametrize("stderr", ["MemoryError", "out of memory", "Cannot allocate memory"])
    def test_memory_error_in_stderr_is_oom(self, stderr: str):
        """Python 自己接住 MemoryError 时走正常退出（exit 1），靠关键词兜住。"""
        assert (
            classify_execution(exit_code=1, stderr=stderr, timed_out=False)
            is ExecStatus.OOM
        )

    @pytest.mark.parametrize("exit_code", [1, 2, 125, 126, 134, 139])
    def test_nonzero_exit_is_runtime_error(self, exit_code: int):
        assert (
            classify_execution(exit_code=exit_code, stderr="boom", timed_out=False)
            is ExecStatus.RUNTIME_ERROR
        )

    @pytest.mark.parametrize("stderr", ["boom", "ZoomError", "no room left", "bloom filter failed"])
    def test_words_containing_oom_are_not_oom(self, stderr: str):
        """子串匹配的经典误伤：boom / room / zoom 里都含 "oom"。

        真出过一次 —— 判定表里放了裸 "oom"，结果 stderr 只要出现这类词，
        普通运行错误就被报成 OOM，模型跑去优化内存而不是修真正的 bug。
        """
        assert (
            classify_execution(exit_code=1, stderr=stderr, timed_out=False)
            is ExecStatus.RUNTIME_ERROR
        )

    @pytest.mark.parametrize(
        "stderr",
        [
            "FileNotFoundError: [Errno 2] No such file or directory: '/data/killed.csv'",
            "KeyError: 'killed'",
            "the worker was killed by the operator",
        ],
    )
    def test_words_containing_killed_are_not_oom(self, stderr: str):
        """含 "killed" 的普通报错不能被判成 OOM。

        判定表里曾经留着**裸词 "killed"** —— 与当初被真机打掉的裸 "oom" 是同一类错误：
        子串匹配会把 `/data/killed.csv` 这种文件名一起吃进来，普通报错于是被报成 OOM，
        模型跑去优化内存而不是修真正的 bug。AGENTS.md 早把"不放裸词"写成规则，
        但那次只修了 `oom`，`killed` 一直留着，而且**没有任何测试覆盖它**。
        """
        assert (
            classify_execution(exit_code=1, stderr=stderr, timed_out=False)
            is ExecStatus.RUNTIME_ERROR
        )

    def test_bare_killed_word_is_no_longer_a_marker(self):
        """"Killed" 这个词本身不再是判据。

        它只在 shell 打印子进程被 SIGKILL 时出现，而那种情况下退出码是 137，
        由 `exit_code == 137` 那条路径兜住（那条路径有独立用例覆盖）。
        保留裸词只会多一类误伤，不会多认出一次真正的 OOM。
        """
        assert (
            classify_execution(exit_code=1, stderr="Killed", timed_out=False)
            is ExecStatus.RUNTIME_ERROR
        )

    def test_exit_code_none_without_timeout_is_runtime_error(self):
        """进程没起来也算运行错误 —— 至少让模型看到 stderr。"""
        assert (
            classify_execution(exit_code=None, stderr="no such file", timed_out=False)
            is ExecStatus.RUNTIME_ERROR
        )


class TestDefaultHint:
    def testOk_has_no_hint(self):
        assert default_hint(ExecStatus.OK) == ""

    def test_sandbox_error_has_no_hint(self):
        """环境坏了不是模型能修的，给建议只会误导它去改本来没错的代码。"""
        assert default_hint(ExecStatus.SANDBOX_ERROR) == ""

    @pytest.mark.parametrize(
        "status,keyword",
        [
            (ExecStatus.TIMEOUT, "超时"),
            (ExecStatus.OOM, "内存"),
            (ExecStatus.RUNTIME_ERROR, "列名"),
            (ExecStatus.REJECTED, "拒绝"),
        ],
    )
    def test_hints_point_at_the_real_cause(self, status: ExecStatus, keyword: str):
        """每条 hint 都要给出具体方向，而不是「再试一次」这种废话。"""
        assert keyword in default_hint(status)


# =============================================================== traceback 清洗


class TestCleanTraceback:
    def test_strips_host_absolute_path(self):
        raw = (
            'Traceback (most recent call last):\n'
            '  File "/tmp/run_9f3a1c2b/script.py", line 7, in <module>\n'
            '    df = pd.read_csv("/tmp/run_9f3a1c2b/data/销售.csv")\n'
            "FileNotFoundError: No such file\n"
        )
        out = clean_traceback(raw)
        assert "/tmp/run_" not in out
        assert "script.py" in out

    def test_preserves_line_numbers(self):
        """行号必须留下 —— 模型要靠它对着自己写的代码定位。"""
        raw = '  File "/tmp/run_abc12345/script.py", line 42, in <module>\nValueError'
        out = clean_traceback(raw)
        assert "line 42" in out

    def test_handles_windows_paths(self):
        raw = '  File "C:\\Users\\alice\\AppData\\Local\\Temp\\run_x\\script.py", line 3\nKeyError'
        out = clean_traceback(raw)
        assert "alice" not in out
        assert "script.py" in out

    def test_keeps_tail_when_too_long(self):
        raw = "\n".join(f"line {i}" for i in range(100))
        out = clean_traceback(raw, max_lines=10)
        assert "已省略" in out
        assert "line 99" in out  # 末行（最关键的信息）必须保留

    def test_empty_input_returns_empty(self):
        assert clean_traceback("") == ""
        assert clean_traceback("   \n  \n") == ""


# =============================================================== 输出截断


class TestTruncateOutput:
    def test_short_text_untouched(self):
        text, truncated = truncate_output("hello", 100)
        assert text == "hello" and truncated is False

    def test_ascii_truncation(self):
        text, truncated = truncate_output("a" * 100, 10)
        assert len(text) == 10 and truncated is True

    def test_counts_bytes_not_characters(self):
        """一个中文字符 3 字节 —— 按字符计数会低估体积，日志照样被撑爆。"""
        text, truncated = truncate_output("一" * 100, 30)  # 300 字节
        assert truncated is True
        assert text.encode("utf-8") <= b"\xe4\xb8\x80" * 30

    def test_never_breaks_a_multibyte_character(self):
        text, truncated = truncate_output("一二三四五", 5)  # 5 字节 = 1.67 个中文
        assert truncated is True
        # 不能切出半个字：解码必须无损
        assert text.encode("utf-8").decode("utf-8") == text

    def test_zero_limit_empties_output(self):
        assert truncate_output("anything", 0) == ("", True)


# =============================================================== to_tool_payload 净化


class TestToToolPayload:
    def test_contains_only_allowlisted_fields(self):
        result = ExecutionResult(status=ExecStatus.RUNTIME_ERROR, stdout="x")
        payload = result.to_tool_payload()
        assert set(payload) == {"status", "stdout", "stderr", "exit_code", "duration_ms"}
        assert "raw_command" not in payload  # 含宿主信息的字段永不外泄

    def test_artifacts_are_basenames_only(self):
        """给模型文件名，给前端宿主路径 —— 两者不能混。"""
        result = ExecutionResult(
            status=ExecStatus.OK,
            artifacts=(
                __import__("pathlib").Path("/secret/host/dir/chart.png"),
                __import__("pathlib").Path("/secret/host/dir/result.csv"),
            ),
        )
        assert result.to_tool_payload()["artifacts"] == ["chart.png", "result.csv"]
        assert "secret" not in str(result.to_tool_payload())

    def test_truncation_is_announced(self):
        result = ExecutionResult(status=ExecStatus.OK, stdout_truncated=True)
        payload = result.to_tool_payload()
        assert payload["truncated"]["stdout"] is True
        # 必须提醒模型别基于残缺输出做统计
        assert "不完整" in payload["truncated"]["note"]

    def test_no_truncation_key_when_clean(self):
        result = ExecutionResult(status=ExecStatus.OK)
        assert "truncated" not in result.to_tool_payload()

    def test_sandbox_error_hides_summary_from_model(self):
        """环境性问题甩给模型只会让它瞎改本来没错的代码。"""
        result = ExecutionResult(
            status=ExecStatus.SANDBOX_ERROR,
            error_summary="docker daemon 不可达",
        )
        assert "error_summary" not in result.to_tool_payload()

    def test_runtime_error_exposes_summary(self):
        result = ExecutionResult(
            status=ExecStatus.RUNTIME_ERROR, error_summary="KeyError: 地区"
        )
        assert result.to_tool_payload()["error_summary"] == "KeyError: 地区"

    def test_hint_is_included(self):
        result = ExecutionResult(status=ExecStatus.OOM, hint="试试分块读取")
        assert result.to_tool_payload()["hint"] == "试试分块读取"

    def test_status_serializes_as_plain_string(self):
        """str 枚举的意义：JSON 能直接序列化。"""
        import json

        payload = ExecutionResult(status=ExecStatus.OK).to_tool_payload()
        assert json.dumps(payload)
        assert payload["status"] == "OK"


# =============================================================== 快捷构造


class TestFactories:
    def test_rejection_has_no_exit_code(self):
        """precheck 拒绝时容器还没起，exit_code 为 None 本身也是信息：
        调用方能据此判断这次没有消耗任何执行资源。"""
        result = rejection("代码为空")
        assert result.status is ExecStatus.REJECTED
        assert result.exit_code is None
        assert result.raw_command == ()

    def test_sandbox_error_has_no_hint(self):
        result = sandbox_error("docker 不可用", command=["docker", "run"])
        assert result.status is ExecStatus.SANDBOX_ERROR
        assert result.hint == ""
        assert result.raw_command == ("docker", "run")

    def test_ok_property(self):
        assert ExecutionResult(status=ExecStatus.OK).ok is True
        assert ExecutionResult(status=ExecStatus.TIMEOUT).ok is False


# =============================================================== 静态预检


class TestPrecheck:
    def test_normal_code_passes(self):
        assert precheck_code("import pandas as pd\nprint(pd.__version__)") is None

    def test_empty_code_rejected(self):
        result = precheck_code("")
        assert result is not None and result.status is ExecStatus.REJECTED

    def test_comment_only_rejected(self):
        result = precheck_code("# 只是一些注释\n# 还没有写代码")
        assert result is not None and result.status is ExecStatus.REJECTED

    def test_oversized_code_rejected(self):
        result = precheck_code("x = 1\n" * 10000, max_code_bytes=1000)
        assert result is not None
        assert "超过上限" in result.error_summary

    @pytest.mark.parametrize(
        "module", ["socket", "requests", "subprocess", "ctypes", "shutil"]
    )
    def test_restricted_imports_rejected(self, module: str):
        result = precheck_code(f"import {module}\nprint(1)")
        assert result is not None
        assert result.status is ExecStatus.REJECTED
        assert module in result.error_summary

    def test_dotted_import_checks_root_module(self):
        """urllib.request 这种带点的导入，要按根模块名 urllib 判定。"""
        result = precheck_code("import urllib.request\nprint(1)")
        assert result is not None and result.status is ExecStatus.REJECTED

    @pytest.mark.parametrize("snippet", ["os.system('ls')", "eval('1+1')", "shutil.rmtree('/')"])
    def test_dangerous_patterns_rejected(self, snippet: str):
        result = precheck_code(snippet)
        assert result is not None and result.status is ExecStatus.REJECTED

    def test_multiprocessing_rejected(self):
        """fork 炸弹入口，即使有 pids-limit 也先在源头拦掉。"""
        assert precheck_code("import multiprocessing\nprint(1)") is not None

    # ------------------------------------------------ 文件访问范围

    @pytest.mark.parametrize(
        "snippet",
        [
            "open('/data/销售.csv', encoding='utf-8')",
            "open('/out/chart.png', 'wb')",
            "with open('/data/sales.xlsx','rb') as f: pass",
        ],
    )
    def test_reading_mounted_files_is_allowed(self, snippet: str):
        """读挂载进来的数据是**系统的本职工作**，绝不能被拦。

        曾经这里是一条粗暴的 "open('/" 匹配，结果把读 /data 的正常分析代码
        全判为危险 —— 数据就在那里，不让读等于把整套系统废掉。
        """
        assert precheck_code(snippet) is None

    def test_relative_path_is_allowed(self):
        """工作目录是 /out，相对路径的落点必然在沙箱内。"""
        assert precheck_code("open('result.csv', 'w', encoding='utf-8')") is None

    @pytest.mark.parametrize(
        "snippet",
        [
            "open('/etc/passwd')",
            "open('/Users/alice/.ssh/id_rsa')",
            "open('C:/Users/alice/secret.txt')",
            "open('../../etc/passwd')",
            "open('/proc/self/environ')",
        ],
    )
    def test_paths_outside_mounts_are_rejected(self, snippet: str):
        result = precheck_code(snippet)
        assert result is not None
        assert result.status is ExecStatus.REJECTED
        assert "/data" in result.hint or "/out" in result.hint


# =============================================================== 文件名归一化


class TestSafeTargetName:
    def test_basename_only(self):
        from pathlib import Path

        assert safe_target_name(Path("sub/dir/销售.csv")) == "销售.csv"

    def test_path_traversal_neutralized(self):
        """ ../../etc/passwd 必须变成安全的纯文件名 —— 否则「副本隔离」形同虚设。"""
        from pathlib import Path

        name = safe_target_name(Path("../../etc/passwd"))
        assert ".." not in name
        assert "/" not in name and "\\" not in name

    def test_dotdot_name_falls_back(self):
        from pathlib import Path

        assert safe_target_name(Path("..")) == "data"

    def test_unique_path_avoids_collision(self, tmp_path):
        (tmp_path / "a.csv").write_text("x", encoding="utf-8")
        first = unique_path(tmp_path, "a.csv")
        assert first.name == "a_2.csv"
        (tmp_path / "a_2.csv").write_text("x", encoding="utf-8")
        assert unique_path(tmp_path, "a.csv").name == "a_3.csv"
