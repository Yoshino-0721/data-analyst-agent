"""Function Calling 循环的四个核心场景。

这里的断言刻意验的是**行为契约**而不是实现细节：
模型收到了什么、循环怎么收尾、有没有把工具摘掉。
这些才是决定 Agent 能不能用的东西。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from src.agent.loop import AgentResult, run_agent
from src.agent.tools import RUN_PYTHON, ToolRuntime
from tests.conftest_agent import (
    ScriptedClient,
    StubExecutor,
    assistant_text,
    assistant_with_tools,
    error_result,
    ok_result,
    tool_call,
)


def make_runtime(executor, tmp_path: Path, **kwargs) -> ToolRuntime:
    return ToolRuntime(
        executor=executor,
        artifact_dir=tmp_path / "artifacts",
        run_dir_factory=lambda: tmp_path / "run_1",
        **kwargs,
    )


# ------------------------------------------------------------------ 场景一


class TestHappyPath:
    def test_tool_then_answer(self, tmp_path):
        """调用工具 → 成功 → 基于结果给出总结。"""
        executor = StubExecutor([ok_result("华东销售额最高：12000")])
        client = ScriptedClient(
            [
                assistant_with_tools(tool_call(RUN_PYTHON, '{"code": "print(1)"}')),
                assistant_text("华东最高，销售额 12000。"),
            ]
        )

        result = run_agent(
            "哪个地区销售额最高？",
            client=client,
            runtime=make_runtime(executor, tmp_path),
            max_steps=4,
        )

        assert result.answer == "华东最高，销售额 12000。"
        assert result.terminated_by == "answer"
        assert len(result.steps) == 1
        assert executor.requests[0].code == "print(1)"

    def test_successful_result_is_fed_back(self, tmp_path):
        """执行结果必须真的出现在给模型的下一条消息里。"""
        executor = StubExecutor([ok_result("华东销售额最高：12000")])
        client = ScriptedClient(
            [
                assistant_with_tools(tool_call(RUN_PYTHON, '{"code": "print(1)"}')),
                assistant_text("答案"),
            ]
        )
        run_agent("q", client=client, runtime=make_runtime(executor, tmp_path), max_steps=3)

        second_call = client.calls[1]["messages"]
        tool_messages = [m for m in second_call if m.get("role") == "tool"]
        assert tool_messages, "执行结果没有被回填"
        assert "华东销售额最高：12000" in tool_messages[0]["content"]

    def test_tool_call_id_matches(self, tmp_path):
        """回填消息的 tool_call_id 必须与请求一致 —— 对不上 API 直接 400。"""
        executor = StubExecutor([ok_result()])
        call = tool_call(RUN_PYTHON, '{"code": "print(1)"}', call_id="call_abc")
        client = ScriptedClient(
            [assistant_with_tools(call), assistant_text("答案")]
        )

        run_agent("q", client=client, runtime=make_runtime(executor, tmp_path), max_steps=3)

        messages = client.calls[1]["messages"]
        assistant_msg = next(m for m in messages if m.get("role") == "assistant")
        tool_msg = next(m for m in messages if m.get("role") == "tool")
        assert assistant_msg["tool_calls"][0]["id"] == "call_abc"
        assert tool_msg["tool_call_id"] == "call_abc"

    def test_arguments_are_kept_as_raw_string(self, tmp_path):
        """arguments 原样回填，不做 loads/dumps 往返。

        模型输出不是严格 JSON 时（尾随逗号、单引号），往返一次就破坏了原样。
        """
        executor = StubExecutor([ok_result()])
        raw = '{"code": "print(\'hi\')"}'
        client = ScriptedClient(
            [
                assistant_with_tools(tool_call(RUN_PYTHON, raw)),
                assistant_text("答案"),
            ]
        )

        run_agent("q", client=client, runtime=make_runtime(executor, tmp_path), max_steps=3)

        messages = client.calls[1]["messages"]
        assistant_msg = next(m for m in messages if m.get("role") == "assistant")
        assert assistant_msg["tool_calls"][0]["function"]["arguments"] == raw


# ------------------------------------------------------------------ 场景二


class TestRecoverFromError:
    def test_error_then_success(self, tmp_path):
        """第一次失败 → 看回填 → 第二次成功。这是闭环存在的意义。"""
        executor = StubExecutor([error_result(), ok_result("华东：12000")])
        client = ScriptedClient(
            [
                assistant_with_tools(tool_call(RUN_PYTHON, '{"code": "bad"}')),
                assistant_with_tools(tool_call(RUN_PYTHON, '{"code": "good"}')),
                assistant_text("华东 12000。"),
            ]
        )

        result = run_agent(
            "哪个地区最高？",
            client=client,
            runtime=make_runtime(executor, tmp_path),
            max_steps=4,
        )

        assert result.terminated_by == "answer"
        assert len(result.steps) == 2
        assert executor.requests[1].code == "good"

    def test_error_feedback_contains_traceback_and_hint(self, tmp_path):
        """模型要能看到错在哪（traceback）和下一步怎么办（hint）。"""
        executor = StubExecutor([error_result(stderr='  File "script.py", line 3\nKeyError: 地区')])
        client = ScriptedClient(
            [
                assistant_with_tools(tool_call(RUN_PYTHON, '{"code": "bad"}')),
                assistant_text("答案"),
            ]
        )

        run_agent("q", client=client, runtime=make_runtime(executor, tmp_path), max_steps=3)

        tool_message = next(
            m for m in client.calls[1]["messages"] if m.get("role") == "tool"
        )
        content = tool_message["content"]
        assert "KeyError: 地区" in content
        assert "hint" in content

    def test_hint_is_the_last_thing_model_reads(self, tmp_path):
        """hint 必须在回填文本的**末尾**。

        项目一实测：约束写在系统提示里几乎无效；挂在最靠近生成位置的
        那条消息末尾才有效。这个位置是踩过坑换来的，别挪。
        """
        executor = StubExecutor(
            [error_result(hint="先用 get_schema 确认列名")]
        )
        client = ScriptedClient(
            [
                assistant_with_tools(tool_call(RUN_PYTHON, '{"code": "bad"}')),
                assistant_text("答案"),
            ]
        )

        run_agent("q", client=client, runtime=make_runtime(executor, tmp_path), max_steps=3)

        content = next(
            m for m in client.calls[1]["messages"] if m.get("role") == "tool"
        )["content"]
        lines = [line for line in content.splitlines() if line.strip()]
        assert lines[-1].startswith("hint:"), f"最后一行不是 hint：{lines[-1]!r}"


# ------------------------------------------------------------------ 场景三


class TestRepeatedFailureGuard:
    def test_three_same_errors_force_finish(self, tmp_path):
        """连续 3 次同一处错误 → 强制收尾，不再让它原地重试。"""
        executor = StubExecutor([error_result() for _ in range(3)])
        client = ScriptedClient(
            [
                assistant_with_tools(tool_call(RUN_PYTHON, '{"code": "a"}', "c1")),
                assistant_with_tools(tool_call(RUN_PYTHON, '{"code": "b"}', "c2")),
                assistant_with_tools(tool_call(RUN_PYTHON, '{"code": "c"}', "c3")),
                assistant_text("我无法完成，建议先确认列名。"),
            ]
        )

        result = run_agent(
            "q",
            client=client,
            runtime=make_runtime(executor, tmp_path),
            max_steps=6,
            max_repeat_failures=3,
        )

        assert result.terminated_by == "repeated_error"
        assert len(result.steps) == 3, "第 3 次就该停，不该继续"

    def test_escalation_text_is_appended(self, tmp_path):
        """第 3 次失败时，回填里要出现更强的换思路提示。"""
        executor = StubExecutor([error_result() for _ in range(3)])
        client = ScriptedClient(
            [
                assistant_with_tools(tool_call(RUN_PYTHON, '{"code": "a"}', "c1")),
                assistant_with_tools(tool_call(RUN_PYTHON, '{"code": "b"}', "c2")),
                assistant_with_tools(tool_call(RUN_PYTHON, '{"code": "c"}', "c3")),
                assistant_text("放弃"),
            ]
        )

        run_agent(
            "q", client=client, runtime=make_runtime(executor, tmp_path),
            max_steps=6, max_repeat_failures=3,
        )

        last = client.calls[-1]["messages"]
        tool_contents = [
            m["content"] for m in last if m.get("role") == "tool"
        ]
        assert any("同一个错误" in content for content in tool_contents)

    def test_escalation_stays_at_the_end(self, tmp_path):
        """升级提示要挂在 hint 之后 —— 它是比常规 hint 更强的信号。"""
        executor = StubExecutor([error_result() for _ in range(3)])
        client = ScriptedClient(
            [
                assistant_with_tools(tool_call(RUN_PYTHON, '{"code": "a"}', "c1")),
                assistant_with_tools(tool_call(RUN_PYTHON, '{"code": "b"}', "c2")),
                assistant_with_tools(tool_call(RUN_PYTHON, '{"code": "c"}', "c3")),
                assistant_text("放弃"),
            ]
        )

        run_agent(
            "q", client=client, runtime=make_runtime(executor, tmp_path),
            max_steps=6, max_repeat_failures=3,
        )

        contents = [
            m["content"] for m in client.calls[-1]["messages"] if m.get("role") == "tool"
        ]
        last_lines = [
            line for line in contents[-1].splitlines() if line.strip()
        ]
        assert "同一个错误" in last_lines[-1]

    def test_different_errors_do_not_trigger_guard(self, tmp_path):
        """错得不一样不算撞墙 —— 模型在真的尝试不同方案。"""
        executor = StubExecutor(
            [
                error_result(summary="KeyError: 地区", stderr="KeyError: 地区"),
                error_result(summary="ValueError: bad", stderr="ValueError: bad"),
                error_result(summary="TypeError: x", stderr="TypeError: x"),
            ]
        )
        client = ScriptedClient(
            [
                assistant_with_tools(tool_call(RUN_PYTHON, '{"code": "a"}', "c1")),
                assistant_with_tools(tool_call(RUN_PYTHON, '{"code": "b"}', "c2")),
                assistant_with_tools(tool_call(RUN_PYTHON, '{"code": "c"}', "c3")),
                assistant_text("答案"),
            ]
        )

        result = run_agent(
            "q", client=client, runtime=make_runtime(executor, tmp_path),
            max_steps=6, max_repeat_failures=3,
        )

        assert result.terminated_by == "answer", "不同错误不应触发防撞墙"

    def test_success_resets_the_counter(self, tmp_path):
        """中间成功一次，失败计数就该清零。"""
        executor = StubExecutor(
            [error_result(), ok_result("部分结果"), error_result(), error_result()]
        )
        client = ScriptedClient(
            [
                assistant_with_tools(tool_call(RUN_PYTHON, '{"code": "a"}', "c1")),
                assistant_with_tools(tool_call(RUN_PYTHON, '{"code": "b"}', "c2")),
                assistant_with_tools(tool_call(RUN_PYTHON, '{"code": "c"}', "c3")),
                assistant_with_tools(tool_call(RUN_PYTHON, '{"code": "d"}', "c4")),
                assistant_text("答案"),
            ]
        )

        result = run_agent(
            "q", client=client, runtime=make_runtime(executor, tmp_path),
            max_steps=6, max_repeat_failures=3,
        )

        assert result.terminated_by == "answer", "成功一次后不应立刻触发防撞墙"


# ------------------------------------------------------------------ 场景四


class TestMaxSteps:
    def test_tools_are_removed_on_final_call(self, tmp_path):
        """步数用尽后**摘掉工具**再问一次 —— 不给它继续调用的机会。"""
        executor = StubExecutor([ok_result() for _ in range(5)])
        client = ScriptedClient(
            [
                assistant_with_tools(tool_call(RUN_PYTHON, '{"code": "a"}', "c1")),
                assistant_with_tools(tool_call(RUN_PYTHON, '{"code": "b"}', "c2")),
                assistant_text("基于已有信息的总结。"),
            ]
        )

        result = run_agent(
            "q", client=client, runtime=make_runtime(executor, tmp_path), max_steps=2
        )

        assert result.terminated_by == "max_steps"
        assert client.last_call["tools"] is None, "收尾那一轮必须摘掉 tools"

    def test_final_instruction_is_added(self, tmp_path):
        executor = StubExecutor([ok_result() for _ in range(5)])
        client = ScriptedClient(
            [
                assistant_with_tools(tool_call(RUN_PYTHON, '{"code": "a"}', "c1")),
                assistant_text("总结"),
            ]
        )

        run_agent("q", client=client, runtime=make_runtime(executor, tmp_path), max_steps=1)

        last_messages = client.last_call["messages"]
        assert any(
            "不要再调用工具" in str(m.get("content", ""))
            for m in last_messages
            if m.get("role") == "user"
        )

    def test_answer_comes_from_final_call(self, tmp_path):
        executor = StubExecutor([ok_result()])
        client = ScriptedClient(
            [
                assistant_with_tools(tool_call(RUN_PYTHON, '{"code": "a"}', "c1")),
                assistant_text("被迫给出的总结"),
            ]
        )

        result = run_agent(
            "q", client=client, runtime=make_runtime(executor, tmp_path), max_steps=1
        )
        assert result.answer == "被迫给出的总结"

    def test_max_steps_must_be_positive(self, tmp_path):
        with pytest.raises(ValueError, match="max_steps"):
            run_agent(
                "q",
                client=ScriptedClient([]),
                runtime=make_runtime(StubExecutor([]), tmp_path),
                max_steps=0,
            )


# ------------------------------------------------------------------ 杂项


class TestLoopBasics:
    def test_system_prompt_is_first_message(self, tmp_path):
        executor = StubExecutor([])
        client = ScriptedClient([assistant_text("直接回答")])
        run_agent("q", client=client, runtime=make_runtime(executor, tmp_path), max_steps=1)

        messages = client.calls[0]["messages"]
        assert messages[0]["role"] == "system"
        assert messages[1]["role"] == "user"
        assert messages[1]["content"] == "q"

    def test_no_tool_call_answers_immediately(self, tmp_path):
        """模型不调用工具就直接收尾 —— 不该空转。"""
        client = ScriptedClient([assistant_text("这个问题不需要跑代码")])
        result = run_agent(
            "你好", client=client, runtime=make_runtime(StubExecutor([]), tmp_path), max_steps=5
        )
        assert result.terminated_by == "answer"
        assert client.call_count == 1

    def test_artifacts_are_collected(self, tmp_path):
        chart = tmp_path / "chart.png"
        executor = StubExecutor([ok_result(artifacts=(chart,))])
        client = ScriptedClient(
            [
                assistant_with_tools(tool_call(RUN_PYTHON, '{"code": "a"}')),
                assistant_text("图已生成"),
            ]
        )
        result = run_agent(
            "q", client=client, runtime=make_runtime(executor, tmp_path), max_steps=3
        )
        assert result.artifacts == ("chart.png",)

    def test_result_type(self, tmp_path):
        client = ScriptedClient([assistant_text("x")])
        result = run_agent(
            "q", client=client, runtime=make_runtime(StubExecutor([]), tmp_path), max_steps=1
        )
        assert isinstance(result, AgentResult)
